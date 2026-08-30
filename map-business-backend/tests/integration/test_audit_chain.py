"""R2-P1-03 acceptance: the audit hash chain can never fork.

- 100 concurrent appends x 20 rounds end in exactly one chain: one
  genesis, every non-tail node has exactly one child, ordinals are
  contiguous, verify stays OK;
- tampering with ANY hash-relevant column locates the first broken link;
  the explicit non-hash column set is asserted to be complete;
- a second branch on a shared predecessor (and a second genesis /
  duplicate ordinal) is rejected by the database constraints.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.audit_events import verify_chain_rows
from app.core.identity import AuthMode
from app.db.models import ConfigAuditEvent
from app.db.session import get_db_session
from app.main import create_app
from app.services.config_mutation import (
    NON_HASH_RELEVANT_COLUMNS,
    append_audit_event,
    audit_record_payload,
    compute_entry_hash,
)
from app.settings import Settings

pytestmark = pytest.mark.asyncio

from conftest import (  # noqa: E402  (needs pytest path setup above)
    ADMIN_DSN,
    MIGRATION_DSN,
    seed_pg_admin_state,
)

TEST_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")

_TRUNCATE_ALL = text(
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN SELECT tablename FROM pg_tables "
    "WHERE schemaname = 'map_control' AND tablename <> 'alembic_version' LOOP "
    "EXECUTE 'TRUNCATE TABLE map_control.' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)


@pytest_asyncio.fixture
async def chain_factory(_engine, tmp_path):
    """Bounded-pool engine (keeps concurrent sessions under the compose
    max_connections=100) plus a truncated map_control schema."""
    engine = create_async_engine(TEST_DSN, pool_size=40, max_overflow=20)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # TRUNCATE requires the admin role (app role is append-only restricted).
    admin_engine = create_async_engine(ADMIN_DSN)
    async with admin_engine.begin() as conn:
        await conn.execute(_TRUNCATE_ALL)
    await admin_engine.dispose()
    yield factory
    await engine.dispose()


async def _admin_execute(sql: str, params: dict | None = None):
    """Run a statement as the admin role (tamper simulation). The app role
    has no UPDATE on audit tables — which is exactly the point."""
    engine = create_async_engine(ADMIN_DSN)
    try:
        async with engine.begin() as conn:
            return await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _append(factory, seq: int, **overrides) -> None:
    kwargs = {
        "workspace_id": None,
        "resource_type": "chain_test",
        "resource_id": f"item-{seq}",
        "action": "append",
        "actor_user_id": "chain-test",
        "actor_subject": "chain-test",
        "actor_roles": ["tester"],
        "request_id": f"chain-{seq}",
        "status": "applied",
        "failure_code": None,
        "before_hash": None,
        "after_hash": None,
        "json_patch": None,
    }
    kwargs.update(overrides)
    async with factory() as session:
        await append_audit_event(session, **kwargs)
        await session.commit()


async def _rows(factory):
    async with factory() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT id, workspace_id, resource_type, resource_id, action, "
                        "actor_user_id, actor_subject, actor_roles, request_id, status, "
                        "failure_code, before_hash, after_hash, json_patch, recovered, "
                        "error_message, prev_entry_hash, entry_hash, ordinal "
                        "FROM map_control.config_audit_events ORDER BY ordinal"
                    )
                )
            ).all()
        )


async def _verify_via_api(factory, tmp_path) -> dict:
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            default_workspace_id=str(WORKSPACE),
        ),
        store=None,
        core_client=None,
    )

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/admin/audit-events/verify")
        assert response.status_code == 200
        return response.json()


# --- 1. concurrency: 100 appends x 20 rounds -> exactly one chain ----------


def _canonical_columns() -> set[str]:
    """Hash-relevant columns defined by ``audit_record_payload``."""
    return set(
        audit_record_payload(
            workspace_id=None,
            resource_type="chain_test",
            resource_id="item",
            action="append",
            actor_user_id="chain-test",
            actor_subject="chain-test",
            actor_roles=["tester"],
            request_id="chain",
            status="applied",
            failure_code=None,
            before_hash=None,
            after_hash=None,
            json_patch=None,
            recovered=False,
            error_message=None,
        ).keys()
    )


async def test_100_concurrent_appends_20_rounds_single_chain(chain_factory, tmp_path) -> None:
    factory = chain_factory
    rounds, per_round = 20, 100
    for rnd in range(rounds):
        results = await asyncio.gather(
            *[_append(factory, rnd * per_round + i) for i in range(per_round)],
            return_exceptions=True,
        )
        assert results == [None] * per_round, results

    rows = await _rows(factory)
    total = rounds * per_round
    assert len(rows) == total

    # Exactly one genesis; ordinals contiguous and total-ordered.
    assert rows[0].prev_entry_hash == ""
    assert [row.ordinal for row in rows] == list(range(total))

    # Every non-tail node has exactly one child (linkage by entry hash).
    assert [row.prev_entry_hash for row in rows[1:]] == [row.entry_hash for row in rows[:-1]]
    assert len({row.entry_hash for row in rows}) == total

    # Head row agrees with the tail event.
    async with factory() as session:
        head = (
            await session.execute(
                text(
                    "SELECT head_ordinal, head_entry_hash "
                    "FROM map_control.config_audit_chain_head WHERE chain_id = 1"
                )
            )
        ).one()
    assert head.head_ordinal == total
    assert head.head_entry_hash == rows[-1].entry_hash

    verify = await _verify_via_api(factory, tmp_path)
    assert verify == {"ok": True, "count": total, "first_broken_at": None}


# --- 2. recovery -> immediate verify (three crash classes) ------------------


async def test_column_split_is_explicit_and_complete() -> None:
    """Every column is either hash-canonical or explicitly non-hash;
    nothing is unaccounted for."""
    columns = {column.name for column in ConfigAuditEvent.__table__.columns}
    assert columns == _canonical_columns() | NON_HASH_RELEVANT_COLUMNS


_TAMPER_SQL = {
    "recovered": "SET recovered = NOT recovered",
    "actor_roles": "SET actor_roles = '[\"tampered\"]'::jsonb",
    "json_patch": "SET json_patch = '[{\"op\": \"tamper\"}]'::jsonb",
    "workspace_id": f"SET workspace_id = '{uuid.UUID(int=0xdeadbeef)}'",
}
_JSONB_COLUMNS = {"actor_roles", "json_patch"}


def _restore_sql(column: str) -> tuple[str, object]:
    """SET clause + parameter transformer for restoring a tampered column."""
    if column in _JSONB_COLUMNS:
        return f"SET {column} = CAST(:original AS jsonb)", json.dumps
    if column == "workspace_id":
        return f"SET {column} = CAST(:original AS uuid)", lambda value: str(value)
    return f"SET {column} = :original", lambda value: value


async def _build_three_event_chain(factory) -> None:
    await _append(factory, 0, workspace_id=WORKSPACE)
    await _append(
        factory,
        1,
        workspace_id=WORKSPACE,
        status="failed",
        failure_code="STORE_WRITE_FAILED",
        before_hash="a" * 64,
    )
    await _append(
        factory,
        2,
        status="failed",
        failure_code="BAD_STATE_FILE",
        recovered=True,
        error_message="state file is corrupt: bad json at byte 3",
    )


async def test_every_hash_relevant_column_tamper_locates_first_break(
    chain_factory, tmp_path
) -> None:
    factory = chain_factory
    await _build_three_event_chain(factory)
    clean = await _verify_via_api(factory, tmp_path)
    assert clean["ok"] is True

    for column in sorted(_canonical_columns()):
        # Tamper with the ordinal-1 row only (admin role: the app role
        # cannot UPDATE audit tables at all).
        clause = _TAMPER_SQL.get(column, f"SET {column} = 'tampered'")
        original = (
            await _admin_execute(
                f"SELECT {column} FROM map_control.config_audit_events "
                "WHERE ordinal = 1"
            )
        ).scalar_one()
        await _admin_execute(
            f"UPDATE map_control.config_audit_events {clause} WHERE ordinal = 1"
        )
        broken = await _verify_via_api(factory, tmp_path)
        assert broken["ok"] is False, column
        assert broken["first_broken_at"] is not None, column
        # Restore the original value for the next iteration.
        restore_clause, transform = _restore_sql(column)
        await _admin_execute(
            f"UPDATE map_control.config_audit_events {restore_clause} WHERE ordinal = 1",
            {"original": transform(original)},
        )
    restored = await _verify_via_api(factory, tmp_path)
    assert restored["ok"] is True


async def test_non_hash_columns_never_break_verification(chain_factory, tmp_path) -> None:
    factory = chain_factory
    await _build_three_event_chain(factory)
    non_hash_mutable = sorted(NON_HASH_RELEVANT_COLUMNS - {"id", "ordinal"})
    for column in non_hash_mutable:
        clause = (
            f"SET {column} = 'tamper-{column}'"
            if column != "created_at"
            else "SET created_at = created_at + interval '1 day'"
        )
        # entry_hash/prev_entry_hash are chain position fields: tampering
        # them MUST break verification, unlike display-only columns.
        original = (
            await _admin_execute(
                f"SELECT {column} FROM map_control.config_audit_events "
                "WHERE ordinal = 1"
            )
        ).scalar_one()
        await _admin_execute(
            f"UPDATE map_control.config_audit_events {clause} WHERE ordinal = 1"
        )
        result = await _verify_via_api(factory, tmp_path)
        if column in {"entry_hash", "prev_entry_hash"}:
            assert result["ok"] is False, column
        else:
            assert result["ok"] is True, column
        restore_clause, transform = _restore_sql(column)
        await _admin_execute(
            f"UPDATE map_control.config_audit_events {restore_clause} WHERE ordinal = 1",
            {"original": transform(original)},
        )


# --- 4. DB invariants block forks -------------------------------------------


async def test_shared_predecessor_second_branch_is_rejected(chain_factory) -> None:
    factory = chain_factory
    await _append(factory, 0)  # genesis
    await _append(factory, 1)  # legitimate child of genesis
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, entry_hash, prev_entry_hash, ordinal "
                    "FROM map_control.config_audit_events ORDER BY ordinal"
                )
            )
        ).all()
    genesis, child = rows
    assert child.prev_entry_hash == genesis.entry_hash

    def _branch_values(prev_hash: str, ordinal: int) -> dict:
        record = audit_record_payload(
            workspace_id=None,
            resource_type="chain_test",
            resource_id="fork",
            action="append",
            actor_user_id="attacker",
            actor_subject=None,
            actor_roles=[],
            request_id=None,
            status="applied",
            failure_code=None,
            before_hash=None,
            after_hash=None,
            json_patch=None,
            recovered=False,
            error_message=None,
        )
        return {
            "resource_type": "chain_test",
            "resource_id": "fork",
            "action": "append",
            "actor_user_id": "attacker",
            "status": "applied",
            "recovered": False,
            "prev_entry_hash": prev_hash,
            "entry_hash": compute_entry_hash(prev_hash, record),
            "ordinal": ordinal,
        }

    insert_sql = text(
        "INSERT INTO map_control.config_audit_events "
        "(resource_type, resource_id, action, actor_user_id, status, recovered, "
        "prev_entry_hash, entry_hash, ordinal) "
        "VALUES (:resource_type, :resource_id, :action, :actor_user_id, :status, "
        ":recovered, :prev_entry_hash, :entry_hash, :ordinal)"
    )

    # A second child of the same predecessor must be rejected (UNIQUE
    # prev_entry_hash).
    async def _expect_rejected(values: dict) -> None:
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(insert_sql, values)
            await session.rollback()

    await _expect_rejected(_branch_values(genesis.entry_hash, 5))
    # A second genesis (prev_entry_hash = '') must be rejected too.
    await _expect_rejected(_branch_values("", 6))
    # A duplicate ordinal must be rejected.
    await _expect_rejected(_branch_values(child.entry_hash, 1))

    # The chain is untouched: verify passes in-process and via the API.
    rows = await _rows(factory)
    count, broken_at = verify_chain_rows(rows)
    assert (count, broken_at) == (2, None)


# --- 5. HTTP path stays fork-free end to end --------------------------------


async def test_concurrent_http_admin_writes_keep_single_chain(chain_factory, tmp_path) -> None:
    """Same guarantee through the real write path (ConfigMutationService):
    concurrent admin writes serialize on the chain head."""
    factory = chain_factory
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            default_workspace_id=str(WORKSPACE),
        ),
        store=None,
        core_client=None,
    )

    async def _override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override
    async with factory() as _seed_session:
        await seed_pg_admin_state(_seed_session)

    transport = ASGITransport(app=app)

    async def _write(client, index: int) -> int:
        payload = (await client.get("/api/admin/model-center")).json()
        payload["large_models"] = []
        response = await client.put(
            "/api/admin/model-center",
            json=payload,
            headers={"X-Request-ID": f"http-chain-{index}"},
        )
        return response.status_code

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        codes = await asyncio.gather(*[_write(client, i) for i in range(25)])
    assert set(codes) <= {200, 409, 500}  # every attempt lands an audit event

    rows = await _rows(factory)
    count, broken_at = verify_chain_rows(rows)
    assert broken_at is None
    assert count == len(rows) and count >= 25
    assert [row.prev_entry_hash for row in rows[1:]] == [row.entry_hash for row in rows[:-1]]


async def test_fresh_database_seeds_chain_head_at_zero(monkeypatch, tmp_path):
    """R2-P1-05 E2E regression: the chain-head seed must count FROM events.

    Migration ``8d1e2f3a4b5c`` used to seed ``config_audit_chain_head``
    with a bare ``count(*)`` (no FROM clause); PostgreSQL evaluates that
    over the implicit single empty row and returns 1, so every fresh
    database started with ``head_ordinal = 1`` and the FIRST real audit
    event got ordinal 1 — the verifier (which walks ordinals from 0)
    reported the chain broken at index 0. The Compose E2E caught this on
    a real fresh volume; this test replays the real migrations against a
    truly fresh database so the failure stays reproducible without it.
    """
    from alembic import command
    from alembic.config import Config

    db_name = f"map_seed_check_{uuid.uuid4().hex[:8]}"
    maintenance_dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/postgres"
    fresh_dsn = MIGRATION_DSN.rsplit("/", 1)[0] + f"/{db_name}"

    admin_engine = create_async_engine(maintenance_dsn, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            # mirror db/init/01-roles.sh on the fresh database
            await conn.execute(
                text(f'GRANT CREATE ON DATABASE "{db_name}" TO map_migrator')
            )
    finally:
        await admin_engine.dispose()

    try:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg = Config(os.path.join(project_root, "alembic.ini"))
        cfg.set_main_option(
            "script_location", os.path.join(project_root, "app/db/migrations")
        )
        monkeypatch.setenv("MAP_CONTROL_MIGRATION_DSN", fresh_dsn)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        fresh_engine = create_async_engine(fresh_dsn)
        try:
            async with fresh_engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT head_ordinal, head_entry_hash "
                            "FROM map_control.config_audit_chain_head "
                            "WHERE chain_id = 1"
                        )
                    )
                ).one()
            # genesis on an EMPTY events table: ordinal 0, empty prev hash
            assert int(row.head_ordinal) == 0, (
                f"chain head seeded at {row.head_ordinal} on an empty events "
                "table; the seed must count FROM config_audit_events"
            )
            assert row.head_entry_hash == ""

            # the first real event must take ordinal 0 and verify clean
            factory = async_sessionmaker(
                fresh_engine, class_=AsyncSession, expire_on_commit=False
            )
            await _append_with_factory(factory, 0)
            async with factory() as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT ordinal, prev_entry_hash, entry_hash "
                                "FROM map_control.config_audit_events "
                                "ORDER BY ordinal"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert [dict(r)["ordinal"] for r in rows] == [0]
        finally:
            await fresh_engine.dispose()
    finally:
        drop_engine = create_async_engine(maintenance_dsn, isolation_level="AUTOCOMMIT")
        try:
            async with drop_engine.begin() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            await drop_engine.dispose()


async def _append_with_factory(factory, seq: int) -> None:
    async with factory() as session:
        await append_audit_event(
            session,
            workspace_id=None,
            resource_type="seed_check",
            resource_id=f"item-{seq}",
            action="append",
            actor_user_id="seed-check",
            actor_subject="seed-check",
            actor_roles=["tester"],
            request_id=f"seed-{seq}",
            status="applied",
            failure_code=None,
            before_hash=None,
            after_hash=None,
            json_patch=None,
        )
        await session.commit()
