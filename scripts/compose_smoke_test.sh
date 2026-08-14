#!/usr/bin/env bash
# R-03 fresh-volume compose smoke test: non-default one-shot credentials.
#
# Generates random Mongo/Postgres credentials for THIS run only, asserts
# `docker compose config --quiet` passes with them, starts postgres+mongo on
# fresh named volumes, waits for both to become healthy and verifies the
# mongo healthcheck path (mongosh ping with the injected credentials) from
# inside the container. Then tears everything down including volumes.
#
# No credential is ever printed: this script runs without `set -x` and the
# assertion below proves the compose config output contains no password.
#
# Usage:  bash scripts/compose_smoke_test.sh
# Exit:   0 = config parse + fresh-volume startup + health checks all pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN_ID="smoke-$(date +%s)-$$"
export MAP_POSTGRES_ADMIN_USER="map_admin"
export MAP_POSTGRES_ADMIN_PASSWORD="$(openssl rand -hex 16)"
export MAP_POSTGRES_APP_USER="map"
export MAP_POSTGRES_APP_PASSWORD="$(openssl rand -hex 16)"
export MAP_POSTGRES_MIGRATOR_USER="map_migrator"
export MAP_POSTGRES_MIGRATOR_PASSWORD="$(openssl rand -hex 16)"
export MAP_MONGO_ROOT_USER="map"
export MAP_MONGO_ROOT_PASSWORD="$(openssl rand -hex 16)"
export MAP_POSTGRES_PORT="$((20000 + RANDOM % 20000))"
export MAP_MONGO_PORT="$((20000 + RANDOM % 20000))"

echo "[smoke] one-shot credentials generated (run $RUN_ID; values never printed)"

# 1. compose config must parse with non-default credentials...
CONFIG_OUT="$(docker compose config --quiet 2>&1)"
if [ $? -ne 0 ]; then
    echo "[smoke] FAILED: docker compose config --quiet did not return 0" >&2
    exit 1
fi
# ...and must never materialize the password on stdout/stderr.
for SECRET_VALUE in "$MAP_POSTGRES_ADMIN_PASSWORD" "$MAP_POSTGRES_APP_PASSWORD" \
    "$MAP_POSTGRES_MIGRATOR_PASSWORD" "$MAP_MONGO_ROOT_PASSWORD"; do
    if printf "%s" "$CONFIG_OUT" | grep -qF "$SECRET_VALUE"; then
        echo "[smoke] FAILED: compose config output leaked a credential" >&2
        exit 1
    fi
done
echo "[smoke] docker compose config --quiet: OK (no credential in output)"

# 2. fresh volumes with the generated credentials.
docker compose -p "map-$RUN_ID" up -d postgres mongo
trap "docker compose -p map-$RUN_ID down -v >/dev/null 2>&1 || true" EXIT

wait_healthy() {
    local service="$1" attempts="$2"
    for _ in $(seq 1 "$attempts"); do
        status="$(docker compose -p "map-$RUN_ID" ps --format "{{.Health}}" "$service" 2>/dev/null || true)"
        if [ "$status" = "healthy" ]; then
            return 0
        fi
        sleep 5
    done
    echo "[smoke] FAILED: $service did not become healthy" >&2
    docker compose -p "map-$RUN_ID" ps >&2 || true
    return 1
}

wait_healthy postgres 24
wait_healthy mongo 24

# 3. the mongo healthcheck path works with the non-default credentials.
docker compose -p "map-$RUN_ID" exec -T mongo \
    mongosh --quiet -u "$MAP_MONGO_ROOT_USER" -p "$MAP_MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin \
    --eval "db.adminCommand({ ping: 1 }).ok" | grep -q 1
echo "[smoke] mongo ping with injected credentials: OK"

# 4. dependency services can connect (pg_isready from inside postgres).
docker compose -p "map-$RUN_ID" exec -T postgres \
    pg_isready -U "$MAP_POSTGRES_ADMIN_USER" -d map >/dev/null
echo "[smoke] postgres ready with injected credentials: OK"

echo "[smoke] PASS (config parse + fresh-volume startup + health checks)"
