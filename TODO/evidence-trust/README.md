# Acceptance-evidence trust root (S4-03)

Pass evidence must have a **trusted source**, not a self-declared one. This
directory pins the trust root that makes that true.

## How it works

- `trusted_keys.json` is the committed trust root: the expected attestation
  issuer, the expected CI workflow, and the public Ed25519 key(s) that are
  allowed to sign release evidence.
- `scripts/evidence_signing.py` implements RFC 8032 Ed25519 and RFC 8785
  (JCS) canonical JSON with the standard library only, so the release gate can
  verify signatures **offline** under the system python3 (no crypto install).
- A `pass` / `not-applicable-approved` manifest carries an `attestation`
  object whose signature covers the whole manifest (commit, command,
  environment, artifacts, assertions, producer, ...) **plus** the attestation's
  `issuer` and `workflow`. `scripts/validate_acceptance_evidence.py`
  verifies that signature against the pinned key and rejects the manifest if
  the issuer, workflow, key, or any covered field does not match.

## Where the key lives

- **Public key**: committed here in `trusted_keys.json` (see
  `keys.map-acceptance-evidence-2026-08.public_key`).
- **Private (signing) key**: NEVER committed. It lives at the git-ignored path
  `tmp/evidence-signing-key/ed25519.key` locally (chmod 600). In CI, the same
  key is injected from the repository secret `EVIDENCE_SIGNING_KEY` — the
  CI workflow is the only thing that signs release evidence.

## Signing / regenerating evidence

Generate a keypair (writes only the git-ignored private key; the public key
must then be pinned here):

    python3 scripts/evidence_signing.py keygen --secret tmp/evidence-signing-key/ed25519.key

Attach a signature to one manifest:

    python3 scripts/evidence_signing.py sign \
        tmp/acceptance/<TASK>/<sha>/<AC>/evidence-manifest.json \
        --secret tmp/evidence-signing-key/ed25519.key \
        --issuer map-release-evidence-ci \
        --workflow release-gate/gate-final \
        --key-id map-acceptance-evidence-2026-08

`scripts/generate_acceptance_evidence.py` signs every `pass` manifest it
creates automatically (pass `--signing-key` to point at the key; `--issuer`,
`--workflow`, `--key-id` default to the values pinned above).

## Rotating a key

1. Generate a new keypair (above).
2. Pin the new public key in `trusted_keys.json` under a new `key_id`.
3. Re-sign the pass manifests with the new key.
4. Once nothing references the old `key_id`, remove it from
   `trusted_keys.json`.
