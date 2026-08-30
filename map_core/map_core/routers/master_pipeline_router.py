from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from ..schema.master_pipeline_schema import (
    MasterAgentChatSchema,
    MasterPipelineChatResponse,
    MasterPipelineStreamEvent,
)
from ..service.master_pipeline import MasterPipeline
from .runtime_transport import (
    apply_runtime_headers,
    format_sse_event,
    request_run_context,
)

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


@master_pipeline_router.post(
    "/chat/stream",
    responses=STREAM_RESPONSES,
)
async def master_chat_stream(
    request: MasterAgentChatSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
):
    apply_runtime_headers(http_request, request_token=request_token)
    master_pipeline = MasterPipeline(request=request, http_request=http_request)

    async def iter_events():
        with request_run_context(
            http_request,
            staff_code=getattr(request, "staff_code", None),
        ):
            async for event in master_pipeline.pipeline_stream(request):
                yield format_sse_event(event)
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
    apply_runtime_headers(http_request, request_token=request_token)
    master_pipeline = MasterPipeline(request=request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(request, "staff_code", None),
    ):
        response_payload = await master_pipeline.consume_event_stream(request)
    return MasterPipelineChatResponse(
        content=str(response_payload.get("content", "")),
        result=response_payload["result"],
        attachment_results=response_payload.get("attachment_results"),
        tool_extra_results=response_payload.get("tool_extra_results"),
        meta=response_payload.get("meta") or {},
    )
