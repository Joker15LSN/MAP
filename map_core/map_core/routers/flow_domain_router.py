import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from ..schema.flow_domain_schema import (
    FlowChatRequest,
    FlowDomainChatResponse,
    FlowDomainStreamEvent,
)
from ..service.flow_domain import FlowDomain
from ..utils.content_review.content_reviewer import build_stream_content_reviewer
from .runtime_transport import (
    apply_runtime_headers,
    format_sse_event,
    request_run_context,
)

flow_domain_router = APIRouter(prefix="/flow_domain")

FLOW_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "SSE stream. Each frame contains event name and JSON payload in data line.",
        "content": {
            "text/event-stream": {
                "schema": FlowDomainStreamEvent.model_json_schema(),
            }
        },
    }
}


_RUNTIME_SNAPSHOT_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{1,128}$")
_RUNTIME_SNAPSHOT_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _runtime_snapshot_id_value(raw: str | None) -> str | None:
    """Return a sane pinned snapshot id: UUID or hex string of <=128 chars."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > 128:
        return None
    try:
        UUID(value)
        return value
    except ValueError:
        pass
    if _RUNTIME_SNAPSHOT_HEX_PATTERN.fullmatch(value):
        return value
    return None


def _runtime_snapshot_digest_value(raw: str | None) -> str | None:
    """Return a 64-char lowercase/uppercase hex digest, else None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not _RUNTIME_SNAPSHOT_DIGEST_PATTERN.fullmatch(value):
        return None
    return value


@flow_domain_router.post(
    "/chat/stream/v1",
    responses=FLOW_STREAM_RESPONSES,
)
async def chat_stream_v1(
    request: FlowChatRequest,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
):
    apply_runtime_headers(http_request, request_token=request_token)
    # J6: pinned runtime snapshot identity comes from the BFF. Malformed or
    # missing headers keep None here; the provider fails closed at the call
    # site, never by fabricating a current-pointer fallback.
    http_request.state.runtime_snapshot_id = _runtime_snapshot_id_value(
        http_request.headers.get("X-Runtime-Snapshot-ID")
    )
    http_request.state.runtime_snapshot_digest = _runtime_snapshot_digest_value(
        http_request.headers.get("X-Runtime-Snapshot-Digest")
    )
    flow_domain = FlowDomain(request=request, http_request=http_request)
    event_stream = flow_domain.pipeline_stream(request)
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
                    request_id=flow_domain.request_id,
                    state_id=flow_domain.state_id,
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


@flow_domain_router.post("/chat/v1", response_model=FlowDomainChatResponse)
async def chat_v1(
    request: FlowChatRequest,
    http_request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> FlowDomainChatResponse:
    apply_runtime_headers(http_request, request_token=request_token)
    # J6: pinned runtime snapshot identity comes from the BFF. Malformed or
    # missing headers keep None here; the provider fails closed at the call
    # site, never by fabricating a current-pointer fallback.
    http_request.state.runtime_snapshot_id = _runtime_snapshot_id_value(
        http_request.headers.get("X-Runtime-Snapshot-ID")
    )
    http_request.state.runtime_snapshot_digest = _runtime_snapshot_digest_value(
        http_request.headers.get("X-Runtime-Snapshot-Digest")
    )
    flow_domain = FlowDomain(request=request, http_request=http_request)
    with request_run_context(
        http_request,
        staff_code=getattr(request, "staff_code", None),
    ):
        response_payload = await flow_domain.consume_event_stream(request)
    return FlowDomainChatResponse(
        content=str(response_payload.get("content", "")),
        attachment_results=response_payload.get("attachment_results"),
        tool_extra_results=response_payload.get("tool_extra_results"),
        meta=response_payload.get("meta") or {},
    )
