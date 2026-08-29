"""OpenAI-compatible provider adapter.

This is the only production module allowed to ``import openai``. It hides the
SDK behind :class:`ModelProvider` and returns only neutral dict payloads so the
engine never sees OpenAI SDK types.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger as loguru_logger
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletion
from opentelemetry.propagate import inject as otel_inject
from pydantic import ValidationError

from ...config.config_schema import LLMConfig
from .provider import (
    PreparedRequest,
    ProviderError,
    ProviderResponse,
    ProviderStream,
)


def _dump_compat(value: Any) -> Any:
    """Recursively convert SDK objects into neutral dict/list/str/number."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover
            return str(value)
    if isinstance(value, list):
        return [_dump_compat(item) for item in value]
    if isinstance(value, tuple):
        return [_dump_compat(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _dump_compat(v) for k, v in value.items()}
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _dump_compat(value.model_dump())
    if hasattr(value, "__dict__"):
        return {
            str(k): _dump_compat(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return value


def _collect_sse_data_lines(stripped: str) -> list[str]:
    data_lines: list[str] = []
    for line in stripped.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            if data_lines:
                break
            continue
        if line_stripped.startswith("data:"):
            data = line_stripped.removeprefix("data:").strip()
            if data == "[DONE]":
                continue
            data_lines.append(data)
        elif data_lines:
            data_lines.append(line)
    return data_lines


def _parse_non_stream_payload(text: str) -> dict[str, Any] | None:
    """Parse a raw non-stream completion body, including SSE-wrapped forms."""
    stripped = text.strip()
    if not stripped:
        return None

    candidates = [stripped]
    has_data_line = any(
        line.strip().startswith("data:") for line in stripped.splitlines()
    )
    if has_data_line:
        loguru_logger.warning(
            "Non-stream LLM response is wrapped as SSE data lines."
        )
        candidates.append(stripped.removeprefix("data:").strip())
        data_lines = _collect_sse_data_lines(stripped)
        if data_lines:
            candidates.append("\n".join(data_lines).strip())

    for candidate in candidates:
        if not candidate or candidate == "[DONE]":
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_chat_completion_response(response: Any) -> ChatCompletion:
    """Accept OpenAI-compatible non-stream responses wrapped as SSE text."""
    if isinstance(response, ChatCompletion):
        return response

    payload: dict[str, Any] | None = None
    if isinstance(response, bytes):
        response = response.decode("utf-8")
    if isinstance(response, str):
        payload = _parse_non_stream_payload(response)
    elif isinstance(response, dict):
        payload = response

    if payload is None:
        return response  # type: ignore[return-value]
    return ChatCompletion.model_validate(payload)


def _parse_exception_payload_candidate(candidate: Any) -> dict[str, Any] | None:
    if callable(candidate):
        candidate = candidate()
    if inspect.isawaitable(candidate):
        return None
    if isinstance(candidate, bytes):
        candidate = candidate.decode("utf-8")
    if isinstance(candidate, str):
        return _parse_non_stream_payload(candidate)
    if isinstance(candidate, dict):
        return candidate
    return None


def _iter_exception_payloads(exc: BaseException):
    response = getattr(exc, "response", None)
    candidates = [
        getattr(exc, "body", None),
        getattr(response, "text", None),
        getattr(response, "content", None),
    ]
    for candidate in candidates:
        payload = _parse_exception_payload_candidate(candidate)
        if payload is not None:
            yield payload


def _raw_response_text(raw_response: Any) -> str | None:
    text = getattr(raw_response, "text", None)
    if callable(text):
        text = text()
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if isinstance(text, str):
        return text

    content = getattr(raw_response, "content", None)
    if callable(content):
        content = content()
    if isinstance(content, bytes):
        return content.decode("utf-8")
    if isinstance(content, str):
        return content
    return None


async def _araw_response_text(raw_response: Any) -> str | None:
    text = getattr(raw_response, "text", None)
    if callable(text):
        text = text()
    if inspect.isawaitable(text):
        text = await text
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if isinstance(text, str):
        return text

    content = getattr(raw_response, "content", None)
    if callable(content):
        content = content()
    if inspect.isawaitable(content):
        content = await content
    if isinstance(content, bytes):
        return content.decode("utf-8")
    if isinstance(content, str):
        return content
    return None


def _coerce_raw_chat_completion_response(raw_response: Any) -> ChatCompletion:
    text = _raw_response_text(raw_response)
    if text is not None:
        payload = _parse_non_stream_payload(text)
        if payload is not None:
            return ChatCompletion.model_validate(payload)

    parsed = raw_response.parse()
    return _coerce_chat_completion_response(parsed)


async def _acoerce_raw_chat_completion_response(
    raw_response: Any,
) -> ChatCompletion:
    text = await _araw_response_text(raw_response)
    if text is not None:
        payload = _parse_non_stream_payload(text)
        if payload is not None:
            return ChatCompletion.model_validate(payload)

    parsed = raw_response.parse()
    if inspect.isawaitable(parsed):
        parsed = await parsed
    return _coerce_chat_completion_response(parsed)


def _coerce_chat_completion_exception(exc: BaseException) -> ChatCompletion | None:
    for payload in _iter_exception_payloads(exc):
        try:
            return ChatCompletion.model_validate(payload)
        except Exception:
            continue
    return None


def _exception_body(exc: BaseException) -> dict[str, Any] | None:
    for payload in _iter_exception_payloads(exc):
        return payload
    return None


def _to_provider_error(exc: BaseException, *, stream: bool) -> ProviderError:
    """Map SDK/network/parse failures to the provider seam error type."""
    if isinstance(exc, RateLimitError):
        return ProviderError(
            "rate_limited",
            str(exc),
            True,
            status=getattr(exc, "status_code", None),
            body=_exception_body(exc),
        )
    if isinstance(exc, (APITimeoutError, asyncio.TimeoutError)):
        return ProviderError("timeout", str(exc), True)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        retryable = bool(status is not None and status >= 500)
        return ProviderError(
            "provider_error",
            str(exc),
            retryable,
            status=status,
            body=_exception_body(exc),
        )
    if isinstance(exc, APIConnectionError):
        return ProviderError("provider_error", str(exc), True)
    if isinstance(exc, APIResponseValidationError):
        code = "stream_parse" if stream else "invalid_response"
        return ProviderError(code, str(exc), False)
    if isinstance(exc, (ValidationError, json.JSONDecodeError, UnicodeDecodeError)):
        code = "stream_parse" if stream else "invalid_response"
        return ProviderError(code, str(exc), False)
    if isinstance(exc, APIError):
        return ProviderError("provider_error", str(exc), True)
    return ProviderError(
        "provider_error" if not stream else "stream_parse",
        str(exc),
        True,
    )


class OpenAICompatibleProvider:
    """Production adapter around ``AsyncOpenAI`` (async-only)."""

    def __init__(self, config: LLMConfig, logger: Any | None = None) -> None:
        self._config = config
        self._logger = logger or loguru_logger
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=0,
            default_headers=config.extra_headers or None,
        )

    @staticmethod
    def _outbound_trace_headers() -> dict[str, str]:
        """Inject W3C traceparent into outbound LLM request headers."""
        headers: dict[str, str] = {}
        otel_inject(headers)
        return headers

    async def request(
        self, prepared: PreparedRequest
    ) -> ProviderResponse | ProviderStream:
        headers = self._outbound_trace_headers()
        if prepared.extra_headers:
            headers = {**prepared.extra_headers, **headers}
        if prepared.params.get("stream"):
            return self._stream_request(prepared, headers)
        return await self._non_stream_request(prepared, headers)

    async def _non_stream_request(
        self,
        prepared: PreparedRequest,
        headers: dict[str, str],
    ) -> ProviderResponse:
        try:
            completions = self._client.chat.completions
            raw_completions = getattr(completions, "with_raw_response", None)
            if raw_completions is not None:
                raw_response = await raw_completions.create(
                    messages=prepared.messages,
                    extra_headers=headers,
                    **prepared.params,
                )
                response = await _acoerce_raw_chat_completion_response(raw_response)
            else:
                response = await completions.create(
                    messages=prepared.messages,
                    extra_headers=headers,
                    **prepared.params,
                )
                response = _coerce_chat_completion_response(response)
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            coerced = _coerce_chat_completion_exception(exc)
            if coerced is not None:
                return ProviderResponse(payload=_dump_compat(coerced))
            raise _to_provider_error(exc, stream=False) from exc
        return ProviderResponse(payload=_dump_compat(response))

    def _stream_request(
        self,
        prepared: PreparedRequest,
        headers: dict[str, str],
    ) -> ProviderStream:
        stream = ProviderStream(chunks=None)  # type: ignore[arg-type]

        async def _chunks() -> AsyncIterator[dict[str, Any]]:
            try:
                response = await self._client.chat.completions.create(
                    messages=prepared.messages,
                    extra_headers=headers,
                    **prepared.params,
                )
                async for chunk in response:
                    if isinstance(chunk, str) and chunk.strip() == "[DONE]":
                        break
                    yield _dump_compat(chunk)
                stream.complete = True
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                raise _to_provider_error(exc, stream=True) from exc

        stream.chunks = _chunks()
        return stream

    async def aclose(self) -> None:
        await self._client.close()
