from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any

import httpx

TRACEPARENT_HEADER = "traceparent"

# W3C trace-context: version 00, 32-hex trace id, 16-hex span id, 2-hex flags.
_TRACEPARENT_PATTERN = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _ensure_traceparent(headers: dict[str, str]) -> dict[str, str]:
    """Propagate a valid inbound traceparent verbatim.

    An absent or malformed traceparent is left out on purpose: fabricating a
    parent span id that no span owns would create dangling traces. In that
    case map_core creates its own root SERVER span.
    """
    normalized = {key.lower(): value for key, value in headers.items()}
    inbound = normalized.get(TRACEPARENT_HEADER, "").strip()
    if _TRACEPARENT_PATTERN.match(inbound):
        merged = dict(headers)
        merged[TRACEPARENT_HEADER] = inbound
        return merged
    return {k: v for k, v in headers.items() if k.lower() != TRACEPARENT_HEADER}


class MapCoreClient:
    def __init__(self, api_origin: str) -> None:
        self.api_origin = api_origin.rstrip("/")

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            return f"{self.api_origin}{path}"
        return f"{self.api_origin}/{path}"

    async def chat_by_path(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                self._url(path),
                json=payload,
                headers=_ensure_traceparent(headers),
            )
            response.raise_for_status()
            return response.json()

    async def stream_chat_by_path(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncGenerator[bytes, None]:
        timeout = httpx.Timeout(timeout=None, connect=20.0)
        client = httpx.AsyncClient(timeout=timeout)
        request = client.build_request(
            "POST",
            self._url(path),
            json=payload,
            headers=_ensure_traceparent(headers),
        )
        response = await client.send(request, stream=True)
        response.raise_for_status()
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    async def chat(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return await self.chat_by_path("/global_domain/chat", payload, headers)

    async def stream_chat(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncGenerator[bytes, None]:
        async for chunk in self.stream_chat_by_path(
            "/global_domain/chat/stream/v2",
            payload,
            headers,
        ):
            yield chunk
