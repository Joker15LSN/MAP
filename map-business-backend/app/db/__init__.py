"""Database layer: SQLAlchemy 2.x async + asyncpg + Alembic (F-03).

All product tables live in the dedicated ``map_control`` schema inside the
shared PostgreSQL instance. BFF/worker roles must not read execution tables
outside this schema; map_core must not read these tables.
"""

from __future__ import annotations

MAP_CONTROL_SCHEMA = "map_control"
