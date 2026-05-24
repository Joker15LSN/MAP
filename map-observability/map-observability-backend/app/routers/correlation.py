from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import get_correlation_service
from app.services.correlation_service import CorrelationService

router = APIRouter(tags=["correlation"])


@router.get("/correlation/time-align")
def time_align(
    start_local: str = Query(...),
    end_local: str = Query(...),
    tz: Optional[str] = Query(default=None),
    buffer_seconds: int = Query(default=120, ge=0, le=3600),
    service: CorrelationService = Depends(get_correlation_service),
) -> dict:
    try:
        return service.time_align(
            start_local=start_local,
            end_local=end_local,
            tz=tz,
            buffer_seconds=buffer_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/correlation/rid/{request_id}")
def correlation_by_rid(
    request_id: str,
    container: str = Query(...),
    levels: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=10),
    window_sec: int = Query(default=120, ge=0, le=3600),
    service: CorrelationService = Depends(get_correlation_service),
) -> dict:
    level_list = [item.strip() for item in (levels or "").split(",") if item.strip()]
    try:
        return service.get_rid_correlation(
            request_id=request_id,
            container=container,
            window_sec=window_sec,
            levels=level_list,
            page=page,
            page_size=page_size,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/correlation/errors")
def correlation_errors(
    container: str = Query(...),
    start_local: str = Query(...),
    end_local: str = Query(...),
    tz: Optional[str] = Query(default=None),
    keywords: Optional[str] = Query(default=None),
    levels: Optional[str] = Query(default=None),
    staff_code: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    request_id: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=10),
    buffer_seconds: int = Query(default=120, ge=0, le=3600),
    service: CorrelationService = Depends(get_correlation_service),
) -> dict:
    keyword_list = [item.strip() for item in (keywords or "").split(",") if item.strip()]
    level_list = [item.strip() for item in (levels or "").split(",") if item.strip()]

    try:
        return service.get_error_clusters(
            container=container,
            start_local=start_local,
            end_local=end_local,
            tz=tz,
            keywords=keyword_list,
            levels=level_list,
            staff_code=staff_code,
            session_id=session_id,
            request_id=request_id,
            page=page,
            page_size=page_size,
            buffer_seconds=buffer_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/correlation/tool-call")
def correlation_tool_call(
    request_id: str = Query(...),
    container: str = Query(...),
    tool: str = Query(...),
    tool_id: Optional[str] = Query(default=None),
    step: Optional[int] = Query(default=None, ge=0),
    levels: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=10),
    window_sec: int = Query(default=120, ge=0, le=3600),
    service: CorrelationService = Depends(get_correlation_service),
) -> dict:
    level_list = [item.strip() for item in (levels or "").split(",") if item.strip()]
    try:
        return service.get_tool_call_correlation(
            request_id=request_id,
            container=container,
            tool=tool,
            tool_id=tool_id,
            step=step,
            levels=level_list,
            page=page,
            page_size=page_size,
            window_sec=window_sec,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
