from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx


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
                headers=headers,
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
            headers=headers,
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
