from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mongomock

from app.core.config import Settings
from app.services.analytics_service import AnalyticsService
from app.services.filters import FilterOptions
from app.services.friday_service import FridayService


def _service() -> FridayService:
    db = mongomock.MongoClient().map_observability_test
    settings = Settings(mongo_uri="mongodb://example.invalid")
    analytics = AnalyticsService(db, settings)
    return FridayService(settings, analytics, correlation_service=None)


def test_llm_calls_query_summary() -> None:
    service = _service()
    now = datetime.now(timezone.utc)
    service.analytics_service.request_collection.insert_one(
        {
            "request_id": "rid-1",
            "status": "success",
            "duration_s": 2.0,
            "start_ts": now,
            "query": "hello",
        }
    )
    service.analytics_service.llm_collection.insert_many(
        [
            {
                "request_id": "rid-1",
                "agent_code": "Master",
                "phase": "route",
                "model": "deepseek",
                "status": "success",
                "duration_s": 1.2,
                "usage": {"total_tokens": 10},
                "start_ts": now,
            },
            {
                "request_id": "rid-1",
                "agent_code": "Operations",
                "phase": "tool_selection",
                "model": "deepseek",
                "status": "failed",
                "duration_s": 6.5,
                "error": "timeout",
                "usage": {"total_tokens": 5},
                "start_ts": now,
            },
        ]
    )

    payload = service.analytics_service.get_llm_calls(
        FilterOptions(
            start_ts=now - timedelta(minutes=1),
            end_ts=now + timedelta(minutes=1),
            container="map_core-dev",
            status=None,
            staff_code=None,
            session_id=None,
            request_id="rid-1",
            agent_code=None,
            tool=None,
            query_like=None,
        )
    )

    assert payload["summary"]["total"] == 2
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["token_total"] == 15


def test_friday_report_collects_failures_and_slow_llm() -> None:
    service = _service()
    now = datetime.now(timezone.utc)
    service.analytics_service.request_collection.insert_one(
        {
            "request_id": "rid-2",
            "status": "failed",
            "error": "agent failed",
            "duration_s": 9.0,
            "start_ts": now,
            "query": "why failed",
        }
    )
    service.analytics_service.tool_collection.insert_one(
        {
            "request_id": "rid-2",
            "agent_code": "Operations",
            "tool": "ask_database_agent",
            "status": "failed",
            "duration_s": 8.0,
            "error": "database timeout",
            "ts": now,
        }
    )
    service.analytics_service.llm_collection.insert_one(
        {
            "request_id": "rid-2",
            "agent_code": "Operations",
            "phase": "sub_agent_tool_selection",
            "model": "deepseek",
            "status": "success",
            "duration_s": 7.0,
            "start_ts": now,
        }
    )

    report = service.run_report(report_type="weekly", lookback_days=7)

    assert report["status"] == "success"
    assert report["summary"]["failed_request_count"] == 1
    assert report["summary"]["tool_failure_count"] == 1
    assert report["summary"]["data_failure_count"] == 1
    assert report["summary"]["slow_llm_count"] == 1
    assert "MAP 调用质量周报" in report["markdown"]
