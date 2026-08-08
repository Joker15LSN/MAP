import json
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..schema.master_pipeline_schema import (
    MasterAgentChatSchema,
    MasterPipelineChatResponse,
    MasterPipelineStreamEvent,
)
from ..service.master_pipeline import MasterPipeline

master_pipeline_router = APIRouter(prefix="/master_pipeline")


STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "SSE stream. Each frame contains event name and JSON payload in data line.",
        "content": {
            "text/event-stream": {
                "schema": MasterPipelineStreamEvent.model_json_schema(),
            }
        },
    }
}


_ID_HEADER_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _validated_id_header(raw: str | None) -> str | None:
    """Return the trimmed header value when it satisfies the F-04 ID contract.

    Contract: non-empty, at most 128 chars, charset [A-Za-z0-9._:-].
    Returns None when missing, empty, over-long, or containing other chars.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > 128:
        return None
    if not _ID_HEADER_PATTERN.fullmatch(value):
        return None
    return value


def _apply_runtime_headers(
    http_request: Request,
    *,
    request_token: str | None,
) -> None:
    http_request.state.request_token = request_token
    http_request.state.x_userid = http_request.headers.get("X-UserId", "missing")
    http_request.state.x_username = http_request.headers.get("X-UserName", "missing")
    # F-04 unified id resolution: honor valid inbound headers, otherwise
    # request_id falls back to a fresh uuid4().hex; session/workspace stay None.
    http_request.state.request_id = (
        _validated_id_header(http_request.headers.get("X-Request-ID"))
        or uuid4().hex
    )
    http_request.state.session_id = _validated_id_header(
        http_request.headers.get("X-Session-ID")
    )
    http_request.state.workspace_id = _validated_id_header(
        http_request.headers.get("X-Workspace-ID")
    )


def _format_sse_event(event: MasterPipelineStreamEvent) -> str:
    payload = (
        event.data.model_dump() if isinstance(event.data, BaseModel) else event.data
    )
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.event}\ndata: {data}\n\n"


@master_pipeline_router.post(
    "/chat/stream",
    responses=STREAM_RESPONSES,
)
async def master_chat_stream(
    request: MasterAgentChatSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
):
    _apply_runtime_headers(http_request, request_token=request_token)
    master_pipeline = MasterPipeline(request=request, http_request=http_request)

    async def iter_events():
        async for event in master_pipeline.pipeline_stream(request):
            yield _format_sse_event(event)
        http_request.state._stream_logically_completed = True

    return StreamingResponse(
        iter_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@master_pipeline_router.post("/chat", response_model=MasterPipelineChatResponse)
async def master_chat(
    request: MasterAgentChatSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> MasterPipelineChatResponse:
    _apply_runtime_headers(http_request, request_token=request_token)
    master_pipeline = MasterPipeline(request=request, http_request=http_request)
    response_payload = await master_pipeline.consume_event_stream(request)
    return MasterPipelineChatResponse(
        content=str(response_payload.get("content", "")),
        result=response_payload["result"],
        attachment_results=response_payload.get("attachment_results"),
        tool_extra_results=response_payload.get("tool_extra_results"),
        meta=response_payload.get("meta") or {},
    )
