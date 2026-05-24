from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_analytics_service, get_query_context
from app.schemas.query import QueryContext
from app.services.analytics_service import AnalyticsService

router = APIRouter(tags=["analytics"])


@router.get("/overview")
def get_overview(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict:
    return service.get_overview(context.filters)


@router.get("/trends")
def get_trends(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list:
    return service.get_trends(context.filters, context.granularity.value)


@router.get("/users")
def get_users(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list:
    return service.get_users(context.filters, context.top_n)


@router.get("/agents")
def get_agents(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> list:
    return service.get_agents(context.filters, context.top_n)


@router.get("/tools")
def get_tools(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict:
    return service.get_tools(context.filters, context.top_n)


@router.get("/requests")
def list_requests(
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict:
    try:
        return service.list_requests(
            filters=context.filters,
            page=context.page,
            page_size=context.page_size,
            sort_by=context.sort_by,
            sort_order=context.sort_order.value,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/requests/export/jsonl")
def export_requests_jsonl(
    request_ids: str | None = Query(default=None),
    context: QueryContext = Depends(get_query_context),
    service: AnalyticsService = Depends(get_analytics_service),
) -> StreamingResponse:
    parsed_request_ids = [item.strip() for item in (request_ids or "").split(",") if item.strip()] or None
    try:
        stream = service.iter_request_export_jsonl(
            filters=context.filters,
            request_ids=parsed_request_ids,
            sort_by=context.sort_by,
            sort_order=context.sort_order.value,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamingResponse(
        stream,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="map_core-request-export.jsonl"'},
    )


@router.get("/requests/{request_id}")
def get_request_detail(
    request_id: str,
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict:
    try:
        return service.get_request_detail(request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
