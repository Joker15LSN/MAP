import json
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..schema.global_domain_schema import (
    DebugSelectSceneRequestSchema,
    GlobalDomainChatResponse,
    GlobalDomainChatSchema,
    GlobalDomainChatV3Schema,
    GlobalDomainDemoResponse,
    GlobalDomainStreamEvent,
    SceneAgentDebugRequest,
    SceneAgentDebugResponse,
    ToolAgentDebugRequest,
    ToolAgentDebugResponse,
)
from ..schema.scene_classification_schema import SceneClassificationResult
from ..service.global_domain import GlobalDomain
from ..utils.content_review.content_reviewer import build_stream_content_reviewer

global_domain_router = APIRouter(prefix="/global_domain")


# Schema for streaming response in v2.
STREAM_V2_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "SSE stream. Each frame contains event name and JSON payload in data line.",
        "content": {
            "text/event-stream": {
                "schema": GlobalDomainStreamEvent.model_json_schema(),
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


def _format_sse_event(event: GlobalDomainStreamEvent) -> str:
    payload = (
        event.data.model_dump() if isinstance(event.data, BaseModel) else event.data
    )
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.event}\ndata: {data}\n\n"


def _format_debug_scene_agents(
    result: SceneClassificationResult,
    agent_name_map: dict[str, str],
) -> list[str]:
    formatted: list[str] = []
    seen: set[str] = set()
    default_agent_names = {"General_Assistant": "通用助手"}

    for sub_scene_result in result.sub_scenes:
        for agent_code in sub_scene_result.sub_scenes:
            if agent_code in seen:
                continue
            seen.add(agent_code)
            formatted.append(
                agent_name_map.get(
                    agent_code,
                    default_agent_names.get(agent_code, agent_code),
                )
            )

    return formatted


@global_domain_router.post(
    "/chat/stream/v2",
    responses=STREAM_V2_RESPONSES,
)
async def chat_stream_v2(
    request: GlobalDomainChatSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
):
    _apply_runtime_headers(
        http_request,
        request_token=request_token,
    )
    global_domain = GlobalDomain(request=request, http_request=http_request)
    event_stream = global_domain.pipeline_stream(request)
    reviewer = build_stream_content_reviewer(
        enabled=request.content_review_enabled,
        company_policy_instruction=request.content_review_company_policy_instruction,
    )

    async def iter_events():
        try:
            async for event in reviewer.moderate_event_stream(
                event_stream,
                request_id=global_domain.request_id,
                state_id=global_domain.state_id,
            ):
                yield _format_sse_event(event)
            http_request.state._stream_logically_completed = True
        finally:
            await reviewer.aclose()

    return StreamingResponse(
        iter_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@global_domain_router.post(
    "/chat/stream/v3",
    responses=STREAM_V2_RESPONSES,
)
async def chat_stream_v3(
    request: GlobalDomainChatV3Schema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
):
    _apply_runtime_headers(
        http_request,
        request_token=request_token,
    )
    global_domain = GlobalDomain(request=request, http_request=http_request)
    event_stream = global_domain.pipeline_stream(request)
    reviewer = build_stream_content_reviewer(
        enabled=request.content_review_enabled,
        company_policy_instruction=request.content_review_company_policy_instruction,
    )

    async def iter_events():
        try:
            async for event in reviewer.moderate_event_stream(
                event_stream,
                request_id=global_domain.request_id,
                state_id=global_domain.state_id,
            ):
                yield _format_sse_event(event)
            http_request.state._stream_logically_completed = True
        finally:
            await reviewer.aclose()

    return StreamingResponse(
        iter_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@global_domain_router.post("/chat", response_model=GlobalDomainChatResponse)
async def chat(
    request: GlobalDomainChatSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> GlobalDomainChatResponse:
    _apply_runtime_headers(
        http_request,
        request_token=request_token,
    )
    global_domain = GlobalDomain(request=request, http_request=http_request)
    response_payload = await global_domain.consume_event_stream(request)
    return GlobalDomainChatResponse(
        content=str(response_payload.get("content", "")),
        attachment_results=response_payload.get("attachment_results"),
        tool_extra_results=response_payload.get("tool_extra_results"),
    )


@global_domain_router.post(
    "/debug/select_scene", response_model=list[str]
)
async def debug_select_scene(
    request: DebugSelectSceneRequestSchema,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> list[str]:
    _apply_runtime_headers(http_request, request_token=request_token)
    chat_request = request.to_chat_request()
    global_domain = GlobalDomain(request=chat_request, http_request=http_request)
    result = await global_domain.select_scene(request=chat_request)
    agent_name_map = global_domain._resolve_scene_selection_agent_name_map(chat_request)
    return _format_debug_scene_agents(result, agent_name_map)


@global_domain_router.post(
    "/debug/scene_agent/run",
    response_model=SceneAgentDebugResponse,
)
async def debug_run_scene_agent(
    request: SceneAgentDebugRequest,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> SceneAgentDebugResponse:
    _apply_runtime_headers(http_request, request_token=request_token)
    global_domain = GlobalDomain(request=request, http_request=http_request)
    return await global_domain.debug_scene_agent(request)


@global_domain_router.post(
    "/debug/tool_agent/run",
    response_model=ToolAgentDebugResponse,
)
async def debug_run_tool_agent(
    request: ToolAgentDebugRequest,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> ToolAgentDebugResponse:
    _apply_runtime_headers(http_request, request_token=request_token)
    global_domain = GlobalDomain(request=request, http_request=http_request)
    return await global_domain.debug_tool_agent(request)


# @global_domain_router.post("/demo", response_model=GlobalDomainDemoResponse)
# async def demo(
#     request: GlobalDomainChatSchema, http_request: Request
# ) -> GlobalDomainDemoResponse:
#     global_domain = GlobalDomain(request=request, http_request=http_request)
#     result = await global_domain.demo(request)
#     return result

# @global_domain_router.post("/chat/stream")
# async def chat_stream(request: GlobalDomainChatSchema, http_request: Request):
#     global_domain = GlobalDomain(request=request, http_request=http_request)
#     _, _, stream = await global_domain.pipeline_stream(request)
#     return StreamingResponse(iter_chunks(), media_type="text/plain")
