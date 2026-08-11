from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from fastapi import HTTPException, Query

from app.services.filters import FilterOptions, normalize_time_range


class Granularity(str, Enum):
    hour = "hour"
    day = "day"


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


@dataclass
class QueryContext:
    filters: FilterOptions
    page: int = 1
    page_size: int = 10
    sort_by: str = "start_ts"
    sort_order: SortOrder = SortOrder.desc
    top_n: int = 20
    granularity: Granularity = Granularity.hour


def parse_query_context(
    start_ts: datetime | None = Query(default=None),
    end_ts: datetime | None = Query(default=None),
    container: str | None = Query(default=None),
    staff_code: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    query_like: str | None = Query(default=None),
    status: str | None = Query(default=None),
    agent_code: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=10),
    sort_by: str = Query(default="start_ts"),
    sort_order: SortOrder = Query(default=SortOrder.desc),
    top_n: int = Query(default=20, ge=1, le=100),
    granularity: Granularity = Query(default=Granularity.hour),
    max_query_days: int = Query(default=31, include_in_schema=False),
    default_time_range_hours: int = Query(default=24, include_in_schema=False),
) -> QueryContext:
    try:
        normalized_start, normalized_end = normalize_time_range(
            start_ts=start_ts,
            end_ts=end_ts,
            default_hours=default_time_range_hours,
            max_days=max_query_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filters = FilterOptions(
        start_ts=normalized_start,
        end_ts=normalized_end,
        container=container,
        staff_code=staff_code,
        session_id=session_id,
        request_id=request_id,
        query_like=query_like,
        status=status,
        agent_code=agent_code,
        tool=tool,
    )

    return QueryContext(
        filters=filters,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        top_n=top_n,
        granularity=granularity,
    )
