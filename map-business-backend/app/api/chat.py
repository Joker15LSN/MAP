"""Chat endpoints (legacy contract, unchanged during F-01).

These routes keep their exact URLs, request/response shapes and SSE event
names. The only F-01 change is dependency injection of the store and core
client instead of module-level singletons.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from ..core_client import MapCoreClient
from ..repositories.config import ConfigRepository
from ..schemas import ChatRequest
from ..services.runtime_payloads import build_runtime_chat_payload
from .deps import get_core_client, get_store

router = APIRouter()


def _forward_headers(
    request_token: str | None,
    request: Request,
) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if request_token:
        headers["X-request-token"] = request_token
    if request.headers.get("X-UserId"):
        headers["X-UserId"] = request.headers["X-UserId"]
    if request.headers.get("X-UserName"):
        headers["X-UserName"] = request.headers["X-UserName"]
    # F-04: propagate the BFF-owned IDs so map_core / Mongo / OTel join the
    # same request/session/workspace identity.
    if getattr(request.state, "request_id", None):
        headers["X-Request-ID"] = request.state.request_id
    if getattr(request.state, "session_id", None):
        headers["X-Session-ID"] = request.state.session_id
    if getattr(request.state, "workspace_id", None):
        headers["X-Workspace-ID"] = request.state.workspace_id
    # Forward inbound W3C propagation headers so an existing upstream trace
    # continues even when OTel is disabled. With OTel enabled the httpx
    # instrumentation additionally injects a dynamic traceparent referencing
    # the BFF CLIENT span (overwriting these values at send time), so
    # map_core always joins the trace owned by the BFF SERVER span.
    for propagation_header in ("traceparent", "tracestate", "baggage"):
        value = request.headers.get(propagation_header)
        if value:
            headers[propagation_header] = value
    return headers


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "map-business-backend"}


@router.post("/api/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    store: ConfigRepository = Depends(get_store),
    core_client: MapCoreClient = Depends(get_core_client),
) -> dict[str, Any]:
    headers = _forward_headers(request_token, request)
    request_payload = build_runtime_chat_payload(store, payload)
    try:
        return await core_client.chat(
            request_payload,
            headers=headers,
        )
    except Exception as exc:
        return {
            "content": (
                "MAP 算法服务当前不可用，业务后端已捕获该异常。"
                "你仍可继续查看前后台页面和管理配置。"
            ),
            "meta": {
                "fallback": True,
                "upstream_error": str(exc),
            },
        }


@router.post("/api/chat/stream/v2")
async def chat_stream_v2(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    store: ConfigRepository = Depends(get_store),
    core_client: MapCoreClient = Depends(get_core_client),
) -> StreamingResponse:
    headers = _forward_headers(request_token, request)
    request_payload = build_runtime_chat_payload(store, payload)

    async def stream() -> Any:
        try:
            async for chunk in core_client.stream_chat(
                request_payload,
                headers=headers,
            ):
                yield chunk
        except Exception as exc:
            error_data = json.dumps(
                {
                    "error": f"MAP 算法服务不可用: {exc}",
                    "fallback": True,
                },
                ensure_ascii=False,
            )
            done_data = json.dumps(
                {
                    "content": (
                        "MAP 算法服务当前不可用，已自动回退到业务后端兜底响应。"
                    ),
                    "meta": {"fallback": True},
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode("utf-8")
            yield f"event: done\ndata: {done_data}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/chat/flow/v1")
async def chat_flow_v1(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    store: ConfigRepository = Depends(get_store),
    core_client: MapCoreClient = Depends(get_core_client),
) -> dict[str, Any]:
    headers = _forward_headers(request_token, request)
    request_payload = build_runtime_chat_payload(store, payload)
    try:
        return await core_client.chat_by_path(
            "/flow_domain/chat/v1",
            request_payload,
            headers=headers,
        )
    except Exception as exc:
        return {
            "content": (
                "MAP 心流算法服务当前不可用，业务后端已捕获该异常。"
                "你仍可继续使用全域模式。"
            ),
            "meta": {
                "fallback": True,
                "upstream_error": str(exc),
                "mode": "flow",
            },
        }


@router.post("/api/chat/stream/flow/v1")
async def chat_stream_flow_v1(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    store: ConfigRepository = Depends(get_store),
    core_client: MapCoreClient = Depends(get_core_client),
) -> StreamingResponse:
    headers = _forward_headers(request_token, request)
    request_payload = build_runtime_chat_payload(store, payload)

    async def stream() -> Any:
        try:
            async for chunk in core_client.stream_chat_by_path(
                "/flow_domain/chat/stream/v1",
                request_payload,
                headers=headers,
            ):
                yield chunk
        except Exception as exc:
            error_data = json.dumps(
                {
                    "error": f"MAP 心流算法服务不可用: {exc}",
                    "fallback": True,
                    "mode": "flow",
                },
                ensure_ascii=False,
            )
            done_data = json.dumps(
                {
                    "content": (
                        "MAP 心流算法服务当前不可用，已自动回退到业务后端兜底响应。"
                    ),
                    "meta": {"fallback": True, "mode": "flow"},
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode("utf-8")
            yield f"event: done\ndata: {done_data}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
