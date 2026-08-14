#!/usr/bin/env python3
"""Unified hardcoded-credential scanner (P0-SEC-01 / review R-01).

Stable entry point:

    python3 scripts/security_scan.py --scope tree,index,build-context,image \
        --redact --fail-on-hit

Scopes:

- tree          : every file tracked by git in the working tree
- index         : the staged (index) version of every tracked file
- build-context : files docker would send for each declared build context
                  (honors .dockerignore, see BUILD_CONTEXTS below)
- image         : the final OCI image of each declared build context;
                  builds the image when --build-image is passed, otherwise
                  scans an existing local image tag per context. Fails
                  closed (exit 2) when docker is unavailable unless
                  --skip-unavailable is given.

Output rules (audited by map_core/tests/test_hardcoded_credential_scan.py):

- the matched secret is NEVER printed, only file:line pattern sha256:<16>;
- --redact is accepted for compatibility and is always on;
- exit code: 0 = no hits; 1 = hits found with --fail-on-hit; 2 = scan error.

Exemptions must be registered here with a reason and an owner; a file is
exempt only on an exact path match (no wildcard drift).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

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

_ALLOWED_LITERALS: tuple[str, ...] = (
    "fake",
    "fake-key",
    "test-api-key",
    "your_token",
    "your_user_id",
    "your_name",
    "<redacted>",
    "<model-endpoint>",
    "<random>",
    "example",
    "changeme",
)

EXEMPT_FILES: dict[str, dict[str, str]] = {
    ".env.example": {
        "reason": "dev-only template with example values; production must override",
        "owner": "platform-security",
    },
}

BUILD_CONTEXTS: dict[str, str] = {
    "map_core": "map_core",
    "map-business-backend": "map-business-backend",
    "map-observability-backend": "map-observability/map-observability-backend",
}

MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_LAYER_MEMBER_BYTES = 2 * 1024 * 1024


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=check,
    )


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


def _allowed_hit(value: str) -> bool:
    if "<" in value and ">" in value:
        # Documentation placeholders like <local-dev-password> are not secrets.
        return True
    lowered = value.lower()
    return any(allowed in lowered for allowed in _ALLOWED_LITERALS)


def scan_text(text: str, location: str, hits: list[Hit]) -> None:
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                if _allowed_hit(value):
                    continue
                hits.append(Hit(location, line_no, pattern_name, value))


def scan_file_bytes(data: bytes, location: str, hits: list[Hit]) -> bool:
    if _is_binary(data) or len(data) > MAX_TEXT_FILE_BYTES:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:
            return False
    scan_text(text, location, hits)
    return True


def _is_exempt(relpath: str) -> bool:
    return relpath.replace(os.sep, "/") in EXEMPT_FILES


def scope_tree(hits: list[Hit]) -> None:
    proc = _git("ls-files", "-z")
    for raw in proc.stdout.split("\x00"):
        rel = raw.strip()
        if not rel or _is_exempt(rel):
            continue
        path = ROOT / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        scan_file_bytes(data, "tree:" + rel, hits)


def scope_index(hits: list[Hit]) -> None:
    proc = _git("ls-files", "-z")
    for raw in proc.stdout.split("\x00"):
        rel = raw.strip()
        if not rel or _is_exempt(rel):
            continue
        blob = _git("show", ":" + rel)
        if blob.returncode != 0:
            continue
        scan_file_bytes(blob.stdout.encode("utf-8"), "index:" + rel, hits)


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


def scope_build_context(hits: list[Hit]) -> None:
    for name, rel in BUILD_CONTEXTS.items():
        context = ROOT / rel
        if not context.is_dir():
            raise RuntimeError("build context %s missing: %s" % (name, context))
        patterns = _dockerignore_patterns(context)
        for path in sorted(p for p in context.rglob("*") if p.is_file()):
            relpath = path.relative_to(context).as_posix()
            if _dockerignore_denies(patterns, relpath):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            scan_file_bytes(data, "build-context:%s/%s" % (name, relpath), hits)


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


def _scan_image_tarball(name: str, raw: bytes, hits: list[Hit]) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as outer:
        for member in outer.getmembers():
            if not member.isfile():
                continue
            if member.name.endswith("manifest.json") or member.name.endswith(".json"):
                payload = outer.extractfile(member)
                if payload is None:
                    continue
                data = payload.read(MAX_TEXT_FILE_BYTES + 1)
                scan_file_bytes(data, "image:%s#config" % name, hits)
            if member.name.endswith("layer.tar"):
                payload = outer.extractfile(member)
                if payload is None:
                    continue
                with tarfile.open(fileobj=payload, mode="r:") as layer:
                    for inner in layer.getmembers():
                        if not inner.isfile() or inner.size > MAX_IMAGE_LAYER_MEMBER_BYTES:
                            continue
                        fp = layer.extractfile(inner)
                        if fp is None:
                            continue
                        data = fp.read(MAX_IMAGE_LAYER_MEMBER_BYTES + 1)
                        scan_file_bytes(data, "image:%s/%s" % (name, inner.name), hits)


def scope_image(
    hits: list[Hit],
    *,
    build: bool,
    skip_unavailable: bool,
    image_tags: dict[str, str] | None,
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
        save = subprocess.run(["docker", "save", tag], capture_output=True)
        if save.returncode != 0:
            raise RuntimeError("docker save %s failed" % tag)
        _scan_image_tarball(name, save.stdout, hits)


def build_report(
    hits: list[Hit], scopes: list[str], exempt: dict[str, dict[str, str]]
) -> dict[str, Any]:
    return {
        "scopes": scopes,
        "hits": [hit.to_dict() for hit in hits],
        "hit_count": len(hits),
        "exempt_files": {
            path: {"reason": info["reason"], "owner": info["owner"]}
            for path, info in exempt.items()
        },
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
    try:
        for scope in scopes:
            if scope == "tree":
                scope_tree(hits)
            elif scope == "index":
                scope_index(hits)
            elif scope == "build-context":
                scope_build_context(hits)
            elif scope == "image":
                scope_image(
                    hits,
                    build=args.build_image,
                    skip_unavailable=args.skip_unavailable,
                    image_tags=None,
                )
    except RuntimeError as exc:
        print("security scan error: %s" % exc, file=sys.stderr)
        return 2

    report = build_report(hits, scopes, EXEMPT_FILES)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for hit in hits:
            print("%s:%d: %s %s" % (hit.location, hit.line, hit.pattern, hit.fingerprint))
        print("security scan: %d hit(s) in %s" % (len(hits), ",".join(scopes)))
        for path, info in sorted(EXEMPT_FILES.items()):
            print("security scan: exempt %s (%s; owner=%s)"
                  % (path, info["reason"], info["owner"]))

    if args.fail_on_hit and hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
