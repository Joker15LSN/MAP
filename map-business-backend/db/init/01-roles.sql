-- FIX-P1-DEPLOY-01: dedicated migration role (fresh volumes only).
-- Runs once via docker-entrypoint-initdb.d when the PostgreSQL volume is
-- first created. Idempotent: existing volumes are left untouched.
--
-- Separation of duties:
--   map_migrator  - owns map_control schema, runs Alembic DDL
--   map           - business role: DML on tables (no DDL)
-- Default privileges make every table created by map_migrator readable/
-- writable by map; the audit tables get stricter grants in FIX-P1-AUDIT-01.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'map_migrator') THEN
        CREATE ROLE map_migrator LOGIN PASSWORD 'map';
    END IF;
END
$$;

-- Create the schema up-front so ALTER DEFAULT PRIVILEGES below has a target.
CREATE SCHEMA IF NOT EXISTS map_control AUTHORIZATION map_migrator;

ALTER DEFAULT PRIVILEGES FOR ROLE map_migrator IN SCHEMA map_control
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO map;
ALTER DEFAULT PRIVILEGES FOR ROLE map_migrator IN SCHEMA map_control
    GRANT USAGE, SELECT ON SEQUENCES TO map;
