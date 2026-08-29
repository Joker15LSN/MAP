"""Internal provider seam for ModelInvocation.

This module is an implementation detail: contract tests may implement
``ModelProvider`` to script provider behavior, but production callers should
not depend on these names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol


class ProviderError(Exception):
    code: Literal[
        "rate_limited",
        "timeout",
        "provider_error",
        "stream_parse",
        "invalid_response",
    ]

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        *,
        status: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.body = body


class PreparedRequest:
    def __init__(
        self,
        *,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.messages = messages
        self.params = params
        self.extra_headers: dict[str, str] = dict(extra_headers or {})


class ProviderResponse:
    payload: dict[str, Any]  # 非流 completion 的中性 dict

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class ProviderStream:
    chunks: AsyncIterator[dict[str, Any]]
    complete: bool  # 正常看到终止时置 True（由 adapter 或消费方判定）

    def __init__(
        self,
        chunks: AsyncIterator[dict[str, Any]],
        *,
        complete: bool = False,
    ) -> None:
        self.chunks = chunks
        self.complete = complete


class ModelProvider(Protocol):
    async def request(
        self, prepared: PreparedRequest
    ) -> ProviderResponse | ProviderStream: ...
