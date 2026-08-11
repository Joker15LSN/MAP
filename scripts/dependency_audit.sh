#!/usr/bin/env bash
# R2-P2-03 / R3-P2-04 / R4-P2-02 supply-chain audit gate (release gate step).
#
# Runs pip-audit against the FROZEN runtime dependency set of the three
# Python services inside a PINNED base image (digest, not floating tag)
# with a PINNED pip-audit version, so identical code always gets identical
# audit results. Upgrades to the image digest or pip-audit version must
# land as a dedicated dependency-update commit.
#
# Allowlist (R3-P2-04 / R4-P2-02): --ignore-vuln arguments are derived
# EXCLUSIVELY from security/dependency_exceptions.json via
# load_dependency_exceptions.py, which validates that every advisory is a
# SINGLE well-formed advisory ID (CVE/GHSA/PYSEC; whitespace, control
# characters, shell metacharacters and extra tokens are rejected) and
# fails the gate (exit 2) on any EXPIRED exception. A hand-maintained
# IGNORE_ARGS list is no longer possible: every ignore corresponds 1:1 to
# a valid, unexpired exception.
#
# R4-P2-02 argument safety: allowlisted IDs are kept in a bash ARRAY and
# passed to the container as POSITIONAL ARGUMENTS of a fixed inner
# script (`"$@"`). No command string is ever assembled from the IDs, so a
# value could never break out into shell syntax — even though the loader
# already rejects anything that is not a single advisory ID.
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
# R4-P2-02: keep the allowlist as an ARRAY — never concatenate it into a
# command string. Each element becomes exactly one argv token below.
IGNORE_ARGS=()
if [ -n "$IGNORE_IDS" ]; then
    while IFS= read -r advisory; do
        IGNORE_ARGS+=("--ignore-vuln" "$advisory")
        echo "[audit] allowlisted (valid unexpired exception): $advisory"
    done <<< "$IGNORE_IDS"
else
    echo "[audit] exception register is empty — no advisory is allowlisted"
fi

# R4-P2-02: the container-side auditor is a fixed script body; every
# audit argument (including all --ignore-vuln pairs) enters it ONLY as
# positional parameters via "$@", never as interpolated shell text.
# Tool installation retries transient PyPI hiccups; the audit itself uses
# a generous service timeout. A real finding or a persistent outage still
# fails the gate — this only removes flakiness, never risk.
#
# Vulnerability data service: PyPI's own advisory database
# (``--vulnerability-service pypi``). The OSV endpoint (pip-audit's
# default) is unreachable from this network environment (verified at the
# host AND container level), so pinning OSV would turn every audit into a
# network failure. The PyPI service is served from the same pypi.org
# endpoint as package metadata and stays fail-closed: any unreachable
# service or any finding fails the gate.
AUDIT_SCRIPT='set -eu
attempt=0
until pip install -q "pip-audit==$1"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 3 ]; then
        echo "[audit] pip-audit install failed after 3 attempts" >&2
        exit 1
    fi
    sleep $((attempt * 10))
done
shift
pip-audit --version
requirements="$1"; shift
# Retry the audit itself against transient service read-timeouts (the
# vulnerability DB is remote); a genuine finding fails every attempt and
# therefore still fails the gate.
attempt=0
until pip-audit --vulnerability-service pypi -r "$requirements" --timeout 60 "$@"; do
    rc=$?
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 3 ]; then
        exit "$rc"
    fi
    echo "[audit] pip-audit attempt $attempt exited rc=$rc (transient network error?); retrying" >&2
    sleep $((attempt * 15))
done'

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
    echo "[audit] $name: auditing frozen runtime dependencies (${#IGNORE_ARGS[@]} allowlist argv tokens)"
    # R4-P2-02: assemble the full argv as an ARRAY (bash 3.2 compatible);
    # the allowlist tokens are appended as discrete elements, never
    # concatenated into a command string.
    audit_argv=(sh -c "$AUDIT_SCRIPT" sh "$PIP_AUDIT_VERSION" "/work/$name.requirements.txt")
    if [ "${#IGNORE_ARGS[@]}" -gt 0 ]; then
        audit_argv+=("${IGNORE_ARGS[@]}")
    fi
    if docker run --rm -v "$WORK:/work" "$AUDITOR_IMAGE" "${audit_argv[@]}"; then
        echo "[audit] $name: PASS (no unhandled findings)"
    else
        echo "[audit] $name: FAIL — findings above must be upgraded or registered as an exception"
        FAILED=1
    fi
done

exit "$FAILED"
