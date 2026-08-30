from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from ..schema.global_domain_schema import (
    DebugSelectSceneRequestSchema,
    GlobalDomainChatResponse,
    GlobalDomainChatSchema,
    GlobalDomainChatV3Schema,
    GlobalDomainStreamEvent,
    SceneAgentDebugRequest,
    SceneAgentDebugResponse,
    ToolAgentDebugRequest,
    ToolAgentDebugResponse,
)
from ..schema.scene_classification_schema import SceneClassificationResult
from ..service.global_domain import GlobalDomain
from ..utils.content_review.content_reviewer import build_stream_content_reviewer
from .runtime_transport import (
    apply_runtime_headers,
    format_sse_event,
    request_run_context,
)

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
    apply_runtime_headers(
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
            with request_run_context(
                http_request,
                staff_code=getattr(request, "staff_code", None),
            ):
                async for event in reviewer.moderate_event_stream(
                    event_stream,
                    request_id=global_domain.request_id,
                    state_id=global_domain.state_id,
                ):
                    yield format_sse_event(event)
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
    apply_runtime_headers(
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
            with request_run_context(
                http_request,
                staff_code=getattr(request, "staff_code", None),
            ):
                async for event in reviewer.moderate_event_stream(
                    event_stream,
                    request_id=global_domain.request_id,
                    state_id=global_domain.state_id,
                ):
                    yield format_sse_event(event)
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
    apply_runtime_headers(
        http_request,
        request_token=request_token,
    )
    global_domain = GlobalDomain(request=request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(request, "staff_code", None),
    ):
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
    apply_runtime_headers(http_request, request_token=request_token)
    chat_request = request.to_chat_request()
    global_domain = GlobalDomain(request=chat_request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(chat_request, "staff_code", None),
    ):
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
    apply_runtime_headers(http_request, request_token=request_token)
    global_domain = GlobalDomain(request=request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(request, "staff_code", None),
    ):
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
    apply_runtime_headers(http_request, request_token=request_token)
    global_domain = GlobalDomain(request=request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(request, "staff_code", None),
    ):
        return await global_domain.debug_tool_agent(request)
