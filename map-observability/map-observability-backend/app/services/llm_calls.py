"""LLM call domain query and serialization helpers.

Owns the llm_call_records collection access and the /llm-calls payload
serialization. Kept independent so new LLM-focused metrics can be added
without touching the analytics facade.
"""

from __future__ import annotations

from typing import Any

from app.services.math_utils import average, percentile, to_float

EMPTY_LLM_CALLS_PAYLOAD = {
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

LLM_PROJECTION = {
    "_id": 0,
    "state_id": 1,
    "request_id": 1,
    "session_id": 1,
    "staff_code": 1,
    "seq": 1,
    "agent_code": 1,
    "agent_name": 1,
    "component": 1,
    "phase": 1,
    "step": 1,
    "call_kind": 1,
    "model": 1,
    "provider_request_id": 1,
    "start_ts": 1,
    "end_ts": 1,
    "duration_s": 1,
    "status": 1,
    "usage": 1,
    "error": 1,
    "finish_reason": 1,
    "prompt_summary": 1,
    "tool_names": 1,
}


class LlmRepository:
    """Thin wrapper around the llm_call_records collection."""

    def __init__(self, llm_collection: Any) -> None:
        self.llm_collection = llm_collection

    def find_for_request_ids(
        self,
        request_ids: list[str],
        agent_code: str | None = None,
        top_n: int = 200,
    ) -> list[dict]:
        match: dict[str, Any] = {"request_id": {"$in": request_ids}}
        if agent_code:
            match["agent_code"] = agent_code
        cursor = (
            self.llm_collection.find(
                match,
                LLM_PROJECTION,
            )
            .sort([("start_ts", -1), ("seq", -1)])
            .limit(top_n)
        )
        return list(cursor)

    def find_for_request(self, request_id: str) -> list[dict]:
        return list(
            self.llm_collection.find(
                {"request_id": request_id},
                LLM_PROJECTION,
            ).sort([("start_ts", 1), ("seq", 1)])
        )


def build_llm_calls_payload(items: list[dict]) -> dict:
    """Serialize raw LLM documents into the /llm-calls response body."""
    failed_count = sum(1 for item in items if str(item.get("status")) != "success")
    durations = [
        to_float(item.get("duration_s"), 0.0)
        for item in items
        if item.get("duration_s") is not None
    ]
    token_total = 0
    for item in items:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        token_total += int(
            to_float(
                usage.get("total_tokens")
                or usage.get("total")
                or usage.get("completion_tokens")
                or 0,
                0.0,
            )
        )
    call_count = len(items)
    return {
        "items": items,
        "summary": {
            "call_count": call_count,
            "failed_count": failed_count,
            "total": call_count,
            "success": call_count - failed_count,
            "failed": failed_count,
            "avg_duration_s": average(durations),
            "p95_duration_s": percentile(durations, 95),
            "token_total": token_total,
        },
    }
