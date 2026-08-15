#!/usr/bin/env python3
"""Unified hardcoded-credential scanner (P0-SEC-01 / review R-01 / S2-05).

Stable entry point:

    python3 scripts/security_scan.py --scope tree,index,build-context,image \
        --redact --fail-on-hit

Scopes:

- tree          : every file tracked by git at an EXPLICIT commit
                  (--commit, default HEAD), read from the git tree object -
                  never from the working tree
- index         : the staged (index) version of every tracked file
- build-context : files docker would send for each declared build context
                  (honors .dockerignore, see BUILD_CONTEXTS below)
- image         : the final OCI image of each declared build context;
                  builds the image when --build-image is passed, otherwise
                  scans an existing local image tag per context. Fails
                  closed (exit 2) when docker is unavailable unless
                  --skip-unavailable is given.

S2-05 hardening:

- the allowlist is EXACT-VALUE only: a value is exempt solely when it
  equals a registered placeholder (or is a <placeholder>); substrings like
  fake/example/changeme inside a real formatted token are NO LONGER exempt
  and are always reported;
- .env.example has no whole-file exemption; only the two explicit
  low-risk dev placeholder lines are exempt, each pinned to path+rule+line
  with an owner and an expiry date (expired exemptions stop applying);
- files are scanned STREAMING in chunks - there is no size-based skip for
  text; oversized git blobs and image members are scanned in full;
- any tracked/index/build-context member that cannot be read, or that is
  binary and therefore not text-scannable, is recorded in the ``unscanned``
  report section and fails the scan (exit 2) in those scopes - never a
  silent skip;
- image members that are binary are reported in ``unscanned`` (images
  legitimately contain binaries) but never hide a text member from
  scanning.

Output rules (audited by map_core/tests/test_hardcoded_credential_scan.py):

- the matched secret is NEVER printed, only file:line pattern sha256:<16>;
- --redact is accepted for compatibility and is always on;
- exit code: 0 = no hits; 1 = hits found with --fail-on-hit; 2 = scan error
  (unreadable members, unavailable tooling, bad usage).

Exemptions are registered below as exact (path, rule, line) entries with an
owner and an expiry date; changing a file's lines invalidates the exemption
and the scan fails closed instead of silently widening.
"""

from __future__ import annotations

import argparse
import codecs
import fnmatch
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]

CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("gpustack_token", re.compile(r"gpustack_[a-z0-9]+_[a-z0-9]{16,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{24,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{24,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[bap]-[A-Za-z0-9-]{10,}\b")),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "uri_embedded_password",
        re.compile(
            r"(?:mongodb|postgres(?:ql)?|mysql|redis|amqp|minio|https?|s3)s?://"
            r"[^/\s\"\']+:[^/\s\"\']+@"
        ),
    ),
    (
        "basic_auth_token",
        re.compile(r"[\"\']Basic\s+[A-Za-z0-9+/]{16,}={0,2}[\"\']"),
    ),
    (
        "session_cookie_token",
        re.compile(r"[\"\']SESSION_[A-Z0-9_]{6,}=[A-Za-z0-9_-]{16,}[\"\']"),
    ),
    (
        "literal_password_assignment",
        re.compile(r"\bpassword\s*=\s*[\"\'][^\"\']{4,}[\"\']", re.IGNORECASE),
    ),
    (
        "literal_secret_assignment",
        re.compile(
            r"\b(secret|auth_token|api_key|access_key|secret_key)\s*=\s*[\"\'][^\"\']{12,}[\"\']",
            re.IGNORECASE,
        ),
    ),
    (
        "env_password_literal",
        re.compile(r"^\s*[A-Z0-9_]*PASSWORD[A-Z0-9_]*=[^\s$]{4,}\s*$"),
    ),
    (
        "env_token_literal",
        re.compile(r"^\s*[A-Z0-9_]*(TOKEN|API_KEY|SECRET)[A-Z0-9_]*=[^\s$]{12,}\s*$"),
    ),
]

# S2-05: exact-value allowlist only. A match is exempt ONLY when the whole
# matched value equals one of these placeholders (or is a <placeholder>).
# Substrings (fake/example/changeme inside a real formatted token) are
# reported.
_ALLOWED_EXACT_VALUES: frozenset[str] = frozenset(
    {
        "changeme",
        "example",
        "fake",
        "fake-key",
        "test-api-key",
        "your_token",
        "your_user_id",
        "your_name",
        "<redacted>",
        "<model-endpoint>",
        "<random>",
        "<local-dev-password>",
        "<your-password-here>",
    }
)


@dataclass(frozen=True)
class Exemption:
    """Exact (path, rule, line) exemption with an owner and an expiry date.

    Only the registered line of the registered file is exempt for the
    registered rule. A line shift or a rule drift invalidates the exemption
    and the scan fails closed. S3-03: ``expected_fingerprint`` pins the
    EXPECTED matched value (sha256, same hash as Hit.fingerprint) - if the
    value on that line changes (someone swaps a real credential into an
    exempt line), the exemption stops applying and the hit is reported as
    an EXEMPTION DRIFT.
    """

    path: str
    rule: str
    line: int
    reason: str
    owner: str
    expires_at: str  # ISO date YYYY-MM-DD
    expected_fingerprint: str | None = None


EXEMPTIONS: tuple[Exemption, ...] = (
    # .env.example dev-only placeholder values (low-risk, non-secret).
    Exemption(
        ".env.example", "env_password_literal", 34,
        "dev-only local placeholder, never a real credential",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:74b2ed30f9b371e0",
    ),
    Exemption(
        ".env.example", "env_password_literal", 38,
        "dev-only local placeholder, never a real credential",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:374941dff629ce3f",
    ),
    # Test canary fixtures: purpose-built fake tokens used to prove the
    # sanitizers/egress wipe secrets. They are never real credentials.
    Exemption(
        "map_core/tests/test_sensitive_data.py", "openai_key", 9,
        "test canary fixture (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:3ffa01e518c4680f",
    ),
    Exemption(
        "map_core/tests/test_mcp_egress_guard.py", "openai_key", 34,
        "test canary fixture (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:3ffa01e518c4680f",
    ),
    Exemption(
        "map_core/tests/test_mcp_egress_guard.py", "openai_key", 179,
        "test fixture value (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:0e3b7d6dfa4cd4f9",
    ),
    Exemption(
        "map_core/tests/test_mcp_egress_guard.py", "openai_key", 183,
        "test fixture value (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:0e3b7d6dfa4cd4f9",
    ),
    Exemption(
        "map_core/tests/test_mcp_egress_guard.py", "openai_key", 184,
        "test fixture value (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:0e3b7d6dfa4cd4f9",
    ),
    Exemption(
        "map_core/tests/test_industry_chat_canary.py", "openai_key", 14,
        "test canary fixture (fake token)", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:73581a736bb0307c",
    ),
    # Auth-boundary fixtures: purpose-built fake bearer tokens proving the
    # identity gates; never real credentials.
    Exemption(
        "map-business-backend/tests/integration/test_config_audit.py",
        "literal_secret_assignment", 42,
        "test fixture: fake bearer token", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:b51dbcb5689cdc37",
    ),
    Exemption(
        "map-business-backend/tests/integration/test_feedback.py",
        "literal_secret_assignment", 34,
        "test fixture: fake bearer token", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:df943f1ffb699d19",
    ),
    Exemption(
        "map-business-backend/tests/integration/test_v1_error_matrix.py",
        "literal_secret_assignment", 39,
        "test fixture: fake matrix secret", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:89298538bf28486d",
    ),
    Exemption(
        "map-business-backend/tests/test_auth_boundary.py",
        "literal_secret_assignment", 28,
        "test fixture: fake boundary secret", "platform-security", "2027-08-31",
        expected_fingerprint="sha256:cc489fa5f758c598",
    ),
    Exemption(
        "map_core/tests/test_sensitive_data.py", "uri_embedded_password", 32,
        "test fixture: fake DSN with placeholder password",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:04f2177864a1ace4",
    ),
    # README quickstart DSNs: documented dev placeholders, never secrets.
    Exemption(
        "README.md", "uri_embedded_password", 335,
        "documented dev DSN placeholder (P0-SEC-01 injection example)",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:8aac73349fc3c064",
    ),
    Exemption(
        "README.md", "uri_embedded_password", 336,
        "documented dev DSN placeholder (P0-SEC-01 injection example)",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:90f504e0156a0007",
    ),
    Exemption(
        "README.md", "uri_embedded_password", 352,
        "documented dev DSN placeholder (P0-SEC-01 injection example)",
        "platform-security", "2027-08-31",
        expected_fingerprint="sha256:90f504e0156a0007",
    ),
)

BUILD_CONTEXTS: dict[str, str] = {
    "map_core": "map_core",
    "map-business-backend": "map-business-backend",
    "map-observability-backend": "map-observability/map-observability-backend",
}

MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024
# Members larger than this in an image layer are scanned STREAMING (not
# skipped); the constant only guards the binary sniffing prefix.
MAX_IMAGE_LAYER_MEMBER_BYTES = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024

# Scopes whose unreadable/binary members FAIL the scan (exit 2). Image
# scopes legitimately contain binaries, so image members are only reported.
STRICT_UNSCANNED_SCOPES = ("tree", "index", "build-context")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        check=check,
    )


def _git_text(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout


def fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class Hit:
    __slots__ = ("location", "line", "pattern", "fingerprint", "length")

    def __init__(self, location: str, line: int, pattern: str, value: str) -> None:
        self.location = location
        self.line = line
        self.pattern = pattern
        self.fingerprint = fingerprint(value)
        self.length = len(value.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "line": self.line,
            "pattern": self.pattern,
            "fingerprint": self.fingerprint,
            "length": self.length,
        }


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _exemption_expired(exemption: Exemption) -> bool:
    try:
        expires = date.fromisoformat(exemption.expires_at)
    except ValueError:
        return True  # malformed expiry = never applies (fail closed)
    return expires < date.today()


def _hit_relpath(hit: Hit) -> str:
    """Repo-relative path from a hit location ("scope:relpath")."""
    return hit.location.split(":", 1)[1] if ":" in hit.location else hit.location


def exempted_exemption(
    relpath: str, line: int, pattern: str, value: str
) -> tuple[Exemption | None, bool]:
    """Return (exemption, drifted) for (relpath, line, pattern, value).

    S3-03: an exemption whose expected fingerprint does not match the
    value actually found on that line has DRIFTED - it no longer applies
    and the caller reports the hit (fail closed).
    """
    for exemption in EXEMPTIONS:
        if exemption.path != relpath:
            continue
        if exemption.line != line:
            continue
        if exemption.rule != pattern:
            continue
        if _exemption_expired(exemption):
            continue
        if (
            exemption.expected_fingerprint is not None
            and exemption.expected_fingerprint != fingerprint(value)
        ):
            return None, True
        return exemption, False
    return None, False


_ASSIGNMENT_PATTERNS: frozenset[str] = frozenset(
    {
        "literal_password_assignment",
        "literal_secret_assignment",
        "env_password_literal",
        "env_token_literal",
    }
)


def _extract_assigned_value(value: str) -> str:
    """For assignment-style patterns, reduce the match to the VALUE part.

    ``password = "changeme"`` -> ``changeme``, ``X_PASSWORD=somevalue`` ->
    ``somevalue`` - the exact-value allowlist applies to the value, not to
    the whole statement.
    """
    match = re.search(r"=\s*(.*)$", value)
    if not match:
        return value
    extracted = match.group(1).strip()
    if len(extracted) >= 2 and extracted[0] == extracted[-1] and extracted[0] in "\"'":
        extracted = extracted[1:-1]
    return extracted


_PLACEHOLDER_RE = re.compile(r"^<[A-Za-z0-9_ ./-]{1,64}>$")


def _allowed_hit(value: str) -> bool:
    # S3-03: placeholders must be a WHOLE value matching the strict
    # placeholder shape - an arbitrary "<...>" substring inside a real
    # formatted token is NOT exempt.
    if _PLACEHOLDER_RE.fullmatch(value):
        return True
    return value in _ALLOWED_EXACT_VALUES


def scan_text(
    text: str,
    location: str,
    hits: list[Hit],
    *,
    relpath: str | None = None,
    exemptions_used: list[Exemption] | None = None,
    drifted_exemptions: list[str] | None = None,
) -> None:
    """Scan a text block line by line; exact exemptions are honored."""
    effective_relpath = relpath if relpath is not None else _hit_relpath_of(location)
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                check_value = (
                    _extract_assigned_value(value)
                    if pattern_name in _ASSIGNMENT_PATTERNS
                    else value
                )
                if _allowed_hit(check_value):
                    continue
                exemption, drifted = exempted_exemption(
                    effective_relpath, line_no, pattern_name, value
                )
                if exemption is not None:
                    if exemptions_used is not None:
                        exemptions_used.append(exemption)
                    continue
                if drifted:
                    # S3-03: the exempt line's value changed - report the hit
                    # AND the drift so the failure is self-explanatory.
                    if drifted_exemptions is not None:
                        drifted_exemptions.append(
                            "%s:%d: exempted value drifted for rule %s"
                            % (effective_relpath, line_no, pattern_name)
                        )
                hits.append(Hit(location, line_no, pattern_name, value))


def _hit_relpath_of(location: str) -> str:
    return location.split(":", 1)[1] if ":" in location else location


def _decode_prefix(data: bytes) -> tuple[codecs.IncrementalDecoder, str]:
    """Pick the text encoding for a byte stream.

    UTF-8 first (strict on the prefix); fall back to latin-1 which never
    fails and preserves byte offsets for the ASCII credential patterns.
    """
    prefix = data[: STREAM_CHUNK_BYTES]
    try:
        prefix.decode("utf-8")
        return codecs.getincrementaldecoder("utf-8")("replace"), "utf-8"
    except UnicodeDecodeError:
        return codecs.getincrementaldecoder("latin-1")("strict"), "latin-1"


def _iter_lines(chunks: Iterator[bytes]) -> Iterator[str]:
    """Streaming line iterator: chunked decode, never buffers whole files."""
    first_chunk = next(chunks, b"")
    if not first_chunk:
        return
    decoder, _encoding = _decode_prefix(first_chunk)
    carry = ""
    chunk = first_chunk
    while chunk:
        carry += decoder.decode(chunk)
        lines = carry.splitlines()
        if not lines:
            chunk = next(chunks, b"")
            continue
        carry = lines.pop()
        yield from lines
        chunk = next(chunks, b"")
    if carry:
        yield carry


def scan_bytes_stream(
    chunks: Iterator[bytes],
    location: str,
    relpath: str,
    hits: list[Hit],
    unscanned: list[dict[str, str]],
    *,
    strict: bool,
    drifted_exemptions: list[str] | None = None,
) -> bool:
    """Streaming scan of a byte source; returns False when unscannable.

    Binary members are recorded in ``unscanned`` and never silently
    skipped; oversized text is scanned in full (no size-based skip).
    """
    first = next(chunks, b"")
    if not first:
        return True
    if _is_binary(first):
        unscanned.append({"location": location, "reason": "binary"})
        return False

    def all_chunks() -> Iterator[bytes]:
        yield first
        yield from chunks

    for line_no, line in enumerate(_iter_lines(all_chunks()), start=1):
        for pattern_name, pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                check_value = (
                    _extract_assigned_value(value)
                    if pattern_name in _ASSIGNMENT_PATTERNS
                    else value
                )
                if _allowed_hit(check_value):
                    continue
                exemption, drifted = exempted_exemption(
                    relpath, line_no, pattern_name, value
                )
                if exemption is not None:
                    continue
                if drifted and drifted_exemptions is not None:
                    drifted_exemptions.append(
                        "%s:%d: exempted value drifted for rule %s"
                        % (relpath, line_no, pattern_name)
                    )
                hits.append(Hit(location, line_no, pattern_name, value))
    return True


def scan_file_bytes(
    data: bytes,
    location: str,
    hits: list[Hit],
    *,
    relpath: str | None = None,
    unscanned: list[dict[str, str]] | None = None,
    drifted_exemptions: list[str] | None = None,
) -> bool:
    """Scan an in-memory byte buffer (whole file). Returns True when scanned.

    Oversized text is processed via the streaming iterator (no skip);
    binary buffers are recorded in ``unscanned`` and return False.
    """
    effective_relpath = relpath if relpath is not None else _hit_relpath_of(location)
    unscan_list = unscanned if unscanned is not None else []
    scanned = scan_bytes_stream(
        iter([data]),
        location,
        effective_relpath,
        hits,
        unscan_list,
        strict=True,
        drifted_exemptions=drifted_exemptions,
    )
    return scanned


def _is_exempt(relpath: str) -> bool:
    """Whole-file exemptions no longer exist (S2-05); kept for the audit test."""
    return False


def _git_blob(commit: str, rel: str) -> bytes | None:
    proc = _git("show", f"{commit}:{rel}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def scope_tree(
    hits: list[Hit],
    unscanned: list[dict[str, str]],
    *,
    commit: str | None = None,
    drifted_exemptions: list[str] | None = None,
) -> None:
    """Scan every tracked file AT the given commit (default HEAD).

    S2-05: contents come from the git tree object, never from the working
    tree; unreadable blobs are recorded and fail closed.
    """
    tree_commit = commit or _git_text("rev-parse", "HEAD").strip()
    proc = _git("ls-tree", "-r", "--name-only", "-z", tree_commit)
    for raw in proc.stdout.split(b"\x00"):
        rel = raw.decode("utf-8", "replace").strip()
        if not rel:
            continue
        blob = _git_blob(tree_commit, rel)
        location = "tree:%s" % rel
        if blob is None:
            unscanned.append(
                {"location": location, "reason": f"git blob unreadable at {tree_commit}"}
            )
            continue
        scan_file_bytes(
            blob, location, hits, relpath=rel, unscanned=unscanned,
            drifted_exemptions=drifted_exemptions,
        )


def scope_index(
    hits: list[Hit],
    unscanned: list[dict[str, str]],
    drifted_exemptions: list[str] | None = None,
) -> None:
    proc = _git("ls-files", "-z")
    for raw in proc.stdout.split(b"\x00"):
        rel = raw.decode("utf-8", "replace").strip()
        if not rel:
            continue
        blob = _git("show", ":" + rel, check=False)
        location = "index:%s" % rel
        if blob.returncode != 0:
            unscanned.append(
                {"location": location, "reason": "index blob unreadable"}
            )
            continue
        scan_file_bytes(
            blob.stdout, location, hits, relpath=rel, unscanned=unscanned,
            drifted_exemptions=drifted_exemptions,
        )


def _dockerignore_patterns(context: Path) -> list[str]:
    patterns: list[str] = []
    ignore_file = context / ".dockerignore"
    if not ignore_file.is_file():
        return patterns
    for raw in ignore_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _match_pattern(pattern: str, rel: str, is_dir: bool) -> bool:
    negate = pattern.startswith("!")
    if negate:
        pattern = pattern[1:]
    if pattern.endswith("/"):
        pattern = pattern[:-1]
        if not is_dir:
            return False
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    base = "/" + rel if anchored else rel
    return (
        fnmatch.fnmatch(base, pattern)
        or fnmatch.fnmatch(base, pattern + "/**")
    )


def _file_with_ancestors(rel: str) -> list[tuple[str, bool]]:
    """Return (path, is_dir) for the file and every ancestor directory.

    Docker applies a directory rule (e.g. '.venv/') to everything below
    that directory, so a file scan must consult its ancestor chain.
    """
    parts = rel.split("/")
    candidates: list[tuple[str, bool]] = []
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        candidates.append((prefix, i < len(parts)))
    return candidates


def _dockerignore_denies(patterns: list[str], rel: str) -> bool:
    # Docker semantics: last matching rule wins; directory rules apply to
    # the whole subtree, so evaluate the file and its ancestors in order.
    denied = False
    for candidate, is_dir in _file_with_ancestors(rel):
        for pattern in patterns:
            if _match_pattern(pattern, candidate, is_dir):
                denied = not pattern.startswith("!")
    return denied


def scope_build_context(
    hits: list[Hit],
    unscanned: list[dict[str, str]],
    drifted_exemptions: list[str] | None = None,
) -> None:
    for name, rel in BUILD_CONTEXTS.items():
        context = ROOT / rel
        if not context.is_dir():
            raise RuntimeError("build context %s missing: %s" % (name, context))
        patterns = _dockerignore_patterns(context)
        for path in sorted(p for p in context.rglob("*") if p.is_file()):
            relpath = path.relative_to(context).as_posix()
            if _dockerignore_denies(patterns, relpath):
                continue
            repo_rel = path.relative_to(ROOT).as_posix()
            location = "build-context:%s" % repo_rel
            try:
                with open(path, "rb") as fh:
                    scan_bytes_stream(
                        _chunked_file(fh),
                        location,
                        repo_rel,
                        hits,
                        unscanned,
                        strict=True,
                        drifted_exemptions=drifted_exemptions,
                    )
            except OSError as exc:
                unscanned.append(
                    {"location": location, "reason": "unreadable: %s" % exc}
                )


def _chunked_file(fh: Any) -> Iterator[bytes]:
    while True:
        chunk = fh.read(STREAM_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _build_image(context_name: str, context: Path, tag: str) -> None:
    proc = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(context)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError("docker build for %s failed: %s" % (context_name, tail))


def _is_third_party_image_member(name: str) -> bool:
    """Dependency/OS code inside the image is not ours.

    The Dockerfiles COPY the repository into /app - only that subtree
    (minus vendored dependency dirs) counts as first-party code whose text
    patterns fail the scan. Everything else (OS tooling under /usr,
    /usr/share docs, /root/.cache build caches, perl/python stdlib) is
    third-party and reported as third_party_hits only.

    NOTE: this layout coupling mirrors the Dockerfiles; moving our code to
    a different image path must update this classifier too.
    """
    path = name.lstrip("/")
    if path.startswith("app/"):
        return (
            "/site-packages/" in path
            or "/dist-packages/" in path
            or "/node_modules/" in path
            or "/.venv/" in path
        )
    return True


def _scan_image_tarball(
    name: str,
    raw: bytes,
    hits: list[Hit],
    third_party_hits: list[Hit],
    unscanned: list[dict[str, str]],
) -> None:
    """Scan a `docker save` tarball in BOTH supported layouts.

    - legacy: manifest.json + <layer>.tar members;
    - OCI (Docker Desktop default): manifest.json + blobs/sha256/<digest>
      members (config blobs are JSON text; layer blobs are gzip-compressed
      tars - opened with mode "r:*" so the compression is auto-detected).

    Every text member is scanned; binary members are recorded in
    ``unscanned`` (never silently skipped).
    """
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as outer:
        for member in outer.getmembers():
            if not member.isfile():
                continue
            payload = outer.extractfile(member)
            if payload is None:
                unscanned.append(
                    {"location": "image:%s#%s" % (name, member.name),
                     "reason": "unreadable"}
                )
                continue
            data = payload.read()
            location = "image:%s#%s" % (name, member.name)
            if member.name == "manifest.json" or member.name.endswith(".json"):
                # image manifest / config: scan as text, never skipped
                scan_bytes_stream(
                    iter([data]), location, member.name,
                    hits, unscanned, strict=False,
                )
                continue
            # a blob member: either a config JSON or a compressed layer tar
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as layer:
                    for inner in layer.getmembers():
                        if not inner.isfile():
                            continue
                        fp = layer.extractfile(inner)
                        if fp is None:
                            unscanned.append(
                                {
                                    "location": "image:%s/%s" % (name, inner.name),
                                    "reason": "unreadable",
                                }
                            )
                            continue
                        # S2-05: large members are scanned STREAMING in
                        # chunks; there is no size-based skip for text.
                        member_hits = (
                            third_party_hits
                            if _is_third_party_image_member(inner.name)
                            else hits
                        )
                        scan_bytes_stream(
                            _chunked_file(fp),
                            "image:%s/%s" % (name, inner.name),
                            inner.name,
                            member_hits,
                            unscanned,
                            strict=False,
                        )
            except tarfile.TarError:
                # not a tar: a plain-text blob (config JSON)
                scan_bytes_stream(
                    iter([data]), location, member.name,
                    hits, unscanned, strict=False,
                )


def scope_image(
    hits: list[Hit],
    third_party_hits: list[Hit],
    unscanned: list[dict[str, str]],
    *,
    build: bool,
    skip_unavailable: bool,
    image_tags: dict[str, str] | None,
    drifted_exemptions: list[str] | None = None,
    image_digests: dict[str, str] | None = None,
) -> None:
    if not _docker_available():
        if skip_unavailable:
            print("image scope skipped: docker unavailable", file=sys.stderr)
            return
        raise RuntimeError("image scope requires docker; it is unavailable")
    for name, rel in BUILD_CONTEXTS.items():
        context = ROOT / rel
        tag = (image_tags or {}).get(name) or "map-security-scan:" + name
        if build:
            _build_image(name, context, tag)
        else:
            probe = subprocess.run(
                ["docker", "image", "inspect", tag],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                raise RuntimeError(
                    "image scope: local image %r not found for %s; "
                    "pass --build-image to build it (or --skip-unavailable)"
                    % (tag, name)
                )
        # S3-03: record the actual immutable image digest of what was
        # scanned (SBOM/attribution evidence in the report). Pushed images
        # carry a registry digest; locally built ones fall back to their
        # content-addressed image ID.
        digest_probe = subprocess.run(
            ["docker", "image", "inspect", tag, "--format",
             "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}"],
            capture_output=True,
            text=True,
        )
        digest = digest_probe.stdout.strip() or tag
        if image_digests is not None:
            image_digests[name] = digest
        save = subprocess.run(["docker", "save", tag], capture_output=True)
        if save.returncode != 0:
            raise RuntimeError("docker save %s failed" % tag)
        _scan_image_tarball(
            name, save.stdout, hits, third_party_hits, unscanned
        )


def build_report(
    hits: list[Hit],
    scopes: list[str],
    exempt: dict[str, dict[str, str]],
    *,
    unscanned: list[dict[str, str]] | None = None,
    exemptions_used: list[Exemption] | None = None,
    commit: str | None = None,
    third_party_hits: list[Hit] | None = None,
    drifted_exemptions: list[str] | None = None,
    image_digests: dict[str, str] | None = None,
    scanner_version: str | None = None,
) -> dict[str, Any]:
    return {
        "scopes": scopes,
        "commit": commit,
        "scanner_version": scanner_version,
        "image_digests": dict(image_digests or {}),
        "hits": [hit.to_dict() for hit in hits],
        "hit_count": len(hits),
        "third_party_hits": [hit.to_dict() for hit in (third_party_hits or [])],
        "third_party_hit_count": len(third_party_hits or []),
        "drifted_exemptions": list(drifted_exemptions or []),
        "drifted_exemption_count": len(drifted_exemptions or []),
        "unscanned": list(unscanned or []),
        "unscanned_count": len(unscanned or []),
        "exempt_files": {
            path: {"reason": info["reason"], "owner": info["owner"]}
            for path, info in exempt.items()
        },
        "exemptions_used": [
            {
                "path": ex.path,
                "rule": ex.rule,
                "line": ex.line,
                "owner": ex.owner,
                "expires_at": ex.expires_at,
            }
            for ex in (exemptions_used or [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True,
                        help="comma-separated scopes: tree,index,build-context,image")
    parser.add_argument("--redact", action="store_true",
                        help="accepted for compatibility; output is always redacted")
    parser.add_argument("--fail-on-hit", action="store_true",
                        help="exit 1 when any hit is found (default: report only)")
    parser.add_argument("--skip-unavailable", action="store_true",
                        help="skip a scope whose tooling is unavailable instead of failing closed")
    parser.add_argument("--build-image", action="store_true",
                        help="build each context image before scanning (image scope)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "--commit", default=None,
        help="git commit whose tree the 'tree' scope scans (default HEAD)",
    )
    args = parser.parse_args(argv)

    scopes = [s.strip() for s in args.scope.split(",") if s.strip()]
    valid = {"tree", "index", "build-context", "image"}
    unknown = sorted(set(scopes) - valid)
    if unknown:
        print("unknown scope(s): %s" % ", ".join(unknown), file=sys.stderr)
        return 2
    if not scopes:
        print("no scope given", file=sys.stderr)
        return 2

    hits: list[Hit] = []
    third_party_hits: list[Hit] = []
    unscanned: list[dict[str, str]] = []
    drifted_exemptions: list[str] = []
    image_digests: dict[str, str] = {}
    try:
        for scope in scopes:
            if scope == "tree":
                scope_tree(
                    hits, unscanned, commit=args.commit,
                    drifted_exemptions=drifted_exemptions,
                )
            elif scope == "index":
                scope_index(hits, unscanned, drifted_exemptions)
            elif scope == "build-context":
                scope_build_context(hits, unscanned, drifted_exemptions)
            elif scope == "image":
                scope_image(
                    hits,
                    third_party_hits,
                    unscanned,
                    build=args.build_image,
                    skip_unavailable=args.skip_unavailable,
                    image_tags=None,
                    image_digests=image_digests,
                )
    except RuntimeError as exc:
        print("security scan error: %s" % exc, file=sys.stderr)
        return 2

    # S2-05 fail-closed: unreadable/binary members in strict scopes mean the
    # scan cannot prove those scopes clean - never a silent skip.
    strict_unscanned = [
        item for item in unscanned
        if item["location"].split(":", 1)[0] in STRICT_UNSCANNED_SCOPES
    ]

    scanner_version = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()[:16]
    report = build_report(
        hits,
        scopes,
        {},
        unscanned=unscanned,
        commit=args.commit,
        third_party_hits=third_party_hits,
        drifted_exemptions=drifted_exemptions,
        image_digests=image_digests,
        scanner_version=scanner_version,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for hit in hits:
            print("%s:%d: %s %s" % (hit.location, hit.line, hit.pattern, hit.fingerprint))
        print("security scan: %d hit(s) in %s" % (len(hits), ",".join(scopes)))
        print(
            "security scan: %d third-party hit(s) (dependency code, reported only)"
            % len(third_party_hits)
        )
        for item in unscanned:
            print("security scan: unscanned %s (%s)" % (item["location"], item["reason"]))
        for drift in drifted_exemptions:
            print("security scan: EXEMPTION DRIFT %s" % drift)
        for exemption in EXEMPTIONS:
            state = "EXPIRED" if _exemption_expired(exemption) else "active"
            print(
                "security scan: exemption %s:%d (%s; owner=%s; expires=%s; %s)"
                % (exemption.path, exemption.line, exemption.rule,
                   exemption.owner, exemption.expires_at, state)
            )

    if strict_unscanned:
        print(
            "security scan error: %d tracked/index/build-context member(s) "
            "could not be text-scanned (fail-closed); first: %s"
            % (len(strict_unscanned), strict_unscanned[0]["location"]),
            file=sys.stderr,
        )
        return 2

    if args.fail_on_hit and hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
