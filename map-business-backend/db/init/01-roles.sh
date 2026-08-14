#!/bin/bash
# FIX-P1-DEPLOY-01 / R2-P1-04 / R3-P2-03: three-role separation
# (fresh volumes only). Runs once via docker-entrypoint-initdb.d when the
# PostgreSQL volume is first created, as the bootstrap superuser
# ($POSTGRES_USER). Existing volumes are left untouched.
#
# Separation of duties:
#   $POSTGRES_USER (map_admin) - bootstrap/admin only; never used by services
#   map_migrator               - owns map_control schema, runs Alembic DDL
#   map                        - business/app role: DML only, NEVER superuser
#
# Passwords are injected via environment references (compose interpolation).
# P0-SEC-01: no repository defaults — the script fails fast when a password
# variable is unset (see .env.example for local profile values).
#
# R3-P2-03 injection safety: role names are validated as simple identifiers
# and interpolated ONLY through format('%I'); passwords travel through psql
# :variables (staged via set_config OUTSIDE the dollar-quoted block, because
# psql does not interpolate :vars inside $...$ quotes) and are interpolated
# ONLY through format('%L'). Real production secrets containing quotes,
# spaces or $ signs therefore cannot break or escape the SQL. Failures
# never print secret values.
set -euo pipefail

APP_USER="${MAP_POSTGRES_APP_USER:-map}"
MIGRATOR_USER="${MAP_POSTGRES_MIGRATOR_USER:-map_migrator}"

# Fail closed on non-simple role names BEFORE any secret handling or SQL
# runs; the message intentionally contains no secret material.
valid_role_name() {
    [[ "$1" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]
}
for role_name in "$APP_USER" "$MIGRATOR_USER"; do
    if ! valid_role_name "$role_name"; then
        echo "ERROR: role name '${role_name}' must match [a-z_][a-z0-9_]{0,62}" >&2
        exit 1
    fi
done

# P0-SEC-01: passwords have no repository defaults — fail fast when unset.
APP_PASSWORD="${MAP_POSTGRES_APP_PASSWORD:?MAP_POSTGRES_APP_PASSWORD is required}"
MIGRATOR_PASSWORD="${MAP_POSTGRES_MIGRATOR_PASSWORD:?MAP_POSTGRES_MIGRATOR_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_user="$APP_USER" \
    -v app_password="$APP_PASSWORD" \
    -v migrator_user="$MIGRATOR_USER" \
    -v migrator_password="$MIGRATOR_PASSWORD" \
    -v pg_db="$POSTGRES_DB" <<'SQL'
-- psql does not interpolate :vars inside dollar-quoted bodies, so stage the
-- values through set_config first (plain quoted context, fully interpolated).
SELECT set_config('map.init.app_user', :'app_user', false),
       set_config('map.init.app_password', :'app_password', false),
       set_config('map.init.migrator_user', :'migrator_user', false),
       set_config('map.init.migrator_password', :'migrator_password', false),
       set_config('map.init.db', :'pg_db', false);

DO $do$
DECLARE
    app_user text := current_setting('map.init.app_user');
    app_password text := current_setting('map.init.app_password');
    migrator_user text := current_setting('map.init.migrator_user');
    migrator_password text := current_setting('map.init.migrator_password');
    db_name text := current_setting('map.init.db');
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = app_user) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', app_user, app_password);
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = migrator_user) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', migrator_user, migrator_password);
    END IF;

    -- R2-P1-04: the app/migrator roles must never be superuser — assert it
    -- explicitly at the end of fresh init regardless of how the role was
    -- created.
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE', app_user);
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE', migrator_user);

    EXECUTE format('CREATE SCHEMA IF NOT EXISTS map_control AUTHORIZATION %I', migrator_user);
    EXECUTE format('GRANT USAGE ON SCHEMA map_control TO %I', app_user);
    EXECUTE format('GRANT CREATE ON SCHEMA map_control TO %I', migrator_user);
    -- Allow the migrator to run migration round-trip checks on fresh databases.
    EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', db_name, migrator_user);

    -- Regular tables: full DML for the app role. Audit tables are exempted
    -- in the audit migrations themselves (explicit REVOKE + SELECT/INSERT
    -- grants, see 9a2b3c4d5e6f_audit_append_only_grants), so append-only is
    -- enforced by grant and not merely by convention.
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA map_control '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
        migrator_user, app_user);
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA map_control '
        'GRANT USAGE, SELECT ON SEQUENCES TO %I',
        migrator_user, app_user);
END
$do$;
SQL

echo "map roles initialised: app=${APP_USER}, migrator=${MIGRATOR_USER}, bootstrap=${POSTGRES_USER}"
