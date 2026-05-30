from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_friday_service
from app.schemas.friday import (
    FridayChatRequest,
    FridayConfigRequest,
    FridayReportConfigRequest,
    FridayReportRunRequest,
)
from app.services.friday_service import FridayService

router = APIRouter(tags=["friday"])


@router.get("/friday/config")
def get_friday_config(
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.get_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/friday/config")
def update_friday_config(
    body: FridayConfigRequest,
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.update_config(base_url=body.base_url, model=body.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/friday/chat")
def friday_chat(
    body: FridayChatRequest,
    service: FridayService = Depends(get_friday_service),
) -> StreamingResponse:
    try:
        event_stream = service.stream_chat(body.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StreamingResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/friday/reports")
def list_friday_reports(
    report_type: str | None = None,
    limit: int = 20,
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.list_reports(report_type=report_type, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/friday/reports/config")
def get_friday_report_config(
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.get_report_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/friday/reports/config")
def update_friday_report_config(
    body: FridayReportConfigRequest,
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.update_report_config(body.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/friday/reports/run")
def run_friday_report(
    body: FridayReportRunRequest,
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.run_report(
            report_type=body.report_type,
            lookback_days=body.lookback_days,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/friday/reports/{report_id}")
def get_friday_report(
    report_id: str,
    service: FridayService = Depends(get_friday_service),
) -> dict:
    try:
        return service.get_report(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
