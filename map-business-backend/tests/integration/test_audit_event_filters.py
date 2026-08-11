"""R3-P1-03 acceptance: audit-events query filters (action + time range).

The report-mandated filter set — resource, actor, action, status,
request_id and a timezone-aware inclusive time range — exercised through
the real API with the workspace predicate always in the SQL:

- single filters (action / created_from / created_to);
- combinations and boundary instants (both bounds inclusive);
- ``created_from > created_to`` and naive timestamps -> 422 envelope;
- cross-workspace rows never leak into filtered results;
- filtered totals agree with pagination.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_filter_state.json")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.services.config_mutation import append_audit_event
from app.settings import Settings

pytestmark = pytest.mark.asyncio

from conftest import ADMIN_DSN  # noqa: E402  (needs pytest path setup above)

TEST_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")

BASE = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
T_MINUS_2H = BASE - timedelta(hours=2)
T_MINUS_1H = BASE - timedelta(hours=1)

_TRUNCATE_ALL = text(
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN SELECT tablename FROM pg_tables "
    "WHERE schemaname = 'map_control' AND tablename <> 'alembic_version' LOOP "
    "EXECUTE 'TRUNCATE TABLE map_control.' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)


@pytest_asyncio.fixture
async def filter_client(_engine, tmp_path):
    """Truncated schema + seeded events + a real FastAPI test client."""
    engine = create_async_engine(TEST_DSN, pool_size=10, max_overflow=5)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    admin_engine = create_async_engine(ADMIN_DSN)
    async with admin_engine.begin() as conn:
        await conn.execute(_TRUNCATE_ALL)
    await admin_engine.dispose()

    # Seed: 3x update_model_center at T-2h/T-1h/T0 in WORKSPACE, 1x
    # update_master_agent at T-1h, 1x cross-workspace lookalike.
    seeds = [
        (WORKSPACE, "update_model_center", T_MINUS_2H, "seed-1"),
        (WORKSPACE, "update_model_center", T_MINUS_1H, "seed-2"),
        (WORKSPACE, "update_model_center", BASE, "seed-3"),
        (WORKSPACE, "update_master_agent", T_MINUS_1H, "seed-4"),
        (OTHER_WORKSPACE, "update_model_center", T_MINUS_1H, "seed-5"),
    ]
    async with factory() as session:
        for workspace, action, _created_at, request_id in seeds:
            await append_audit_event(
                session,
                workspace_id=workspace,
                resource_type="admin_config",
                resource_id="model_center",
                action=action,
                actor_user_id="alice",
                actor_subject="alice",
                actor_roles=["admin"],
                request_id=request_id,
                status="applied",
                failure_code=None,
                before_hash=None,
                after_hash=None,
                json_patch=None,
            )
        await session.commit()
    # Fix the timestamps deterministically (admin role: the app role has no
    # UPDATE on audit tables — which is exactly the point).
    admin_engine = create_async_engine(ADMIN_DSN)
    async with admin_engine.begin() as conn:
        for _workspace, _action, created_at, request_id in seeds:
            await conn.execute(
                text(
                    "UPDATE map_control.config_audit_events "
                    "SET created_at = :ts WHERE request_id = :rid"
                ),
                {"ts": created_at, "rid": request_id},
            )
    await admin_engine.dispose()

    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=str(tmp_path / "filter_state.json"),
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
        yield client
    await engine.dispose()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


async def _query(client, **params) -> dict:
    response = await client.get("/api/v1/admin/audit-events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- single filters ----------------------------------------------------------


async def test_action_filter_single(filter_client) -> None:
    body = await _query(filter_client, action="update_model_center")
    assert body["total"] == 3  # the cross-workspace lookalike never counts
    assert {item["action"] for item in body["items"]} == {"update_model_center"}
    assert {item["workspace_id"] for item in body["items"]} == {str(WORKSPACE)}

    body = await _query(filter_client, action="update_master_agent")
    assert body["total"] == 1
    assert body["items"][0]["request_id"] == "seed-4"

    body = await _query(filter_client, action="nonexistent_action")
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.parametrize(
    ("params", "expected_request_ids"),
    [
        ({"created_from": _iso(T_MINUS_1H)}, {"seed-2", "seed-3", "seed-4"}),
        ({"created_to": _iso(T_MINUS_1H)}, {"seed-1", "seed-2", "seed-4"}),
        # boundary instants are INCLUSIVE on both ends
        ({"created_from": _iso(BASE)}, {"seed-3"}),
        ({"created_to": _iso(T_MINUS_2H)}, {"seed-1"}),
        ({"created_from": _iso(T_MINUS_1H), "created_to": _iso(T_MINUS_1H)}, {"seed-2", "seed-4"}),
    ],
)
async def test_time_filters_single_and_boundaries(
    filter_client, params, expected_request_ids
) -> None:
    body = await _query(filter_client, **params)
    assert {item["request_id"] for item in body["items"]} == expected_request_ids
    assert body["total"] == len(expected_request_ids)
    for item in body["items"]:
        assert item["workspace_id"] == str(WORKSPACE)


# --- combinations --------------------------------------------------------------


async def test_action_and_time_combined(filter_client) -> None:
    body = await _query(
        filter_client,
        action="update_model_center",
        created_from=_iso(T_MINUS_1H),
        created_to=_iso(BASE),
    )
    assert {item["request_id"] for item in body["items"]} == {"seed-2", "seed-3"}
    assert body["total"] == 2

    body = await _query(
        filter_client,
        action="update_model_center",
        actor="alice",
        status="applied",
        created_from=_iso(T_MINUS_2H),
    )
    assert body["total"] == 3


# --- pagination agrees with filtered totals -----------------------------------


async def test_filtered_pagination_consistent(filter_client) -> None:
    full = await _query(filter_client, action="update_model_center")
    assert full["total"] == 3

    seen: list[str] = []
    for offset in range(full["total"]):
        page = await _query(filter_client, action="update_model_center", limit=1, offset=offset)
        assert page["total"] == full["total"]  # stable total while paging
        assert len(page["items"]) == 1
        seen.append(page["items"][0]["request_id"])
    assert sorted(seen) == sorted(item["request_id"] for item in full["items"])

    beyond = await _query(filter_client, action="update_model_center", limit=1, offset=10)
    assert beyond["total"] == 3
    assert beyond["items"] == []


# --- invalid ranges -> standard 422 envelope ----------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"created_from": _iso(BASE), "created_to": _iso(T_MINUS_2H)},
        {"created_from": "2026-08-01T12:00:00"},  # naive: no timezone
        {"created_to": "2026-08-01T12:00:00"},
        {"created_from": "not-a-timestamp"},
    ],
)
async def test_invalid_time_range_returns_standard_envelope(filter_client, params) -> None:
    response = await filter_client.get("/api/v1/admin/audit-events", params=params)
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["request_id"]
    if "not-a-timestamp" in params.values():
        assert body["code"] == "VALIDATION_ERROR"
    else:
        assert body["code"] == "INVALID_TIME_RANGE"


# --- workspace predicate survives every filter --------------------------------


async def test_cross_workspace_never_leaks_through_filters(filter_client) -> None:
    for params in (
        {"action": "update_model_center"},
        {"created_from": _iso(T_MINUS_2H)},
        {"created_to": _iso(BASE)},
        {"actor": "alice"},
        {"request_id": "seed-5"},  # the cross-workspace row's own request id
        {
            "action": "update_model_center",
            "created_from": _iso(T_MINUS_2H),
            "created_to": _iso(BASE),
        },
    ):
        body = await _query(filter_client, **params)
        assert all(item["workspace_id"] == str(WORKSPACE) for item in body["items"]), params
        assert all(item["request_id"] != "seed-5" for item in body["items"]), params
