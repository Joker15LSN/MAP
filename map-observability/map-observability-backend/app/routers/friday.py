from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_friday_service
from app.schemas.friday import FridayChatRequest, FridayConfigRequest
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
