"""Agent / tool domain query and serialization helpers.

Owns the agent_executions and tool_call_records collections access plus the
/agents and /tools payload serialization, plus the cross-request aggregates
used by other domains (request_id resolution and tool call counts).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.services.filters import FilterOptions, build_agent_match, build_tool_match
from app.services.math_utils import average, safe_div, to_float
from app.services.serializers import tool_status

EXCLUDED_AGENT_CODES_FOR_DURATION = {"globaldomainorchestrator"}


class AgentToolRepository:
    """Thin wrapper around agent_executions + tool_call_records collections."""

    def __init__(self, agent_collection: Any, tool_collection: Any) -> None:
        self.agent_collection = agent_collection
        self.tool_collection = tool_collection

    def request_ids_from_agents(self, filters: FilterOptions) -> set:
        match = build_agent_match(filters)
        cursor = self.agent_collection.find(match, {"request_id": 1})
        return {doc.get("request_id") for doc in cursor if doc.get("request_id")}

    def request_ids_from_tools(self, filters: FilterOptions) -> set:
        match = build_tool_match(filters)
        cursor = self.tool_collection.find(match, {"request_id": 1})
        return {doc.get("request_id") for doc in cursor if doc.get("request_id")}

    def tool_call_count_map(self, request_ids: Iterable[str]) -> dict[str, int]:
        ids = [request_id for request_id in request_ids if request_id]
        if not ids:
            return {}

        pipeline = [
            {"$match": {"request_id": {"$in": ids}}},
            {"$group": {"_id": "$request_id", "count": {"$sum": 1}}},
        ]
        return {
            item["_id"]: int(item.get("count", 0))
            for item in self.tool_collection.aggregate(pipeline)
        }

    def group_agent_executions(self, filters: FilterOptions, request_ids: list[str]) -> list[dict]:
        if not request_ids:
            return []

        match = build_agent_match(filters)
        match["request_id"] = {"$in": request_ids}

        pipeline = [
            {"$match": match},
            {"$sort": {"ts": 1}},
            {
                "$project": {
                    "request_id": 1,
                    "state_id": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "status": 1,
                    "ts": 1,
                    "start_ts": {"$cond": [{"$eq": ["$stage", "start"]}, "$ts", None]},
                    "end_ts": {"$cond": [{"$eq": ["$stage", "end"]}, "$ts", None]},
                }
            },
            {
                "$group": {
                    "_id": {
                        "request_id": "$request_id",
                        "state_id": "$state_id",
                        "agent_code": "$agent_code",
                    },
                    "request_id": {"$first": "$request_id"},
                    "agent_code": {"$first": "$agent_code"},
                    "agent_name": {"$last": "$agent_name"},
                    "start_ts": {"$min": "$start_ts"},
                    "end_ts": {"$max": "$end_ts"},
                    "first_ts": {"$min": "$ts"},
                    "last_ts": {"$max": "$ts"},
                    "status": {"$last": "$status"},
                }
            },
        ]

        return list(self.agent_collection.aggregate(pipeline))

    def agent_events_for_request(self, request_id: str) -> list[dict]:
        return list(
            self.agent_collection.find(
                {"request_id": request_id},
                {
                    "_id": 0,
                    "state_id": 1,
                    "request_id": 1,
                    "session_id": 1,
                    "staff_code": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "seq": 1,
                    "event_type": 1,
                    "component": 1,
                    "stage": 1,
                    "status": 1,
                    "payload": 1,
                    "ts": 1,
                },
            ).sort("ts", 1)
        )

    def tool_calls_raw_for_request(self, request_id: str) -> list[dict]:
        return list(
            self.tool_collection.find(
                {"request_id": request_id},
                {
                    "_id": 0,
                    "event_type": 1,
                    "state_id": 1,
                    "request_id": 1,
                    "session_id": 1,
                    "ts": 1,
                    "agent_code": 1,
                    "agent_name": 1,
                    "agent_id": 1,
                    "tool": 1,
                    "tool_id": 1,
                    "step": 1,
                    "args": 1,
                    "output": 1,
                    "status": 1,
                    "duration_s": 1,
                },
            ).sort("ts", 1)
        )

    def find_tools(
        self,
        match: dict,
        projection: dict,
        sort: list | None = None,
    ) -> list[dict]:
        cursor = self.tool_collection.find(match, projection)
        if sort:
            cursor = cursor.sort(*sort) if len(sort) == 2 else cursor.sort(sort)
        return list(cursor)


def build_agents_rows(
    executions: list[dict],
    request_status_map: dict[str, str],
    slow_threshold_s: float,
) -> list[dict]:
    """Serialize grouped agent executions into /agents rows."""
    grouped = defaultdict(
        lambda: {
            "agent_name": "",
            "call_count": 0,
            "success_count": 0,
            "durations": [],
            "slow_count": 0,
        }
    )

    for item in executions:
        agent_code = item.get("agent_code") or "UNKNOWN"
        if str(agent_code).strip().lower() in EXCLUDED_AGENT_CODES_FOR_DURATION:
            continue
        group = grouped[agent_code]
        group["agent_name"] = item.get("agent_name") or agent_code
        group["call_count"] += 1

        request_status = request_status_map.get(item.get("request_id"), "")
        if request_status == "success":
            group["success_count"] += 1

        start_ts = item.get("start_ts")
        end_ts = item.get("end_ts")
        first_ts = item.get("first_ts")
        last_ts = item.get("last_ts")
        duration_s = 0.0
        if isinstance(start_ts, datetime) and isinstance(end_ts, datetime):
            duration_s = max((end_ts - start_ts).total_seconds(), 0.0)
        elif isinstance(first_ts, datetime) and isinstance(last_ts, datetime):
            duration_s = max((last_ts - first_ts).total_seconds(), 0.0)

        group["durations"].append(duration_s)
        if duration_s >= slow_threshold_s:
            group["slow_count"] += 1

    rows = []
    for agent_code, info in grouped.items():
        call_count = info["call_count"]
        rows.append(
            {
                "agent_code": agent_code,
                "agent_name": info["agent_name"],
                "call_count": call_count,
                "success_rate": safe_div(info["success_count"], call_count),
                "avg_duration_s": average(info["durations"]),
                "slow_call_ratio": safe_div(info["slow_count"], call_count),
            }
        )

    rows.sort(key=lambda item: item["call_count"], reverse=True)
    return rows


def build_tools_payload(
    tool_docs: list[dict],
    request_duration_map: dict[str, float],
    top_n: int,
) -> dict:
    """Serialize tool call documents into the /tools response body."""
    grouped = defaultdict(
        lambda: {
            "call_count": 0,
            "known_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "explicit_duration": [],
            "proxy_duration_sum": 0.0,
            "proxy_duration_count": 0,
        }
    )

    for doc in tool_docs:
        tool_name = doc.get("tool") or "UNKNOWN"
        group = grouped[tool_name]
        group["call_count"] += 1

        normalized_status = tool_status(doc.get("status"))
        if normalized_status != "unknown":
            group["known_count"] += 1
            if normalized_status == "success":
                group["success_count"] += 1
            else:
                group["failed_count"] += 1

        explicit_duration = doc.get("duration_s")
        if explicit_duration is not None:
            group["explicit_duration"].append(to_float(explicit_duration, 0.0))
        else:
            request_duration = request_duration_map.get(doc.get("request_id"), 0.0)
            group["proxy_duration_sum"] += request_duration
            group["proxy_duration_count"] += 1

    items = []
    for tool_name, info in grouped.items():
        if info["explicit_duration"]:
            avg_duration = average(info["explicit_duration"])
        else:
            avg_duration = safe_div(info["proxy_duration_sum"], info["proxy_duration_count"])

        items.append(
            {
                "tool": tool_name,
                "call_count": info["call_count"],
                "success_rate": safe_div(info["success_count"], info["known_count"]),
                "avg_duration_s": avg_duration,
                "failed_count": info["failed_count"],
            }
        )

    items.sort(key=lambda item: item["call_count"], reverse=True)
    failure_top = sorted(items, key=lambda item: item["failed_count"], reverse=True)[:top_n]

    return {
        "items": items[:top_n],
        "failure_top": failure_top,
    }
