#!/usr/bin/env bash
# R2-P2-02 release gate: the EXACT command set CI runs. Every step starts
# from a clean dependency resolution (`uv sync --frozen` / `npm ci`), so a
# green gate proves reproducibility on a fresh checkout — no reliance on
# locally installed tools or leftover environments.
#
# Prerequisites for a full local run:
#   - uv >= 0.5, node >= 20
#   - docker (supply-chain audit step runs pip-audit in python:3.13-slim)
#   - the dev PostgreSQL from docker-compose.yml reachable at 127.0.0.1:15432
#     (map-business-backend integration tests need the three roles created by
#     db/init/01-roles.sh: map_admin / map_migrator / map)
#
# Usage:  bash scripts/release_gate.sh
# Exit:   0 = every step green; 1 = at least one step failed. Every step's
#         full log is kept under tmp/gate-logs/ and the summary prints each
#         exit code + artifact path (required by the quality record).
#
# R3-P2-01: the step count is counted by THIS script (see the final
# "steps=" line); quality documents must reference the gate output and
# never hardcode step numbers.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/tmp/gate-logs"
mkdir -p "$LOG_DIR"

FAILURES=()
STEP_TOTAL=0

# run <step-name> <workdir> <command...>
run() {
    local name="$1" dir="$2"
    shift 2
    local log="$LOG_DIR/$name.log"
    STEP_TOTAL=$((STEP_TOTAL + 1))
    echo "[gate] $name :: (cd $dir && $*)"
    (cd "$dir" && "$@") >"$log" 2>&1
    local rc=$?
    echo "[gate] $name exit=$rc artifact=$log"
    if [ "$rc" -ne 0 ]; then
        FAILURES+=("$name (exit=$rc, artifact=$log)")
    fi
}

echo "[gate] log dir: $LOG_DIR"

# ---- Repo hygiene (R3-P2-01 unified standard commands)
run diff-check "$ROOT" git diff --check
run compose-config "$ROOT" docker compose config --quiet

# ---- Python backends: frozen sync, then the unified ruff + pytest commands
run bff-deps "$ROOT/map-business-backend" uv sync --frozen
run bff-lint "$ROOT/map-business-backend" uv run ruff check app tests
run bff-test "$ROOT/map-business-backend" uv run pytest

run core-deps "$ROOT/map_core" uv sync --frozen
run core-lint "$ROOT/map_core" uv run ruff check map_core tests
run core-test "$ROOT/map_core" uv run pytest -q

run obs-deps "$ROOT/map-observability/map-observability-backend" uv sync --frozen
run obs-lint "$ROOT/map-observability/map-observability-backend" uv run ruff check app tests
run obs-test "$ROOT/map-observability/map-observability-backend" uv run pytest -q

# ---- Frontends: clean install, tests (must exit 0 with NO unhandled errors),
#      production build, then the bundle size gate
run biz-fe-deps "$ROOT/map-business-frontend" npm ci
run biz-fe-test "$ROOT/map-business-frontend" npm test
run biz-fe-build "$ROOT/map-business-frontend" npm run build

run obs-fe-deps "$ROOT/map-observability/map-observability-frontend" npm ci
run obs-fe-test "$ROOT/map-observability/map-observability-frontend" npm test
run obs-fe-build "$ROOT/map-observability/map-observability-frontend" npm run build

run bundle-gate "$ROOT" python3 scripts/check_bundle_size.py

# ---- Supply-chain audit (R2-P2-03): pip-audit on the frozen runtime deps of
#      the three Python services + npm audit on both frontends. Exceptions
#      must be registered (with expiry) in SECURITY_EXCEPTIONS.md.
run py-dep-audit "$ROOT" bash scripts/dependency_audit.sh
run biz-fe-audit "$ROOT/map-business-frontend" npm audit --omit=dev --audit-level=high
run obs-fe-audit "$ROOT/map-observability/map-observability-frontend" npm audit --omit=dev --audit-level=high

echo "========================================================================"
echo "[gate] steps=$STEP_TOTAL failed=${#FAILURES[@]} artifacts=$LOG_DIR"
if [ "${#FAILURES[@]}" -eq 0 ]; then
    echo "[gate] RELEASE GATE PASSED (all $STEP_TOTAL steps exit=0, artifacts in $LOG_DIR)"
    exit 0
fi
echo "[gate] RELEASE GATE FAILED:"
for failure in "${FAILURES[@]}"; do
    echo "  - $failure"
done
exit 1
