from __future__ import annotations

from datetime import datetime

from fastapi import Depends, HTTPException, Query, Request

from app.core.config import Settings
from app.schemas.query import Granularity, QueryContext, SortOrder
from app.services.analytics_service import AnalyticsService
from app.services.correlation_service import CorrelationService
from app.services.filters import FilterOptions, normalize_time_range
from app.services.friday_service import FridayService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_analytics_service(
    request: Request,
    container: str | None = Query(default=None),
) -> AnalyticsService:
    normalized = str(container or "").strip()
    if normalized == "map_core-preprod":
        alt = getattr(request.app.state, "analytics_service_ubddev", None)
        if alt is not None:
            return alt
    return request.app.state.analytics_service


def get_correlation_service(
    request: Request,
    container: str | None = Query(default=None),
) -> CorrelationService:
    normalized = str(container or "").strip()
    if normalized == "map_core-preprod":
        alt = getattr(request.app.state, "correlation_service_ubddev", None)
        if alt is not None:
            return alt
    return request.app.state.correlation_service


def get_friday_service(request: Request) -> FridayService:
    return request.app.state.friday_service


def get_query_context(
    settings: Settings = Depends(get_settings),
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
) -> QueryContext:
    try:
        normalized_start, normalized_end = normalize_time_range(
            start_ts=start_ts,
            end_ts=end_ts,
            default_hours=settings.default_time_range_hours,
            max_days=settings.max_query_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return QueryContext(
        filters=FilterOptions(
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
        ),
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        top_n=top_n,
        granularity=granularity,
    )
