"""Declarative base for the map_control schema.

F-03: all product tables (workspaces, users, jobs, outbox_events,
idempotency_records, ...) live in the dedicated ``map_control`` schema so
they never collide with map_core runtime tables in the same instance.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from . import MAP_CONTROL_SCHEMA


class Base(DeclarativeBase):
    metadata = MetaData(schema=MAP_CONTROL_SCHEMA)
