#!/usr/bin/env bash
# R2-P2-03 / R3-P2-04 supply-chain audit gate (release gate step).
#
# Runs pip-audit against the FROZEN runtime dependency set of the three
# Python services inside a PINNED base image (digest, not floating tag)
# with a PINNED pip-audit version, so identical code always gets identical
# audit results. Upgrades to the image digest or pip-audit version must
# land as a dedicated dependency-update commit.
#
# Allowlist (R3-P2-04): --ignore-vuln arguments are derived EXCLUSIVELY
# from security/dependency_exceptions.json via load_dependency_exceptions.py,
# which validates advisory/owner/ticket/approver/dates and fails the gate
# (exit 2) on any EXPIRED exception. A hand-maintained IGNORE_ARGS list is
# no longer possible: every ignore must correspond 1:1 to a valid,
# unregistered exception.
#
# The artifact log records the audit tool version, the base image digest
# and the sha256 of every audited lockfile (R3-P2-04 evidence requirement).
#
# Usage:  bash scripts/dependency_audit.sh
# Exit:   0 = clean (or only allowlisted findings); non-zero otherwise.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# R3-P2-04: pinned auditor. Upgrade = dedicated dependency-update commit.
AUDITOR_IMAGE="python@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6"  # python:3.13-slim
PIP_AUDIT_VERSION="2.10.1"
EXCEPTIONS_FILE="$ROOT/security/dependency_exceptions.json"

echo "[audit] base image: $AUDITOR_IMAGE"
echo "[audit] pip-audit:  $PIP_AUDIT_VERSION (pinned)"
echo "[audit] exceptions: $EXCEPTIONS_FILE"

# Fail closed BEFORE any audit runs if the exception register is missing,
# malformed or contains expired entries.
IGNORE_IDS="$(python3 "$ROOT/scripts/load_dependency_exceptions.py" "$EXCEPTIONS_FILE")" || {
    echo "[audit] FAIL — exception register rejected (expired/malformed entries fail the gate)"
    exit 1
}
IGNORE_ARGS=""
if [ -n "$IGNORE_IDS" ]; then
    while IFS= read -r advisory; do
        IGNORE_ARGS="$IGNORE_ARGS --ignore-vuln $advisory"
        echo "[audit] allowlisted (valid unexpired exception): $advisory"
    done <<< "$IGNORE_IDS"
else
    echo "[audit] exception register is empty — no advisory is allowlisted"
fi

SERVICES=(
    "map_core:map_core"
    "map-business-backend:map-business-backend"
    "map-observability/map-observability-backend:map-observability-backend"
)

FAILED=0
for entry in "${SERVICES[@]}"; do
    dir="${entry%%:*}"
    name="${entry##*:}"
    req="$WORK/$name.requirements.txt"
    (cd "$ROOT/$dir" && uv export --frozen --no-dev --no-hashes -o "$req") || {
        echo "[audit] $name: uv export FAILED"
        FAILED=1
        continue
    }
    lock_hash="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$ROOT/$dir/uv.lock")"
    echo "[audit] $name: lockfile sha256=$lock_hash"
    echo "[audit] $name: auditing frozen runtime dependencies"
    if docker run --rm -v "$WORK:/work" "$AUDITOR_IMAGE" \
        sh -c "pip install -q pip-audit==$PIP_AUDIT_VERSION \
            && pip-audit --version \
            && pip-audit -r /work/$name.requirements.txt$IGNORE_ARGS"; then
        echo "[audit] $name: PASS (no unhandled findings)"
    else
        echo "[audit] $name: FAIL — findings above must be upgraded or registered as an exception"
        FAILED=1
    fi
done

exit "$FAILED"
