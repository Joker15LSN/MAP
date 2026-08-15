#!/usr/bin/env python3
"""Trusted-source signing for acceptance evidence (review S4-03).

This module is the single implementation of the "trusted source" rule: pass
evidence must be produced by a trusted CI workflow and carry an offline-
verifiable Ed25519 signature over a canonical statement that binds the
manifest's commit, command, environment, artifacts, assertions, producer and
the attestation's issuer/workflow. Free-text 'producer' is descriptive only
and NEVER forms a release pass by itself - the validator derives trust from
the signature + the public key pinned in TODO/evidence-trust/trusted_keys.json.

Design goals:

- Pure standard library. The release gate runs under the system python3 (see
  scripts/release_gate.sh) and must not depend on a third-party crypto install,
  so Ed25519 (RFC 8032) and RFC 8785 (JCS) canonical JSON are implemented here
  with only hashlib, json and big integers.
- Signatures are over a canonical statement, not a loose concatenation of
  fields, so no field can be reordered or reinterpreted to change meaning.
- The signing key is never committed. It lives at a git-ignored path locally
  (tmp/evidence-signing-key/ed25519.key) and, in CI, is injected from a
  repository secret (EVIDENCE_SIGNING_KEY). Only the public key is committed,
  in TODO/evidence-trust/trusted_keys.json.

Statement signed by the workflow (deterministic, JSON object):

    {
        "type": "map-acceptance-evidence/v1",
        "issuer": "<expected issuer, e.g. map-release-evidence-ci>",
        "workflow": "<expected workflow, e.g. release-gate/gate-final>",
        "repository": "<expected repository, e.g. owner/repo>",
        "git_ref": "<protected branch ref, e.g. refs/heads/main>",
        "run_id": "<CI run id - never reused>",
        "manifest": { <the manifest dict WITHOUT its "attestation" field> }
    }

The manifest then stores, under 'attestation', the type/issuer/workflow plus
repository/git_ref/run_id plus 'signatures' (key_id/algorithm/signature).
The validator reconstructs the statement from the actual manifest bytes and
verifies the signature offline.

S5-02 trust model:

- the CI identity (repository/git_ref/run_id) is PART of the signed
  statement, so a signature produced anywhere but the pinned protected
  workflow (right repository, allowed ref, fresh run) never verifies;
- the trusted public key pinned in TODO/evidence-trust/trusted_keys.json is
  only a MIRROR: the release validator demands an externally injected
  anchor (MAP_EVIDENCE_TRUST_DIGEST / MAP_EVIDENCE_TRUST_PUBLIC_KEY) that
  must match the mirror, so a developer who swaps the trust root inside the
  same implementation commit can never self-establish trust;
- local signing keys therefore can never produce a releasable pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_B = 256


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _inv(121666)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * _inv(5)
_BX = _xrecover(_BY)
_BASE = (_BX % _Q, _BY % _Q)


def _edwards_add(p, q):
    x1, y1 = p
    x2, y2 = q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2) % _Q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2) % _Q
    return (x3, y3)


def _scalarmult(p, e):
    if e == 0:
        return (0, 1)
    q = _scalarmult(p, e // 2)
    q = _edwards_add(q, q)
    if e & 1:
        q = _edwards_add(q, p)
    return q


def _encode_int(y):
    bits = [(y >> i) & 1 for i in range(_B)]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_B // 8)
    )


def _encode_point(p):
    x, y = p
    bits = [(y >> i) & 1 for i in range(_B - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_B // 8)
    )


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _decode_int(s):
    return sum(2**i * _bit(s, i) for i in range(_B))


def _is_on_curve(p):
    x, y = p
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _decode_point(s):
    y = sum(2**i * _bit(s, i) for i in range(_B - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, _B - 1):
        x = _Q - x
    p = (x, y)
    if not _is_on_curve(p):
        raise ValueError("ed25519: decoded point is not on the curve")
    return p


def _hint(m):
    h = _sha512(m)
    return sum(2**i * _bit(h, i) for i in range(2 * _B))


def _secret_scalar(sk):
    h = _sha512(sk)
    a = 2 ** (_B - 2) + sum(2**i * _bit(h, i) for i in range(3, _B - 2))
    return a


def public_key_bytes(secret):
    if len(secret) != 32:
        raise ValueError("ed25519: secret key must be 32 bytes")
    return _encode_point(_scalarmult(_BASE, _secret_scalar(secret)))


def sign_bytes(message, secret, public):
    if len(secret) != 32:
        raise ValueError("ed25519: secret key must be 32 bytes")
    if len(public) != 32:
        raise ValueError("ed25519: public key must be 32 bytes")
    h = _sha512(secret)
    a = _secret_scalar(secret)
    prefix = h[32:]
    r = _hint(prefix + message)
    r_point = _scalarmult(_BASE, r)
    r_enc = _encode_point(r_point)
    k = _hint(r_enc + public + message)
    s = (r + k * a) % _L
    return r_enc + _encode_int(s)


def verify_bytes(message, signature, public):
    if len(signature) != 64:
        return False
    if len(public) != 32:
        return False
    try:
        r_point = _decode_point(signature[:32])
        a_point = _decode_point(public)
    except ValueError:
        return False
    s = _decode_int(signature[32:])
    if s >= _L:
        return False
    k = _hint(signature[:32] + public + message)
    left = _scalarmult(_BASE, s)
    right = _edwards_add(r_point, _scalarmult(a_point, k))
    return left == right


def generate_keypair():
    import os
    secret = os.urandom(32)
    return secret.hex(), public_key_bytes(secret).hex()


def _es_number(value):
    if isinstance(value, bool):
        raise TypeError("bool is not a JSON number")
    if isinstance(value, int):
        return "0" if value == 0 else str(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("non-finite numbers are not valid JSON")
    if value == 0:
        return "0"
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, exponent = text.replace("E", "e").split("e")
        sign = ""
        if exponent.startswith("+") or exponent.startswith("-"):
            sign, exponent = exponent[0], exponent[1:]
        exponent = exponent.lstrip("0") or "0"
        text = mantissa + "e" + sign + exponent
    return text


def canonical_json(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _es_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return (
            "{"
            + ",".join(
                canonical_json(key) + ":" + canonical_json(value[key])
                for key in keys
            )
            + "}"
        )
    raise TypeError("cannot canonicalize " + type(value).__name__)


ATTESTATION_TYPE = "map-acceptance-evidence/v1"
ED25519_ALGORITHM = "ed25519"


def build_statement(manifest, attestation):
    subject = {k: v for k, v in manifest.items() if k != "attestation"}
    return {
        "type": attestation.get("type"),
        "issuer": attestation.get("issuer"),
        "workflow": attestation.get("workflow"),
        "repository": attestation.get("repository"),
        "git_ref": attestation.get("git_ref"),
        "run_id": attestation.get("run_id"),
        "run_attempt": attestation.get("run_attempt"),
        "issued_at": attestation.get("issued_at"),
        "manifest": subject,
    }


def statement_bytes(statement):
    return canonical_json(statement).encode("utf-8")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sign_manifest(
    manifest,
    secret_hex,
    *,
    issuer,
    workflow,
    key_id,
    repository,
    git_ref,
    run_id,
    run_attempt=None,
    issued_at=None,
):
    """Attach a CI-bound attestation to one manifest.

    S5-02/S6-04: repository/git_ref/run_id are REQUIRED and become part of
    the signed statement; run_attempt and issued_at (the signing time) are
    included too, so a REPLAY of an older CI run is rejected by exact
    comparison with the externally injected expected run identity.
    """
    if not repository or not git_ref or not run_id:
        raise ValueError(
            "repository, git_ref and run_id are required to sign "
            "acceptance evidence (S5-02 CI-bound attestation)"
        )
    if issued_at is None:
        issued_at = _now_iso()
    secret = bytes.fromhex(secret_hex)
    public = public_key_bytes(secret)
    meta = {
        "type": ATTESTATION_TYPE,
        "issuer": issuer,
        "workflow": workflow,
        "repository": repository,
        "git_ref": git_ref,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "issued_at": issued_at,
    }
    attestation = {
        **meta,
        "signatures": [
            {
                "key_id": key_id,
                "algorithm": ED25519_ALGORITHM,
                "signature": sign_bytes(
                    statement_bytes(build_statement(manifest, meta)),
                    secret,
                    public,
                ).hex(),
            }
        ],
    }
    result = dict(manifest)
    result["attestation"] = attestation
    return result


def _ref_matches(git_ref: str, allowed_refs: list[str]) -> bool:
    """A ref matches when it equals an allowed ref or an allowed glob."""
    import fnmatch

    return any(fnmatch.fnmatchcase(git_ref, pattern) for pattern in allowed_refs)


def verify_attestation(
    manifest,
    *,
    trusted_keys,
    expected_issuer,
    expected_workflow,
    expected_repository,
    allowed_refs,
    expected_run_id=None,
    expected_run_attempt=None,
    now=None,
):
    attestation = manifest.get("attestation")
    if not isinstance(attestation, dict):
        return ["missing attestation object"]

    problems = []
    if attestation.get("type") != ATTESTATION_TYPE:
        problems.append(
            "attestation type " + repr(attestation.get("type"))
            + " != " + repr(ATTESTATION_TYPE)
        )
    if attestation.get("issuer") != expected_issuer:
        problems.append(
            "attestation issuer " + repr(attestation.get("issuer"))
            + " != expected " + repr(expected_issuer)
        )
    if attestation.get("workflow") != expected_workflow:
        problems.append(
            "attestation workflow " + repr(attestation.get("workflow"))
            + " != expected " + repr(expected_workflow)
        )
    # S5-02: the CI identity is part of the signed statement - an
    # attestation minted on the wrong repository / unprotected ref / without
    # a fresh run id never verifies.
    if attestation.get("repository") != expected_repository:
        problems.append(
            "attestation repository " + repr(attestation.get("repository"))
            + " != expected " + repr(expected_repository)
        )
    git_ref = attestation.get("git_ref")
    if not git_ref or not _ref_matches(str(git_ref), allowed_refs):
        problems.append(
            "attestation git_ref " + repr(git_ref)
            + " is not an allowed protected ref " + repr(allowed_refs)
        )
    if not attestation.get("run_id"):
        problems.append("attestation run_id is missing")
    # S6-04: when the protected CI injects the EXPECTED run identity, the
    # attestation must match it EXACTLY - replaying a manifest signed by an
    # older run (or an older run attempt) can never pass the release gate.
    if expected_run_id is not None and attestation.get("run_id") != expected_run_id:
        problems.append(
            "attestation run_id " + repr(attestation.get("run_id"))
            + " != expected protected-CI run " + repr(expected_run_id)
        )
    if expected_run_attempt is not None and str(
        attestation.get("run_attempt") or ""
    ) != str(expected_run_attempt):
        problems.append(
            "attestation run_attempt " + repr(attestation.get("run_attempt"))
            + " != expected protected-CI attempt " + repr(expected_run_attempt)
        )
    # S6-04: the signed statement carries the ISSUING TIME; a missing,
    # malformed or future issued_at (beyond a small clock-skew window) is
    # rejected.
    issued_at_raw = attestation.get("issued_at")
    if not issued_at_raw:
        problems.append("attestation issued_at is missing")
    elif now is not None:
        try:
            from datetime import datetime, timedelta, timezone

            issued = datetime.fromisoformat(str(issued_at_raw).replace("Z", "+00:00"))
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            skew = timedelta(minutes=5)
            if issued > now + skew:
                problems.append(
                    "attestation issued_at " + repr(issued_at_raw)
                    + " is in the future (clock-skew window exceeded)"
                )
        except ValueError:
            problems.append(
                "attestation issued_at " + repr(issued_at_raw) + " is malformed"
            )

    signatures = attestation.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        problems.append("attestation has no signatures")
        return problems

    statement = build_statement(manifest, attestation)
    message = statement_bytes(statement)
    valid = False
    for sig in signatures:
        if not isinstance(sig, dict):
            problems.append("attestation signature entry is not an object")
            continue
        key_id = sig.get("key_id")
        algorithm = sig.get("algorithm")
        signature_hex = sig.get("signature")
        if algorithm != ED25519_ALGORITHM:
            problems.append("unsupported signature algorithm " + repr(algorithm))
            continue
        public_hex = trusted_keys.get(str(key_id))
        if public_hex is None:
            problems.append(
                "attestation key_id " + repr(key_id) + " is not a trusted key"
            )
            continue
        try:
            public = bytes.fromhex(public_hex)
            signature = bytes.fromhex(str(signature_hex))
        except (ValueError, TypeError):
            problems.append(
                "attestation key/signature for " + repr(key_id) + " is malformed"
            )
            continue
        if len(public) != 32 or len(signature) != 64:
            problems.append(
                "attestation key/signature for " + repr(key_id) + " has wrong length"
            )
            continue
        if verify_bytes(message, signature, public):
            valid = True
    if not valid:
        problems.append("attestation signature verification failed")
    return problems


TRUST_CONFIG_DEFAULT = "TODO/evidence-trust/trusted_keys.json"


def load_trust_config(root, path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("trust config must be a JSON object")
    for field in (
        "expected_issuer",
        "expected_workflow",
        "expected_repository",
        "allowed_refs",
        "keys",
    ):
        if field not in data:
            raise ValueError("trust config missing " + repr(field))
    keys = data["keys"]
    if not isinstance(keys, dict) or not keys:
        raise ValueError("trust config must pin at least one key")
    allowed_refs = data["allowed_refs"]
    if (
        not isinstance(allowed_refs, list)
        or not allowed_refs
        or any(not isinstance(ref, str) or not ref for ref in allowed_refs)
    ):
        raise ValueError(
            "trust config allowed_refs must be a non-empty list of ref patterns"
        )
    return data


def trust_config_digest(data: dict) -> str:
    """S5-02 external anchor: the pinned sha256 of the canonical trust config.

    Protected CI injects this digest (or the public key itself); the
    validator computes the SAME digest over the in-repo mirror and requires
    equality, so the trust root can no longer be rewritten inside the
    reviewed range.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_secret(path):
    return path.read_text(encoding="utf-8").strip()


def save_secret(path, secret_hex):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret_hex + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _cli_keygen(args):
    secret_hex, public_hex = generate_keypair()
    save_secret(Path(args.secret), secret_hex)
    print("secret  -> " + args.secret + " (git-ignored, chmod 600)")
    print("public  -> " + public_hex)
    print("Add this public key (hex) to TODO/evidence-trust/trusted_keys.json")
    return 0


def _cli_sign(args):
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    secret_hex = load_secret(Path(args.secret))
    signed = sign_manifest(
        manifest,
        secret_hex,
        issuer=args.issuer,
        workflow=args.workflow,
        key_id=args.key_id,
        repository=args.repository,
        git_ref=args.git_ref,
        run_id=args.run_id,
    )
    out = Path(args.out) if args.out else manifest_path
    out.write_text(
        json.dumps(signed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("signed -> " + str(out))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="generate a keypair")
    keygen.add_argument("--secret", default="tmp/evidence-signing-key/ed25519.key")
    keygen.set_defaults(func=_cli_keygen)

    sign = sub.add_parser("sign", help="attach an attestation to one manifest")
    sign.add_argument("manifest")
    sign.add_argument("--secret", default="tmp/evidence-signing-key/ed25519.key")
    sign.add_argument("--issuer", required=True)
    sign.add_argument("--workflow", required=True)
    sign.add_argument("--key-id", required=True)
    # S5-02: the CI identity is part of the signed statement.
    sign.add_argument("--repository", required=True)
    sign.add_argument("--git-ref", required=True)
    sign.add_argument("--run-id", required=True)
    sign.add_argument("--out", default=None)
    sign.set_defaults(func=_cli_sign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
