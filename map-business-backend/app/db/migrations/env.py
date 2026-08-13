"""Alembic environment for the map_control schema (async).

Migrations run with a separate role/DSN (``MAP_CONTROL_MIGRATION_DSN``,
falls back to ``MAP_CONTROL_DB_DSN``). The schema is created by the first
migration.

NOTE: do not execute any statement on the connection before Alembic's
``begin_transaction()`` — SQLAlchemy 2.0 autobegin would open a transaction
that Alembic then refuses to own, and the whole migration silently rolls
back on connection close. Any pre-statements must be committed explicitly.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import models so they register on Base.metadata.
from app.db import (
    MAP_CONTROL_SCHEMA,
    models,  # noqa: F401
)
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    os.getenv(
        "MAP_CONTROL_MIGRATION_DSN",
        os.getenv("MAP_CONTROL_DB_DSN", ""),
    ),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=MAP_CONTROL_SCHEMA,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Ensure the schema exists before Alembic creates its version table.
    # Commit explicitly: leaving an open (autobegun) transaction would make
    # Alembic skip transaction management and roll back all DDL on close.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {MAP_CONTROL_SCHEMA}"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema=MAP_CONTROL_SCHEMA,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
