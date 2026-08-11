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
#
# R4-P2-03 self-contained evidence: at startup the gate records the git
# SHA, tree hash, branch, dirty state, optional baseline SHA and UTC time;
# every step records its exit code, log sha256 and UTC timestamps; the
# machine-readable summary lands in tmp/gate-logs/gate-summary.json.
# FINAL mode (RELEASE_GATE_FINAL=1) refuses dirty PRODUCT code; docs-only
# drift is tolerated and stays recorded in the summary. Quality documents
# must reference the fields inside gate-summary.json, never a narration.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/tmp/gate-logs"
mkdir -p "$LOG_DIR"

FAILURES=()
STEP_TOTAL=0
STEPS_JSONL="$LOG_DIR/steps.jsonl"
SUMMARY="$LOG_DIR/gate-summary.json"
: > "$STEPS_JSONL"

# ---- R4-P2-03 / R5-P2-02: source-control self-description --------------
# The snapshot is produced by the ONE tested NUL-safe parser
# (scripts/source_control.py) shared with the E2E runner — no bash
# porcelain slicing, no core.quotePath escaping, no fixed-column cuts.
GATE_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GATE_BASELINE_SHA="${GATE_BASELINE_SHA:-}"
SOURCE_CONTROL_JSON="$LOG_DIR/source-control.json"
if ! python3 "$ROOT/scripts/source_control.py" --repo "$ROOT" --json > "$SOURCE_CONTROL_JSON"; then
    echo "[gate] RELEASE GATE FAILED — source-control snapshot could not be produced"
    exit 1
fi

DIRTY_FILES=()
DIRTY_PRODUCT=()
while IFS= read -r path; do
    [ -n "$path" ] || continue
    DIRTY_FILES+=("$path")
done < <(python3 -c 'import json,sys; [print(p) for p in json.load(open(sys.argv[1]))["dirty_files"]]' "$SOURCE_CONTROL_JSON")
while IFS= read -r path; do
    [ -n "$path" ] || continue
    DIRTY_PRODUCT+=("$path")
done < <(python3 -c 'import json,sys; [print(p) for p in json.load(open(sys.argv[1]))["dirty_product"]]' "$SOURCE_CONTROL_JSON")
GIT_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["git_sha"] or "unknown")' "$SOURCE_CONTROL_JSON")"
GIT_TREE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["git_tree"] or "unknown")' "$SOURCE_CONTROL_JSON")"
GIT_BRANCH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["branch"] or "unknown")' "$SOURCE_CONTROL_JSON")"

FINAL_MODE=0
if [ "${RELEASE_GATE_FINAL:-0}" = "1" ]; then
    FINAL_MODE=1
    if [ "${#DIRTY_PRODUCT[@]}" -gt 0 ]; then
        echo "[gate] RELEASE GATE FAILED — final mode refuses dirty product code:"
        for path in "${DIRTY_PRODUCT[@]}"; do
            echo "  - $path"
        done
        echo "[gate] commit the product changes (or rerun without RELEASE_GATE_FINAL=1)"
        exit 1
    fi
    if [ "${#DIRTY_FILES[@]}" -gt 0 ]; then
        echo "[gate] final mode: docs-only dirtiness tolerated (${#DIRTY_FILES[@]} file(s))"
    fi
fi

echo "[gate] log dir: $LOG_DIR"
echo "[gate] source control: sha=$GIT_SHA tree=$GIT_TREE branch=$GIT_BRANCH dirty=${#DIRTY_FILES[@]} product_dirty=${#DIRTY_PRODUCT[@]}"
if [ -n "$GATE_BASELINE_SHA" ]; then
    echo "[gate] baseline sha: $GATE_BASELINE_SHA"
fi

# run <step-name> <workdir> <command...>
run() {
    local name="$1" dir="$2"
    shift 2
    local log="$LOG_DIR/$name.log"
    local started_utc ended_utc log_sha256 rc
    STEP_TOTAL=$((STEP_TOTAL + 1))
    started_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "[gate] $name :: (cd $dir && $*)"
    (cd "$dir" && "$@") >"$log" 2>&1
    rc=$?
    ended_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log_sha256="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$log")"
    # R4-P2-03: one machine-readable record per step (name/command/exit/
    # log sha256/UTC times) — assembled into gate-summary.json at the end.
    STEP_NAME="$name" STEP_EXIT="$rc" STEP_LOG="$log" STEP_SHA="$log_sha256" \
        STEP_START="$started_utc" STEP_END="$ended_utc" STEP_DIR="$dir" \
        STEP_CMD="$*" python3 -c '
import json, os
print(json.dumps({
    "step": os.environ["STEP_NAME"],
    "command": os.environ["STEP_CMD"],
    "workdir": os.environ["STEP_DIR"],
    "exit_code": int(os.environ["STEP_EXIT"]),
    "log": os.environ["STEP_LOG"],
    "log_sha256": os.environ["STEP_SHA"],
    "started_utc": os.environ["STEP_START"],
    "finished_utc": os.environ["STEP_END"],
}, ensure_ascii=False))' >> "$STEPS_JSONL"
    echo "[gate] $name exit=$rc artifact=$log sha256=$log_sha256"
    if [ "$rc" -ne 0 ]; then
        FAILURES+=("$name (exit=$rc, artifact=$log)")
    fi
}

# ---- R4-P2-01: the browser-event hygiene gate must fail closed; its
#      failure-reproduction self-test runs WITHOUT any stack.
run browser-e2e-self-test "$ROOT" python3 e2e/browser_e2e.py --self-test

# ---- Repo hygiene (R3-P2-01 unified standard commands)
run diff-check "$ROOT" git diff --check
run compose-config "$ROOT" docker compose config --quiet

# ---- Python backends: frozen sync, then the unified ruff + pytest commands
run bff-deps "$ROOT/map-business-backend" uv sync --frozen
run bff-lint "$ROOT/map-business-backend" uv run ruff check app tests
# R5-P1-01: unawaited-coroutine / unraisable warnings are ERRORS in the gate.
# A coroutine predicate called without await used to make a 30s cleanup wait
# vacuously true while the leaked RuntimeWarning was swallowed — the suite
# went green on a skipped check. These filters make that shape fail loudly.
run bff-test "$ROOT/map-business-backend" uv run pytest \
  -W error::RuntimeWarning \
  -W error::pytest.PytestUnraisableExceptionWarning

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

# ---- R4-P2-03 / R5-P2-02: machine-readable summary artifact (self-contained
#      SHA proof). source_control comes VERBATIM from the shared snapshot
#      artifact (tmp/gate-logs/source-control.json) — same bytes the E2E
#      runner records, so gate and E2E can never classify differently.
GATE_END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GATE_START_UTC="$GATE_START_UTC" GATE_END_UTC="$GATE_END_UTC" \
    GATE_BASELINE_SHA="$GATE_BASELINE_SHA" FINAL_MODE="$FINAL_MODE" \
    SUMMARY="$SUMMARY" STEPS_JSONL="$STEPS_JSONL" \
    SOURCE_CONTROL_JSON="$SOURCE_CONTROL_JSON" \
    GATE_FAILED="${#FAILURES[@]}" GATE_STEPS="$STEP_TOTAL" python3 -c '
import json, os
with open(os.environ["SOURCE_CONTROL_JSON"], encoding="utf-8") as fh:
    source_control = json.load(fh)
source_control["baseline_sha"] = os.environ.get("GATE_BASELINE_SHA") or None
steps = []
with open(os.environ["STEPS_JSONL"], encoding="utf-8") as fh:
    for line in fh:
        if line.strip():
            steps.append(json.loads(line))
summary = {
    "source_control": source_control,
    "final_mode": os.environ["FINAL_MODE"] == "1",
    "started_utc": os.environ["GATE_START_UTC"],
    "finished_utc": os.environ["GATE_END_UTC"],
    "steps_total": int(os.environ["GATE_STEPS"]),
    "steps_failed": int(os.environ["GATE_FAILED"]),
    "result": "PASS" if os.environ["GATE_FAILED"] == "0" else "FAIL",
    "steps": steps,
}
with open(os.environ["SUMMARY"], "w", encoding="utf-8") as fh:
    json.dump(summary, fh, ensure_ascii=False, indent=2)
'
echo "[gate] summary artifact: $SUMMARY (sha=$GIT_SHA tree=$GIT_TREE)"

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
