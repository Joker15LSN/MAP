"""Request-domain query and serialization helpers.

Keeps the analytics facades thin by owning the request_records collection
access and every request-level payload serialization. No service coupling:
inject the raw pymongo/mongomock collection.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from app.services.math_utils import average, percentile, safe_div, to_float
from app.services.serializers import to_scene_confidences, to_token_total


class RequestRepository:
    """Thin wrapper around the request_records collection."""

    def __init__(self, collection: Any) -> None:
        self.collection = collection

    def find(
        self,
        match: Dict,
        projection: Dict,
        sort: Optional[list] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        cursor = self.collection.find(match, projection)
        if sort:
            cursor = cursor.sort(*sort) if len(sort) == 2 else cursor.sort(sort)
        if skip is not None:
            cursor = cursor.skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        return list(cursor)

    def find_one(self, match: Dict, projection: Optional[Dict] = None) -> Optional[Dict]:
        return self.collection.find_one(match, projection)

    def count(self, match: Dict) -> int:
        return self.collection.count_documents(match)

    def aggregate(self, pipeline: List[Dict]) -> List[Dict]:
        return list(self.collection.aggregate(pipeline))


def build_overview_payload(docs: List[Dict], tool_count_map: Dict[str, int]) -> Dict:
    """Serialize request documents into the /overview response body."""
    total_requests = len(docs)
    success_requests = sum(1 for doc in docs if str(doc.get("status", "")).lower() == "success")
    error_requests = total_requests - success_requests

    durations = [to_float(doc.get("duration_s"), 0.0) for doc in docs if doc.get("duration_s") is not None]
    token_totals = [to_token_total(doc) for doc in docs]

    total_tool_calls = sum(tool_count_map.values())

    all_big_conf = []
    all_sub_conf = []
    for doc in docs:
        confidence_map = to_scene_confidences(doc)
        all_big_conf.extend(confidence_map["big"])
        all_sub_conf.extend(confidence_map["sub"])

    return {
        "total_requests": total_requests,
        "success_requests": success_requests,
        "success_rate": safe_div(success_requests, total_requests),
        "error_rate": safe_div(error_requests, total_requests),
        "duration_s": {
            "avg": average(durations),
            "p50": percentile(durations, 0.50),
            "p90": percentile(durations, 0.90),
            "p95": percentile(durations, 0.95),
            "max": max(durations) if durations else 0.0,
        },
        "token": {
            "total": sum(token_totals),
            "avg_per_request": safe_div(sum(token_totals), total_requests),
            "efficiency_per_success_request": safe_div(sum(token_totals), success_requests),
        },
        "tool_calls": {
            "total": total_tool_calls,
            "per_request": safe_div(total_tool_calls, total_requests),
        },
        "scene_confidence_avg": {
            "big_scene": average(all_big_conf),
            "sub_scene": average(all_sub_conf),
        },
    }


def build_trends_rows(items: List[Dict]) -> List[Dict]:
    """Serialize aggregate rows into the /trends response body."""
    rows = []
    for item in items:
        total_requests = int(item.get("total_requests", 0))
        success_requests = int(item.get("success_requests", 0))
        rows.append(
            {
                "bucket_ts": item.get("_id"),
                "total_requests": total_requests,
                "success_rate": safe_div(success_requests, total_requests),
                "avg_duration_s": to_float(item.get("avg_duration_s"), 0.0),
                "token_total": to_float(item.get("token_total"), 0.0),
            }
        )
    return rows


def build_users_rows(docs: List[Dict], tool_count_map: Dict[str, int], top_n: int) -> List[Dict]:
    """Serialize request documents into the /users response body."""
    from collections import defaultdict

    user_map = defaultdict(
        lambda: {
            "request_count": 0,
            "success_count": 0,
            "durations": [],
            "token_total": 0.0,
            "request_ids": [],
        }
    )

    for doc in docs:
        staff_code = doc.get("staff_code") or "UNKNOWN"
        user = user_map[staff_code]
        user["request_count"] += 1
        if str(doc.get("status", "")).lower() == "success":
            user["success_count"] += 1
        user["durations"].append(to_float(doc.get("duration_s"), 0.0))
        user["token_total"] += to_token_total(doc)
        if doc.get("request_id"):
            user["request_ids"].append(doc["request_id"])

    rows = []
    for staff_code, info in user_map.items():
        request_count = info["request_count"]
        tool_calls = sum(tool_count_map.get(request_id, 0) for request_id in info["request_ids"])
        rows.append(
            {
                "staff_code": staff_code,
                "request_count": request_count,
                "success_rate": safe_div(info["success_count"], request_count),
                "avg_duration_s": average(info["durations"]),
                "p95_duration_s": percentile(info["durations"], 0.95),
                "token_total": info["token_total"],
                "tool_calls_per_request": safe_div(tool_calls, request_count),
            }
        )

    rows.sort(key=lambda item: item["request_count"], reverse=True)
    return rows[:top_n]


def build_request_items(docs: List[Dict], tool_count_map: Dict[str, int]) -> List[Dict]:
    """Serialize request documents into /requests list items."""
    items = []
    for doc in docs:
        request_id = doc.get("request_id")
        items.append(
            {
                "request_id": request_id,
                "session_id": doc.get("session_id"),
                "staff_code": doc.get("staff_code"),
                "status": doc.get("status"),
                "duration_s": to_float(doc.get("duration_s"), 0.0),
                "start_ts": doc.get("start_ts"),
                "end_ts": doc.get("end_ts"),
                "query": doc.get("query"),
                "agents_called": doc.get("agents_called") or [],
                "token_total": to_token_total(doc),
                "tool_call_count": tool_count_map.get(request_id, 0),
            }
        )
    return items


def build_request_payload(request_doc: Dict) -> Dict:
    """Serialize a single request document for /requests/{id} detail."""
    return {
        **request_doc,
        "duration_s": to_float(request_doc.get("duration_s"), 0.0),
        "token_total": to_token_total(request_doc),
    }
