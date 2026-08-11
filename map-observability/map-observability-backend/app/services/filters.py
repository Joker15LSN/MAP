from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class FilterOptions:
    start_ts: datetime
    end_ts: datetime
    container: str | None = None
    staff_code: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    status: str | None = None
    agent_code: str | None = None
    tool: str | None = None
    query_like: str | None = None


def normalize_time_range(
    start_ts: datetime | None,
    end_ts: datetime | None,
    default_hours: int,
    max_days: int,
) -> (datetime, datetime):
    now_utc = datetime.now(UTC)

    end = end_ts or now_utc
    start = start_ts or (end - timedelta(hours=default_hours))

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    if end < start:
        raise ValueError("end_ts must be greater than or equal to start_ts")

    if end - start > timedelta(days=max_days):
        raise ValueError(f"time range exceeds MAX_QUERY_DAYS={max_days}")

    return start.astimezone(UTC), end.astimezone(UTC)


def build_request_match(filters: FilterOptions) -> dict:
    match = {
        "start_ts": {
            "$gte": filters.start_ts,
            "$lte": filters.end_ts,
        }
    }

    if filters.staff_code:
        match["staff_code"] = filters.staff_code
    if filters.session_id:
        match["session_id"] = filters.session_id
    if filters.request_id:
        match["request_id"] = filters.request_id
    if filters.status:
        match["status"] = filters.status
    if filters.query_like:
        query_pattern = re.escape(filters.query_like.strip())
        if query_pattern:
            match["query"] = {"$regex": query_pattern, "$options": "i"}

    return match


def build_agent_match(filters: FilterOptions) -> dict:
    match = {
        "ts": {
            "$gte": filters.start_ts,
            "$lte": filters.end_ts,
        }
    }

    if filters.session_id:
        match["session_id"] = filters.session_id
    if filters.request_id:
        match["request_id"] = filters.request_id
    if filters.staff_code:
        match["staff_code"] = filters.staff_code
    if filters.agent_code:
        match["agent_code"] = filters.agent_code

    return match


def build_tool_match(filters: FilterOptions) -> dict:
    match = {
        "ts": {
            "$gte": filters.start_ts,
            "$lte": filters.end_ts,
        }
    }

    if filters.session_id:
        match["session_id"] = filters.session_id
    if filters.request_id:
        match["request_id"] = filters.request_id
    if filters.agent_code:
        match["agent_code"] = filters.agent_code
    if filters.tool:
        match["tool"] = filters.tool

    return match
