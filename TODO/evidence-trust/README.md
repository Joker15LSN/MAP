# Acceptance-evidence trust root (S4-03 / S5-02)

Pass evidence must have a **trusted source**, not a self-declared one. This
directory pins the IN-REPO MIRROR of the trust root; the authoritative trust
anchor lives OUTSIDE the reviewed range (protected CI).

## How it works

- `trusted_keys.json` is the committed MIRROR: expected attestation issuer,
  expected CI workflow, expected repository, the allowed protected refs, and
  the public Ed25519 key(s) allowed to sign release evidence.
- `scripts/evidence_signing.py` implements RFC 8032 Ed25519 and RFC 8785
  (JCS) canonical JSON with the standard library only, so the release gate
  verifies signatures **offline** under the system python3.
- A `pass` / `not-applicable-approved` manifest carries an `attestation`
  object whose signature covers the whole manifest (commit, command,
  environment, artifacts, assertions, producer, ...) **plus** the CI identity
  (`issuer`, `workflow`, `repository`, `git_ref`, `run_id`).
  `scripts/validate_acceptance_evidence.py` verifies that signature and
  rejects the manifest when any covered field does not match.

## S5-02: the mirror is NOT the trust anchor

The release validator (`--require-final`) refuses to trust the in-repo file
by itself:

1. it demands an externally injected anchor —
   `MAP_EVIDENCE_TRUST_DIGEST` (sha256 of the canonical trust config) or
   `MAP_EVIDENCE_TRUST_PUBLIC_KEY` — and requires the mirror to MATCH it;
2. it verifies the attestation was produced AFTER the implementation commit
   (started_at later than the freeze commit time);
3. it rejects a reviewed range (baseline..freeze) that modified
   `TODO/evidence-trust/` or `scripts/evidence_signing.py` - a range that
   rewrites its own trust root can never establish trust from itself;
4. local evidence generation NEVER attests a pass (attestation=None): the
   structure validator tolerates it, the release validator rejects it. Only
   the protected CI workflow (MAP_EVIDENCE_CI=1 + EVIDENCE_SIGNING_KEY +
   repository/git_ref/run_id) attests/re-attests pass manifests.

The current pinned digest (mirroring this file's committed content):

    MAP_EVIDENCE_TRUST_DIGEST=ece5b73755935ff34de4d9da06f922cf31753fcceec97454d2667083a62d4238

## Where the key lives

- **Public key**: committed here in `trusted_keys.json`
  (`keys.map-acceptance-evidence-2026-08.public_key`).
- **Private (signing) key**: NEVER in the workspace, git, artifacts or OCI
  layers. It exists ONLY as the GitHub repository secret
  `EVIDENCE_SIGNING_KEY` (hex, 64 chars), visible solely to the protected
  `gate-final` job. Rotating the mirror key REQUIRES rotating the secret
  and the pinned `MAP_EVIDENCE_TRUST_DIGEST` variable together.

## Provisioning after a key rotation

1. Generate a keypair somewhere OUTSIDE the repository:
   `python3 scripts/evidence_signing.py keygen --secret /path/outside/repo.key`
2. Set the GitHub Actions secret `EVIDENCE_SIGNING_KEY` to the hex secret;
   never write it to the repo.
3. Pin the new public key in `trusted_keys.json`.
4. Recompute the digest and set the GitHub Actions variable
   `MAP_EVIDENCE_TRUST_DIGEST`:
   `python3 -c "import json,hashlib; d=json.load(open('TODO/evidence-trust/trusted_keys.json')); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())"`
5. The protected CI then attests pass evidence; local runs stay unattested
   (and therefore not releasable) by design.
