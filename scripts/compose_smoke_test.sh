#!/usr/bin/env bash
# R-03 / S2-03 fresh-volume compose smoke test: non-default one-shot credentials.
#
# Generates random Mongo/Postgres credentials for THIS run only, asserts
# `docker compose config --quiet` parses with them, starts postgres+mongo on
# fresh named volumes under a RANDOM project name, waits for both to become
# healthy and verifies the mongo healthcheck path (mongosh ping with the
# injected credentials) from inside the container. Then tears everything
# down including volumes and asserts ZERO leftover containers/networks/
# volumes for this project.
#
# S2-03 hardening:
# - the cleanup trap is registered BEFORE `compose up`, so a failing start
#   can never leave networks/containers/volumes behind;
# - config PARSING (`--quiet`, no output) and the secret-leak check are two
#   separate steps: every line the script WOULD print is buffered first and
#   the leak assertion runs on that buffer before anything reaches the
#   terminal/log - no credential can ever enter the CI log;
# - the base compose file carries no fixed container_name, so a random
#   project is truly isolated from a running dev stack (parallel runs safe);
# - a host-port conflict gets ONE retry with fresh ports.
#
# Usage:  bash scripts/compose_smoke_test.sh
# Exit:   0 = config parse + fresh-volume startup + health checks + clean
#             teardown with zero leftovers and zero leaked secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN_ID="smoke-$(date +%s)-$$"
PROJECT="map-$RUN_ID"
export MAP_POSTGRES_ADMIN_USER="map_admin"
export MAP_POSTGRES_ADMIN_PASSWORD="$(openssl rand -hex 16)"
export MAP_POSTGRES_APP_USER="map"
export MAP_POSTGRES_APP_PASSWORD="$(openssl rand -hex 16)"
export MAP_POSTGRES_MIGRATOR_USER="map_migrator"
export MAP_POSTGRES_MIGRATOR_PASSWORD="$(openssl rand -hex 16)"
export MAP_MONGO_ROOT_USER="map"
export MAP_MONGO_ROOT_PASSWORD="$(openssl rand -hex 16)"
# External injection wins (test hooks / CI pinning); random otherwise.
export MAP_POSTGRES_PORT="${MAP_POSTGRES_PORT:-$((20000 + RANDOM % 20000))}"
export MAP_MONGO_PORT="${MAP_MONGO_PORT:-$((20000 + RANDOM % 20000))}"

# ---- output buffer: nothing reaches the terminal before the leak check ----
BUFFER=""
say() { BUFFER="$BUFFER$1"$'\n'; }
emit() {
    if [ -n "$BUFFER" ]; then
        printf '%s' "$BUFFER"
        BUFFER=""
    fi
}
say "[smoke] one-shot credentials generated (run $RUN_ID; values never printed)"

cleanup() {
    docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
}

fail_and_emit() {
    # print whatever was buffered so far (leak-checked), then fail
    emit
    exit 1
}
trap cleanup EXIT

# ---- 1. config parse (structure only; --quiet emits no config) ------------
if ! docker compose config --quiet >/dev/null 2>&1; then
    say "[smoke] FAILED: docker compose config --quiet did not return 0"
    fail_and_emit
fi
say "[smoke] docker compose config --quiet: OK"

# ---- 2. the buffered OUTPUT must never contain a credential ----------------
leak_check() {
    for SECRET_VALUE in "$MAP_POSTGRES_ADMIN_PASSWORD" "$MAP_POSTGRES_APP_PASSWORD" \
        "$MAP_POSTGRES_MIGRATOR_PASSWORD" "$MAP_MONGO_ROOT_PASSWORD"; do
        if printf "%s" "$BUFFER" | grep -qF "$SECRET_VALUE"; then
            say "[smoke] FAILED: a credential leaked into the smoke-test output"
            fail_and_emit
        fi
    done
}
leak_check
say "[smoke] output leak check (before emission): OK"

# ---- 3. fresh volumes with the generated credentials -----------------------
# The base compose file has no fixed container_name and the project name is
# random, so this run can never collide with a running dev stack.
start_stack() {
    docker compose -p "$PROJECT" up -d postgres mongo
}
if ! start_stack >/dev/null 2>&1; then
    if [ "${MAP_SMOKE_NO_RETRY:-0}" = "1" ]; then
        say "[smoke] FAILED: compose up failed (retry disabled for fault injection)"
        fail_and_emit
    fi
    # S2-03: a host-port conflict (parallel runs / dev stack) gets ONE
    # retry with fresh ports instead of failing nondeterministically.
    say "[smoke] first compose up failed; retrying once with new ports"
    export MAP_POSTGRES_PORT="$((20000 + RANDOM % 20000))"
    export MAP_MONGO_PORT="$((20000 + RANDOM % 20000))"
    start_stack >/dev/null
fi

wait_healthy() {
    local service="$1" attempts="$2"
    for _ in $(seq 1 "$attempts"); do
        status="$(docker compose -p "$PROJECT" ps --format "{{.Health}}" "$service" 2>/dev/null || true)"
        if [ "$status" = "healthy" ]; then
            return 0
        fi
        sleep 5
    done
    say "[smoke] FAILED: $service did not become healthy"
    emit
    docker compose -p "$PROJECT" ps >&2 || true
    exit 1
}

wait_healthy postgres 24
wait_healthy mongo 24

# ---- 4. health paths with the non-default credentials ---------------------
docker compose -p "$PROJECT" exec -T mongo \
    mongosh --quiet -u "$MAP_MONGO_ROOT_USER" -p "$MAP_MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin \
    --eval "db.adminCommand({ ping: 1 }).ok" | grep -q 1
say "[smoke] mongo ping with injected credentials: OK"

docker compose -p "$PROJECT" exec -T postgres \
    pg_isready -U "$MAP_POSTGRES_ADMIN_USER" -d map >/dev/null
say "[smoke] postgres ready with injected credentials: OK"

# ---- 5. teardown + zero-leftover assertion --------------------------------
docker compose -p "$PROJECT" down -v >/dev/null 2>&1
LEFTOVERS=""
for leftover in \
    "$(docker ps -a --format '{{.Names}}' | grep -F "map-$RUN_ID" || true)" \
    "$(docker network ls --format '{{.Name}}' | grep -F "map-$RUN_ID" || true)" \
    "$(docker volume ls --format '{{.Name}}' | grep -F "map-$RUN_ID" || true)"; do
    if [ -n "$leftover" ]; then
        LEFTOVERS="$LEFTOVERS $leftover"
    fi
done
if [ -n "$LEFTOVERS" ]; then
    say "[smoke] FAILED: leftover resources after teardown:$LEFTOVERS"
    fail_and_emit
fi
say "[smoke] zero leftover containers/networks/volumes: OK"

# ---- 6. final leak check on the COMPLETE output, then emit ----------------
leak_check
say "[smoke] PASS (config parse + fresh-volume startup + health checks + clean teardown)"
emit
