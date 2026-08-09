"""Contract tests for the AnalyticsService facade against mongomock.

These tests pin the public JSON contract of every analytics endpoint the
frontend consumes. They must keep passing as long as response field
names/types and the query/route semantics stay unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock

from app.core.config import Settings
from app.services.analytics_service import AnalyticsService
from app.services.filters import FilterOptions


def _make_service() -> AnalyticsService:
    db = mongomock.MongoClient().map_observability_test
    settings = Settings(mongo_uri="mongodb://example.invalid")
    return AnalyticsService(db, settings)


def _seed_request(
    service: AnalyticsService,
    *,
    request_id: str,
    staff_code: str = "u-1",
    status: str = "success",
    duration_s: float = 3.0,
    start_ts: datetime | None = None,
) -> datetime:
    now = start_ts or datetime.now(UTC)
    service.request_collection.insert_one(
        {
            "request_id": request_id,
            "session_id": "sess-1",
            "staff_code": staff_code,
            "query": "hello",
            "status": status,
            "duration_s": duration_s,
            "start_ts": now,
            "end_ts": now + timedelta(seconds=duration_s),
            "agents_called": ["Master", "Operations"],
            "token_usage_total": {"total": {"total_tokens": 100}},
            "scene_result": {
                "big_scenes": [{"confidence": 0.9}],
                "sub_scenes": [{"confidence": 0.8}],
            },
        }
    )
    return now


def _make_filters(now: datetime, **kwargs) -> FilterOptions:
    defaults = {
        "start_ts": now - timedelta(minutes=5),
        "end_ts": now + timedelta(minutes=5),
        "container": None,
        "status": None,
        "staff_code": None,
        "session_id": None,
        "request_id": None,
        "agent_code": None,
        "tool": None,
        "query_like": None,
    }
    defaults.update(kwargs)
    return FilterOptions(**defaults)


def test_overview_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-overview", duration_s=2.0)
    payload = service.get_overview(_make_filters(now))

    assert set(payload) == {
        "total_requests",
        "success_requests",
        "success_rate",
        "error_rate",
        "duration_s",
        "token",
        "tool_calls",
        "scene_confidence_avg",
    }
    assert payload["total_requests"] == 1
    assert payload["success_requests"] == 1
    assert payload["success_rate"] == 1.0
    assert payload["error_rate"] == 0.0
    assert payload["duration_s"] == {
        "avg": 2.0,
        "p50": 2.0,
        "p90": 2.0,
        "p95": 2.0,
        "max": 2.0,
    }
    assert payload["token"] == {
        "total": 100.0,
        "avg_per_request": 100.0,
        "efficiency_per_success_request": 100.0,
    }
    assert payload["tool_calls"] == {"total": 0, "per_request": 0.0}
    assert payload["scene_confidence_avg"] == {"big_scene": 0.9, "sub_scene": 0.8}


def test_requests_list_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-list", duration_s=2.0)
    service.tool_collection.insert_one(
        {
            "request_id": "rid-list",
            "agent_code": "Operations",
            "tool": "ask_database_agent",
            "status": "success",
            "duration_s": 1.0,
            "ts": now,
        }
    )

    payload = service.list_requests(
        filters=_make_filters(now),
        page=1,
        page_size=10,
        sort_by="start_ts",
        sort_order="desc",
    )

    assert set(payload) == {"total", "page", "page_size", "items"}
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 10
    item = payload["items"][0]
    assert set(item) == {
        "request_id",
        "session_id",
        "staff_code",
        "status",
        "duration_s",
        "start_ts",
        "end_ts",
        "query",
        "agents_called",
        "token_total",
        "tool_call_count",
    }
    assert item["request_id"] == "rid-list"
    assert item["duration_s"] == 2.0
    assert item["token_total"] == 100.0
    assert item["tool_call_count"] == 1
    assert item["agents_called"] == ["Master", "Operations"]


def test_request_detail_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-detail", duration_s=2.0)
    service.agent_collection.insert_many(
        [
            {
                "request_id": "rid-detail",
                "state_id": "st-1",
                "agent_code": "Master",
                "agent_name": "Master",
                "seq": 0,
                "stage": "start",
                "status": None,
                "ts": now,
            },
            {
                "request_id": "rid-detail",
                "state_id": "st-1",
                "agent_code": "Master",
                "agent_name": "Master",
                "seq": 1,
                "stage": "end",
                "status": "success",
                "ts": now + timedelta(seconds=1),
            },
        ]
    )
    service.tool_collection.insert_one(
        {
            "request_id": "rid-detail",
            "agent_code": "Operations",
            "tool": "ask_database_agent",
            "tool_id": "t-1",
            "step": 1,
            "status": "success",
            "duration_s": 1.0,
            "ts": now,
        }
    )
    service.llm_collection.insert_one(
        {
            "request_id": "rid-detail",
            "agent_code": "Master",
            "phase": "route",
            "model": "deepseek",
            "status": "success",
            "duration_s": 1.0,
            "usage": {"total_tokens": 10},
            "start_ts": now,
        }
    )

    payload = service.get_request_detail("rid-detail")

    assert set(payload) == {
        "request",
        "agent_timeline",
        "agent_events",
        "tool_calls",
        "llm_calls",
        "summary",
    }
    assert payload["request"]["request_id"] == "rid-detail"
    assert payload["request"]["token_total"] == 100.0
    assert payload["summary"] == {
        "agent_event_count": 2,
        "tool_call_count": 1,
        "tool_call_raw_count": 1,
        "llm_call_count": 1,
    }
    assert len(payload["agent_timeline"]) == 1
    assert payload["agent_timeline"][0]["duration_s"] == 1.0
    assert len(payload["tool_calls"]) == 1
    assert payload["tool_calls"][0]["tool"] == "ask_database_agent"
    assert len(payload["llm_calls"]) == 1


def test_request_detail_404() -> None:
    service = _make_service()
    try:
        service.get_request_detail("missing")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert "missing" in str(exc)


def test_users_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-user-a", staff_code="alice", duration_s=2.0)
    _seed_request(service, request_id="rid-user-b", staff_code="bob", duration_s=4.0, start_ts=now)
    service.tool_collection.insert_one(
        {
            "request_id": "rid-user-a",
            "agent_code": "Operations",
            "tool": "ask_database_agent",
            "status": "success",
            "duration_s": 1.0,
            "ts": now,
        }
    )

    rows = service.get_users(_make_filters(now), top_n=20)

    assert isinstance(rows, list)
    assert len(rows) == 2
    alice = next(row for row in rows if row["staff_code"] == "alice")
    assert set(alice) == {
        "staff_code",
        "request_count",
        "success_rate",
        "avg_duration_s",
        "p95_duration_s",
        "token_total",
        "tool_calls_per_request",
    }
    assert alice["request_count"] == 1
    assert alice["success_rate"] == 1.0
    assert alice["avg_duration_s"] == 2.0
    assert alice["token_total"] == 100.0
    assert alice["tool_calls_per_request"] == 1.0


def test_agents_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-agent", status="success", duration_s=15.0)
    service.agent_collection.insert_many(
        [
            {
                "request_id": "rid-agent",
                "state_id": "st-1",
                "agent_code": "Operations",
                "agent_name": "Operations",
                "seq": 0,
                "stage": "start",
                "status": None,
                "ts": now,
            },
            {
                "request_id": "rid-agent",
                "state_id": "st-1",
                "agent_code": "Operations",
                "agent_name": "Operations",
                "seq": 1,
                "stage": "end",
                "status": "success",
                "ts": now + timedelta(seconds=12),
            },
        ]
    )

    rows = service.get_agents(_make_filters(now), top_n=20)

    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "agent_code",
        "agent_name",
        "call_count",
        "success_rate",
        "avg_duration_s",
        "slow_call_ratio",
    }
    assert row["agent_code"] == "Operations"
    assert row["call_count"] == 1
    assert row["success_rate"] == 1.0
    assert row["avg_duration_s"] == 12.0
    # slow threshold default 10s, duration 12s -> slow
    assert row["slow_call_ratio"] == 1.0


def test_tools_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-tool", duration_s=10.0)
    service.tool_collection.insert_many(
        [
            {
                "request_id": "rid-tool",
                "agent_code": "Operations",
                "tool": "ask_database_agent",
                "status": "success",
                "duration_s": 2.0,
                "ts": now,
            },
            {
                "request_id": "rid-tool",
                "agent_code": "Operations",
                "tool": "ask_database_agent",
                "status": "failed",
                "duration_s": 3.0,
                "ts": now,
            },
        ]
    )

    payload = service.get_tools(_make_filters(now), top_n=20)

    assert set(payload) == {"items", "failure_top"}
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert set(item) == {"tool", "call_count", "success_rate", "avg_duration_s", "failed_count"}
    assert item["tool"] == "ask_database_agent"
    assert item["call_count"] == 2
    assert item["success_rate"] == 0.5
    assert item["avg_duration_s"] == 2.5
    assert item["failed_count"] == 1
    assert payload["failure_top"][0]["tool"] == "ask_database_agent"


def test_llm_calls_contract() -> None:
    service = _make_service()
    now = _seed_request(service, request_id="rid-llm", duration_s=2.0)
    service.llm_collection.insert_many(
        [
            {
                "request_id": "rid-llm",
                "agent_code": "Master",
                "phase": "route",
                "model": "deepseek",
                "status": "success",
                "duration_s": 1.0,
                "usage": {"total_tokens": 10},
                "start_ts": now,
            },
            {
                "request_id": "rid-llm",
                "agent_code": "Operations",
                "phase": "tool_selection",
                "model": "deepseek",
                "status": "failed",
                "duration_s": 5.0,
                "error": "timeout",
                "usage": {"total_tokens": 5},
                "start_ts": now,
            },
        ]
    )

    payload = service.get_llm_calls(_make_filters(now))

    assert set(payload) == {"items", "summary"}
    summary = payload["summary"]
    assert set(summary) == {
        "call_count",
        "failed_count",
        "total",
        "success",
        "failed",
        "avg_duration_s",
        "p95_duration_s",
        "token_total",
    }
    assert summary["total"] == 2
    assert summary["call_count"] == 2
    assert summary["failed"] == 1
    assert summary["success"] == 1
    assert summary["token_total"] == 15
    assert len(payload["items"]) == 2
    items = {item["agent_code"] for item in payload["items"]}
    assert items == {"Master", "Operations"}


def test_llm_calls_empty_payload() -> None:
    service = _make_service()
    now = datetime.now(UTC)
    payload = service.get_llm_calls(_make_filters(now))
    assert payload == {
        "items": [],
        "summary": {
            "call_count": 0,
            "failed_count": 0,
            "total": 0,
            "success": 0,
            "failed": 0,
            "avg_duration_s": 0.0,
            "p95_duration_s": 0.0,
            "token_total": 0,
        },
    }


def test_contract_field_types_are_stable() -> None:
    """FIX-P2-OBSERVABILITY-01:pin field NAMES and TYPES, not just values.

    Every endpoint listed in the F-05 plan (overview/requests/detail/users/
    agents/tools/llm-calls) must keep its field names and value types; a
    type drift (e.g. duration_s str->int) breaks the frontend contract.
    """
    service = _make_service()
    now = _seed_request(service, request_id="rid-types", duration_s=2.0)

    overview = service.get_overview(_make_filters(now))
    assert type(overview["total_requests"]) is int
    assert type(overview["success_rate"]) is float
    assert type(overview["duration_s"]) is dict
    assert type(overview["duration_s"]["avg"]) is float
    assert type(overview["token"]) is dict
    assert type(overview["tool_calls"]) is dict
    assert type(overview["scene_confidence_avg"]) is dict

    listing = service.list_requests(
        _make_filters(now), page=1, page_size=10, sort_by="start_ts", sort_order="desc"
    )
    assert type(listing["total"]) is int
    assert type(listing["items"]) is list
    for item in listing["items"]:
        assert type(item["request_id"]) is str
        assert type(item["duration_s"]) is float

    detail = service.get_request_detail("rid-types")
    assert type(detail["request"]) is dict
    assert type(detail["request"]["request_id"]) is str
    assert type(detail["tool_calls"]) is list
    assert type(detail["llm_calls"]) is list
    assert type(detail["agent_timeline"]) is list
    assert type(detail["summary"]) is dict

    users = service.get_users(_make_filters(now), top_n=10)
    assert type(users) is list
    for item in users:
        assert type(item["staff_code"]) is str
        assert type(item["request_count"]) is int
        assert type(item["success_rate"]) is float

    agents = service.get_agents(_make_filters(now), top_n=10)
    assert type(agents) is list
    for item in agents:
        assert type(item["agent_code"]) is str
        assert type(item["call_count"]) is int
        assert type(item["success_rate"]) is float

    tools = service.get_tools(_make_filters(now), top_n=10)
    assert type(tools) is dict
    assert type(tools["items"]) is list
    assert type(tools["failure_top"]) is list

    llms = service.get_llm_calls(_make_filters(now))
    assert type(llms["summary"]) is dict
    assert type(llms["summary"]["call_count"]) is int
