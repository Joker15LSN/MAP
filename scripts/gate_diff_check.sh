#!/usr/bin/env bash
# R7-P2-02: auditable whitespace checks for the release gate.
#
# The seventh-round review proved the old gate's bare `git diff --check`
# a false green: with a clean worktree it only inspects UNCOMMITTED
# drift, so whitespace defects already committed (e.g. a blank line at
# EOF) passed every gate run, while GATE_BASELINE_SHA was recorded but
# never drove a check. This helper splits the check into two auditable
# steps and makes the baseline fail-closed.
#
# Usage (run inside the repository under test):
#   gate_diff_check.sh worktree
#       git diff --check                       # uncommitted drift only
#   gate_diff_check.sh validate <baseline>
#       baseline must be set, resolve to a commit and be an ANCESTOR of
#       HEAD (merge-base pins the range direction) — otherwise exit 3
#   gate_diff_check.sh committed <baseline>
#       validate, then git diff --check <baseline> HEAD
#
# Exit codes: 0 clean; 2 whitespace defects found; 3 invalid usage or
# invalid baseline (fail-closed — never a silently empty diff).
set -u

MODE="${1:-}"

case "$MODE" in
    worktree)
        exec git diff --check
        ;;
    validate|committed)
        BASELINE="${2:-}"
        if [ -z "$BASELINE" ]; then
            echo "[diff-check] $MODE: missing baseline SHA (set GATE_BASELINE_SHA)" >&2
            exit 3
        fi
        if ! git rev-parse --verify --quiet "${BASELINE}^{commit}" >/dev/null; then
            echo "[diff-check] $MODE: baseline '$BASELINE' does not resolve to a commit" >&2
            exit 3
        fi
        if ! git merge-base --is-ancestor "$BASELINE" HEAD; then
            echo "[diff-check] $MODE: baseline '$BASELINE' is not an ancestor of HEAD" >&2
            exit 3
        fi
        if [ "$MODE" = "validate" ]; then
            exit 0
        fi
        exec git diff --check "$BASELINE" HEAD
        ;;
    *)
        echo "[diff-check] unknown mode '$MODE' (expected worktree|validate|committed)" >&2
        exit 3
        ;;
esac
