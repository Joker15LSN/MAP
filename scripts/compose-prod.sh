#!/usr/bin/env bash
# S4-06: production Compose entrypoint (fail-closed).
#
# Usage: MAP_ENV=prod scripts/compose-prod.sh [compose args...]
# e.g.   MAP_ENV=prod scripts/compose-prod.sh up -d
#        MAP_ENV=prod scripts/compose-prod.sh config
#
# Refuses to run unless MAP_ENV is exactly "prod", so a missing, dev, or
# unknown environment signal can never reach a production deployment.
# docker-compose.prod.yml independently enforces the same signal with
# ${MAP_ENV:?} (fails when unset) for defense in depth.
set -euo pipefail

if [ "${MAP_ENV:-}" != "prod" ]; then
  echo "error: MAP_ENV must be set to "prod" for production deployments (got "${MAP_ENV:-}")" >&2
  exit 1
fi

exec docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
