"""Single async typed ModelInvocation interface (design A).

This module owns retry, structured-output fallback, usage, OTel spans and
llm_call event recording. Provider SDK access is delegated to the
OpenAI-compatible adapter (or a scripted mock in tests).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, overload
from zoneinfo import ZoneInfo

from loguru import logger as loguru_logger
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace import StatusCode as OtelStatusCode
from tenacity import wait_exponential

from ...config.config_schema import LLMConfig
from ..llm_trace_context import (
    get_llm_trace_context,
    now_shanghai,
    summarize_llm_messages,
)
from .openai_compatible import OpenAICompatibleProvider, _dump_compat
from .provider import (
    ModelProvider,
    PreparedRequest,
    ProviderError,
    ProviderResponse,
    ProviderStream,
)
from .types import (
    ModelInvocationError,
    ModelInvocationEvent,
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelMessage,
    ModelUsage,
    ProviderParams,
    ToolSpec,
)

JSON_SCHEMA_UNAVAILABLE_MESSAGE = "This response_format type is unavailable now"
JSON_OBJECT_OUTPUT_INSTRUCTION = "Use JSON format as output."
JSON_OBJECT_SCHEMA_INSTRUCTION = "Follow this JSON schema:"


class _RetryStateStub:
    def __init__(self, attempt_number: int) -> None:
        self.attempt_number = attempt_number


def datetime_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=ZoneInfo("Asia/Shanghai"))


class ModelInvocationStream:
    """Async iterator of :class:`ModelInvocationEvent`.

    The underlying generator owns the OTel span for the whole consumption
    lifecycle (request + chunk iteration) so the span always ends on success,
    failure, cancellation or close.
    """

    def __init__(self, agen: AsyncGenerator[ModelInvocationEvent, None]) -> None:
        self._agen = agen

    def __aiter__(self) -> "ModelInvocationStream":
        return self

    async def __anext__(self) -> ModelInvocationEvent:
        return await self._agen.__anext__()

    async def aclose(self) -> None:
        await self._agen.aclose()


class ModelInvocation:
    """Typed async entry point for OpenAI-compatible model invocations."""

    def __init__(
        self,
        config: LLMConfig,
        logger: Any | None = None,
        *,
        provider: ModelProvider | None = None,
        before_retry: Callable[[int, BaseException | None], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or loguru_logger
        self.before_retry = before_retry
        self._provider = provider or OpenAICompatibleProvider(config, self.logger)
        self._retry_wait = wait_exponential(multiplier=1, min=1, max=8)

    @classmethod
    def from_config(
        cls, config: LLMConfig, logger: Any | None = None
    ) -> "ModelInvocation":
        return cls(config, logger)

    @overload
    async def invoke(
        self, req: ModelInvocationRequest[Literal[False]]
    ) -> ModelInvocationOutcome: ...

    @overload
    async def invoke(
        self, req: ModelInvocationRequest[Literal[True]]
    ) -> ModelInvocationStream: ...

    async def invoke(
        self, req: ModelInvocationRequest[Any]
    ) -> ModelInvocationOutcome | ModelInvocationStream:
        if req.stream is True:
            return ModelInvocationStream(self._stream_events(req))
        return await self._invoke_non_stream(req)

    async def aclose(self) -> None:
        aclose = getattr(self._provider, "aclose", None)
        if aclose is not None:
            await aclose()

    async def __aenter__(self) -> "ModelInvocation":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Non-stream path
    # ------------------------------------------------------------------
    async def _invoke_non_stream(
        self, req: ModelInvocationRequest[Any]
    ) -> ModelInvocationOutcome:
        started = time.time()
        try:
            msgs = self._normalize_messages(req.messages)
        except (TypeError, ValueError) as exc:
            return self._failed_outcome(
                "invalid_request", str(exc), attempts=0, started=started
            )
        if not msgs:
            return self._failed_outcome(
                "invalid_request", "messages must not be empty", 0, started
            )
        if req.tools is not None and req.structured is not None:
            return self._failed_outcome(
                "invalid_request",
                "structured and tools are mutually exclusive",
                0,
                started,
            )
        if self._is_cancelled(req):
            return self._cancelled_outcome(attempts=0, started=started)

        params = self._build_params(req, stream=False)
        call_kind = self._call_kind(req, stream=False)
        attempt = 0
        max_attempts = self.config.max_retries + 1
        while attempt < max_attempts:
            if self._is_cancelled(req):
                return self._cancelled_outcome(attempts=attempt, started=started)
            attempt += 1
            with self._llm_outbound_span(call_kind) as span:
                outcome, retry_exc = await self._request_once(
                    req=req,
                    msgs=msgs,
                    params=params,
                    started=started,
                    attempts=attempt,
                    call_kind=call_kind,
                    otel_span=span,
                )
            if outcome.status == "succeeded" or outcome.error is None:
                return outcome
            if outcome.error.retryable and attempt < max_attempts:
                self._call_before_retry(attempt, retry_exc)
                await self._sleep_before_retry(attempt)
                continue
            return outcome
        return self._failed_outcome(
            "unknown", "retry logic exhausted", attempt, started
        )

    async def _request_once(
        self,
        *,
        req: ModelInvocationRequest[Any],
        msgs: list[dict[str, Any]],
        params: dict[str, Any],
        started: float,
        attempts: int,
        call_kind: str,
        otel_span: Span,
    ) -> tuple[ModelInvocationOutcome, BaseException | None]:
        try:
            response = await self._provider.request(
                PreparedRequest(messages=[dict(m) for m in msgs], params=params)
            )
            outcome = self._response_to_outcome(
                response, req, started=started, attempts=attempts, params=params
            )
            self._record_outcome(
                outcome=outcome,
                messages=msgs,
                params=params,
                started=started,
                call_kind=call_kind,
                otel_span=otel_span,
            )
            return outcome, None
        except ProviderError as exc:
            if self._should_fallback_to_json_object(exc, params):
                self.logger.warning(
                    "LLM upstream does not support json_schema response_format; "
                    "retrying once with json_object."
                )
                fallback_msgs, fallback_params = self._build_json_object_fallback_request(
                    msgs, params
                )
                try:
                    response = await self._provider.request(
                        PreparedRequest(
                            messages=[dict(m) for m in fallback_msgs],
                            params=fallback_params,
                        )
                    )
                    outcome = self._response_to_outcome(
                        response,
                        req,
                        started=started,
                        attempts=attempts,
                        params=fallback_params,
                    )
                    self._record_outcome(
                        outcome=outcome,
                        messages=fallback_msgs,
                        params=fallback_params,
                        started=started,
                        call_kind=call_kind,
                        otel_span=otel_span,
                    )
                    return outcome, None
                except ProviderError as fallback_error:
                    self._record_llm_call(
                        messages=fallback_msgs,
                        params=fallback_params,
                        started_at=started,
                        status="failed",
                        call_kind=call_kind,
                        error=fallback_error,
                        otel_span=otel_span,
                    )
                    return (
                        self._outcome_from_provider_error(
                            fallback_error, attempts, started
                        ),
                        fallback_error,
                    )
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="failed",
                call_kind=call_kind,
                error=exc,
                otel_span=otel_span,
            )
            return self._outcome_from_provider_error(exc, attempts, started), exc

    def _response_to_outcome(
        self,
        response: ProviderResponse | ProviderStream,
        req: ModelInvocationRequest[Any],
        *,
        started: float,
        attempts: int,
        params: dict[str, Any],
    ) -> ModelInvocationOutcome:
        if isinstance(response, ProviderStream):
            return self._failed_outcome(
                "invalid_response",
                "provider returned a stream for a non-stream request",
                attempts,
                started,
            )
        payload = _dump_compat(response.payload)
        if not isinstance(payload, dict):
            return self._failed_outcome(
                "invalid_response", "provider response is not a dict", attempts, started
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            # Frozen legacy ask_tool semantics: a provider that answers a tool
            # request with no choices is an empty success, not an error.
            if req.tools is not None:
                return self._empty_tool_outcome(
                    payload, req, started=started, attempts=attempts
                )
            return self._failed_outcome(
                "invalid_response", "provider response has no choices", attempts, started
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return self._failed_outcome(
                "invalid_response", "provider choice is not a dict", attempts, started
            )
        message = first_choice.get("message")
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        structured: Any = None
        if req.structured is not None and req.structured.parse:
            try:
                structured = json.loads(content)
            except json.JSONDecodeError:
                return self._failed_outcome(
                    "structured_refused",
                    "model returned content that is not valid JSON",
                    attempts,
                    started,
                )

        tool_calls = self._normalize_tool_calls(message.get("tool_calls"))
        usage = self._usage_from_mapping(payload.get("usage"))
        finish_reason = first_choice.get("finish_reason")
        model = payload.get("model") or req.model or self.config.model
        request_id = payload.get("id")
        latency_ms = (time.time() - started) * 1000.0

        # Legacy parity: a provider response with choices and an empty assistant
        # message is a valid (empty) success, never a failure.
        return ModelInvocationOutcome(
            status="succeeded",
            content=content,
            reasoning_content=self._extract_reasoning_content(message),
            tool_calls=tool_calls,
            structured=structured,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            request_id=request_id,
            attempts=attempts,
            latency_ms=latency_ms,
            raw=payload if req.raw_compat else None,
        )

    def _empty_tool_outcome(
        self,
        payload: dict[str, Any],
        req: ModelInvocationRequest[Any],
        *,
        started: float,
        attempts: int,
    ) -> ModelInvocationOutcome:
        return ModelInvocationOutcome(
            status="succeeded",
            content="",
            tool_calls=[],
            usage=self._usage_from_mapping(payload.get("usage")),
            model=payload.get("model") or req.model or self.config.model,
            request_id=payload.get("id"),
            attempts=attempts,
            latency_ms=(time.time() - started) * 1000.0,
            raw=payload if req.raw_compat else None,
        )

    def _record_outcome(
        self,
        *,
        outcome: ModelInvocationOutcome,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        started: float,
        call_kind: str,
        otel_span: Span,
    ) -> None:
        status = "success" if outcome.status == "succeeded" else "failed"
        self._record_llm_call(
            messages=messages,
            params=params,
            started_at=started,
            status=status,
            call_kind=call_kind,
            outcome=outcome,
            otel_span=otel_span,
        )

    # ------------------------------------------------------------------
    # Stream path
    # ------------------------------------------------------------------
    async def _stream_events(
        self, req: ModelInvocationRequest[Any]
    ) -> AsyncGenerator[ModelInvocationEvent, None]:
        started = time.time()
        try:
            msgs = self._normalize_messages(req.messages)
        except (TypeError, ValueError) as exc:
            yield self._terminal_event(
                "failed", 0, None, started,
                self._error("invalid_request", str(exc), False),
            )
            return
        if not msgs:
            yield self._terminal_event(
                "failed", 0, None, started,
                self._error("invalid_request", "messages must not be empty", False),
            )
            return
        if req.tools is not None and req.structured is not None:
            yield self._terminal_event(
                "failed", 0, None, started,
                self._error(
                    "invalid_request",
                    "structured and tools are mutually exclusive",
                    False,
                ),
            )
            return
        if self._is_cancelled(req):
            yield self._terminal_event(
                "cancelled", 0, None, started,
                self._error("cancelled", "cancelled before invocation", False),
            )
            return

        params = self._build_params(req, stream=True)
        call_kind = self._call_kind(req, stream=True)
        attempt = 0
        max_attempts = self.config.max_retries + 1
        while attempt < max_attempts:
            if self._is_cancelled(req):
                yield self._terminal_event(
                    "cancelled", attempt, None, started,
                    self._error("cancelled", "cancelled before attempt", False),
                )
                return
            attempt += 1
            result: dict[str, Any] = {}
            async for event in self._stream_attempt(
                req=req,
                msgs=msgs,
                params=params,
                started=started,
                attempt=attempt,
                call_kind=call_kind,
                max_attempts=max_attempts,
                result=result,
            ):
                yield event
            if result.get("retry"):
                continue
            terminal = result.get("terminal")
            if terminal is not None:
                yield terminal
                return

    async def _stream_attempt(
        self,
        *,
        req: ModelInvocationRequest[Any],
        msgs: list[dict[str, Any]],
        params: dict[str, Any],
        started: float,
        attempt: int,
        call_kind: str,
        max_attempts: int,
        result: dict[str, Any],
    ) -> AsyncGenerator[ModelInvocationEvent, None]:
        span = self._start_llm_span(call_kind)
        token = otel_context.attach(otel_trace.set_span_in_context(span))
        try:
            try:
                provider_response = await self._provider.request(
                    PreparedRequest(messages=[dict(m) for m in msgs], params=params)
                )
            except ProviderError as exc:
                self._record_llm_call(
                    messages=msgs,
                    params=params,
                    started_at=started,
                    status="failed",
                    call_kind=call_kind,
                    error=exc,
                    otel_span=span,
                )
                self._fail_llm_span(span, exc)
                if exc.retryable and attempt < max_attempts:
                    self._call_before_retry(attempt, exc)
                    await self._sleep_before_retry(attempt)
                    result["retry"] = True
                    return
                result["terminal"] = self._terminal_event(
                    "failed", attempt, None, started,
                    self._provider_error_to_outcome_error(exc),
                )
                return
        finally:
            otel_context.detach(token)

        if isinstance(provider_response, ProviderResponse):
            err = self._error(
                "invalid_response",
                "provider returned a non-stream response for a stream request",
                False,
            )
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="failed",
                call_kind=call_kind,
                error=err,
                otel_span=span,
            )
            self._fail_llm_span(span, RuntimeError(err.message))
            result["terminal"] = self._terminal_event(
                "failed", attempt, None, started, err
            )
            return

        final: dict[str, Any] = {}
        try:
            async for event in self._consume_stream_attempt(
                provider_response, req=req, final=final
            ):
                yield event
        except ProviderError as exc:
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="failed",
                call_kind=call_kind,
                error=exc,
                otel_span=span,
            )
            self._fail_llm_span(span, exc)
            if exc.retryable and attempt < max_attempts:
                self._call_before_retry(attempt, exc)
                await self._sleep_before_retry(attempt)
                result["retry"] = True
                return
            result["terminal"] = self._terminal_event(
                "failed", attempt, None, started,
                self._provider_error_to_outcome_error(exc),
            )
            return
        except asyncio.CancelledError as exc:
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="failed",
                call_kind=call_kind,
                error=exc,
                otel_span=span,
            )
            self._fail_llm_span(span, exc)
            raise
        except GeneratorExit as exc:
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="failed",
                call_kind=call_kind,
                error=exc,
                otel_span=span,
            )
            self._fail_llm_span(span, exc)
            raise

        self._finish_stream_attempt(
            final=final,
            span=span,
            msgs=msgs,
            params=params,
            started=started,
            attempt=attempt,
            call_kind=call_kind,
            result=result,
        )

    def _finish_stream_attempt(
        self,
        *,
        final: dict[str, Any],
        span: Span,
        msgs: list[dict[str, Any]],
        params: dict[str, Any],
        started: float,
        attempt: int,
        call_kind: str,
        result: dict[str, Any],
    ) -> None:
        status = final.get("status")
        usage = final.get("usage")
        if status == "succeeded":
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="success",
                call_kind=call_kind,
                usage=usage,
                otel_span=span,
            )
            self._end_llm_span(span, usage=usage)
            result["terminal"] = self._terminal_event(
                "succeeded", attempt, usage, started, None
            )
            return
        if status == "cancelled":
            error = final.get("error") or self._error(
                "cancelled", "cancelled during stream", False
            )
            self._record_llm_call(
                messages=msgs,
                params=params,
                started_at=started,
                status="cancelled",
                call_kind=call_kind,
                error=error,
                otel_span=span,
            )
            self._fail_llm_span(span, RuntimeError(error.message))
            result["terminal"] = self._terminal_event(
                "cancelled", attempt, usage, started, error
            )
            return
        # Clean but incomplete stream => unknown, never fake success.
        error = self._error(
            "unknown", "stream ended before provider completion", False
        )
        self._record_llm_call(
            messages=msgs,
            params=params,
            started_at=started,
            status="unknown",
            call_kind=call_kind,
            error=error,
            otel_span=span,
        )
        self._fail_llm_span(span, RuntimeError(error.message))
        result["terminal"] = self._terminal_event(
            "unknown", attempt, usage, started, error
        )

    async def _consume_stream_attempt(
        self,
        provider_stream: ProviderStream,
        *,
        req: ModelInvocationRequest[Any],
        final: dict[str, Any],
    ) -> AsyncGenerator[ModelInvocationEvent, None]:
        usage: ModelUsage | None = None
        async for raw_chunk in provider_stream.chunks:
            if self._is_cancelled(req):
                final["status"] = "cancelled"
                final["usage"] = usage
                final["error"] = self._error(
                    "cancelled", "cancelled during stream", False
                )
                return
            chunk = _dump_compat(raw_chunk)
            if not isinstance(chunk, dict):
                raise ProviderError(
                    "stream_parse", "stream chunk is not a dict", False
                )
            for event_dict in self._stream_chunk_events(chunk):
                if event_dict["type"] == "usage":
                    usage = self._usage_from_mapping(event_dict.get("data"))
                yield ModelInvocationEvent(type=event_dict["type"], data=event_dict)
        final["usage"] = usage
        final["status"] = "succeeded" if provider_stream.complete else "unknown"

    def _stream_chunk_events(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        if not chunk.get("choices"):
            if chunk.get("usage"):
                usage_dict = self._usage_mapping_from_chunk(chunk)
                if usage_dict:
                    return [{"type": "usage", "data": usage_dict}]
            return []
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return []
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        events: list[dict[str, Any]] = []
        has_content_event = (
            "content" in delta
            or "role" in delta
            or "logprobs" in first_choice
        )
        if has_content_event:
            events.append(
                {
                    "type": "content",
                    "data": "" if delta.get("content") is None else str(delta.get("content")),
                    "id": chunk.get("id"),
                    "object": chunk.get("object"),
                    "created": chunk.get("created"),
                    "model": chunk.get("model"),
                    "choices": chunk.get("choices"),
                    "prompt_token_ids": chunk.get("prompt_token_ids"),
                    "logprobs": first_choice.get("logprobs"),
                    "raw_chunk": chunk,
                }
            )
        reasoning_chunk = self._extract_reasoning_content(delta)
        if reasoning_chunk:
            events.append({"type": "reasoning", "data": reasoning_chunk})
        return events

    # ------------------------------------------------------------------
    # Message normalization / params
    # ------------------------------------------------------------------
    def _normalize_messages(
        self, messages: Any
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, ModelMessage):
                prepared.append(
                    self._normalize_message_dict(msg.model_dump(exclude_none=True))
                )
            elif isinstance(msg, dict):
                prepared.append(self._normalize_message_dict(msg))
            else:
                raise TypeError(f"Unsupported message type: {type(msg)}")
        return prepared

    def _normalize_message_dict(self, msg: dict[str, Any]) -> dict[str, Any]:
        if "role" not in msg:
            raise ValueError("Message dict must contain 'role'")
        role = msg.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"Unsupported message role: {role!r}")
        normalized: dict[str, Any] = dict(msg)

        if role == "tool":
            if "tool_call_id" not in normalized and "call_id" in normalized:
                normalized["tool_call_id"] = normalized.get("call_id")
            if "content" not in normalized and "output" in normalized:
                normalized["content"] = normalized.get("output")

        if "content" not in normalized:
            if role == "assistant" and (
                "tool_calls" in normalized or "function_call" in normalized
            ):
                normalized["content"] = ""
            else:
                raise ValueError(
                    "Message dict must contain 'content' unless it is an assistant tool-calling message"
                )
        if role == "tool" and "tool_call_id" not in normalized:
            raise ValueError(
                "Tool message dict must contain 'tool_call_id' and 'content'"
            )
        if normalized.get("content") is None:
            if role == "assistant":
                normalized["content"] = ""
            else:
                raise ValueError(
                    "Message dict 'content' cannot be None for non-assistant roles"
                )
        elif not isinstance(normalized.get("content"), str):
            normalized["content"] = str(normalized.get("content"))

        allowed_keys = self._allowed_message_keys(role)
        return {k: v for k, v in normalized.items() if k in allowed_keys}

    @staticmethod
    def _allowed_message_keys(role: str) -> set[str]:
        keys_by_role = {
            "system": {"role", "content", "name"},
            "user": {"role", "content", "name"},
            "assistant": {"role", "content", "name", "tool_calls", "function_call"},
            "tool": {"role", "content", "tool_call_id"},
        }
        return keys_by_role[role]

    def _build_params(
        self, req: ModelInvocationRequest[Any], *, stream: bool
    ) -> dict[str, Any]:
        provider_params = req.provider_params or ProviderParams()
        params: dict[str, Any] = {
            "model": req.model or self.config.model,
            "temperature": (
                req.temperature
                if req.temperature is not None
                else self.config.temperature
            ),
            "logprobs": (
                provider_params.logprobs
                if provider_params.logprobs is not None
                else self.config.logprobs
            ),
            "top_logprobs": (
                provider_params.top_logprobs
                if provider_params.top_logprobs is not None
                else self.config.top_logprobs
            ),
            "max_tokens": (
                req.max_tokens if req.max_tokens is not None else self.config.max_tokens
            ),
            "top_p": (
                provider_params.top_p
                if provider_params.top_p is not None
                else self.config.top_p
            ),
            "frequency_penalty": (
                provider_params.frequency_penalty
                if provider_params.frequency_penalty is not None
                else self.config.frequency_penalty
            ),
            "presence_penalty": (
                provider_params.presence_penalty
                if provider_params.presence_penalty is not None
                else self.config.presence_penalty
            ),
            "timeout": req.timeout,
            "stream": stream,
        }
        if req.structured is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.structured.name,
                    "schema": req.structured.json_schema,
                    "strict": req.structured.strict,
                },
            }
        extra_body = self._resolve_extra_body(req)
        if extra_body:
            params["extra_body"] = extra_body
        if stream:
            params["stream_options"] = self._resolve_stream_options(provider_params)
        elif provider_params.stream_options is not None:
            params["stream_options"] = provider_params.stream_options
        if req.tools is not None:
            params["tools"] = [self._tool_to_dict(tool) for tool in req.tools]
            params["tool_choice"] = req.tool_choice
        return {k: v for k, v in params.items() if v is not None}

    def _resolve_extra_body(self, req: ModelInvocationRequest[Any]) -> dict[str, Any]:
        if req.provider_params is not None and req.provider_params.extra_body is not None:
            return dict(req.provider_params.extra_body)
        extra_body: dict[str, Any] = {}
        if self.config.thinking is not None:
            extra_body["thinking"] = self.config.thinking
        if self.config.chat_template_kwargs:
            extra_body["chat_template_kwargs"] = self.config.chat_template_kwargs
        return extra_body

    @staticmethod
    def _resolve_stream_options(provider_params: ProviderParams) -> dict[str, Any]:
        stream_options = provider_params.stream_options
        if isinstance(stream_options, dict):
            return {
                **stream_options,
                "include_usage": stream_options.get("include_usage", True),
            }
        return {"include_usage": True}

    @staticmethod
    def _tool_to_dict(tool: Any) -> dict[str, Any]:
        if isinstance(tool, dict):
            return dict(tool)
        if isinstance(tool, ToolSpec):
            return {"type": tool.type, "function": dict(tool.function)}
        return tool

    @staticmethod
    def _call_kind(req: ModelInvocationRequest[Any], *, stream: bool) -> str:
        if stream:
            return "stream"
        return "tool_selection" if req.tools is not None else "chat"

    # ------------------------------------------------------------------
    # Structured output helpers (json_schema -> json_object fallback)
    # ------------------------------------------------------------------
    @staticmethod
    def _uses_json_schema_response_format(params: dict[str, Any]) -> bool:
        response_format = params.get("response_format")
        return (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
        )

    def _should_fallback_to_json_object(
        self, exc: ProviderError, params: dict[str, Any]
    ) -> bool:
        if not self._uses_json_schema_response_format(params):
            return False
        body = exc.body
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        return error.get("message") == JSON_SCHEMA_UNAVAILABLE_MESSAGE

    @staticmethod
    def _append_json_output_instruction(
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        fallback_messages = [dict(message) for message in messages]
        instruction_parts = [JSON_OBJECT_OUTPUT_INSTRUCTION]
        if json_schema is not None:
            schema_text = json.dumps(json_schema, ensure_ascii=False, indent=2)
            instruction_parts.append(f"{JSON_OBJECT_SCHEMA_INSTRUCTION}\n{schema_text}")
        instruction = "\n\n".join(instruction_parts)

        last_system_index: int | None = None
        for index, message in enumerate(fallback_messages):
            if message.get("role") == "system":
                last_system_index = index

        if last_system_index is None:
            fallback_messages.insert(
                0,
                {
                    "role": "system",
                    "content": instruction,
                },
            )
            return fallback_messages

        system_message = dict(fallback_messages[last_system_index])
        content = str(system_message.get("content") or "")
        separator = "\n\n" if content else ""
        system_message["content"] = f"{content}{separator}{instruction}"
        fallback_messages[last_system_index] = system_message
        return fallback_messages

    @classmethod
    def _build_json_object_fallback_request(
        cls,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fallback_params = dict(params)
        json_schema = None
        response_format = params.get("response_format")
        if isinstance(response_format, dict):
            json_schema_format = response_format.get("json_schema")
            if isinstance(json_schema_format, dict):
                schema = json_schema_format.get("schema")
                if isinstance(schema, dict):
                    json_schema = schema
        fallback_params["response_format"] = {"type": "json_object"}
        return (
            cls._append_json_output_instruction(messages, json_schema=json_schema),
            fallback_params,
        )

    # ------------------------------------------------------------------
    # Outcome helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_cancelled(req: ModelInvocationRequest[Any]) -> bool:
        return req.cancel is not None and req.cancel.is_set()

    @staticmethod
    def _error(code: str, message: str, retryable: bool) -> ModelInvocationError:
        return ModelInvocationError(code=code, message=message, retryable=retryable)

    @staticmethod
    def _failed_outcome(
        code: str,
        message: str,
        attempts: int,
        started: float,
    ) -> ModelInvocationOutcome:
        return ModelInvocationOutcome(
            status="failed",
            attempts=attempts,
            latency_ms=(time.time() - started) * 1000.0,
            error=ModelInvocationError(
                code=code, message=message, retryable=False
            ),
        )

    @staticmethod
    def _cancelled_outcome(
        attempts: int, started: float
    ) -> ModelInvocationOutcome:
        return ModelInvocationOutcome(
            status="cancelled",
            attempts=attempts,
            latency_ms=(time.time() - started) * 1000.0,
            error=ModelInvocationError(
                code="cancelled", message="cancelled before invocation", retryable=False
            ),
        )

    def _outcome_from_provider_error(
        self, exc: ProviderError, attempts: int, started: float
    ) -> ModelInvocationOutcome:
        return ModelInvocationOutcome(
            status="failed",
            attempts=attempts,
            latency_ms=(time.time() - started) * 1000.0,
            error=self._provider_error_to_outcome_error(exc),
        )

    def _provider_error_to_outcome_error(
        self, exc: ProviderError
    ) -> ModelInvocationError:
        return ModelInvocationError(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            provider_status=exc.status,
            body=exc.body,
        )

    def _terminal_event(
        self,
        status: str,
        attempts: int,
        usage: ModelUsage | None,
        started: float,
        error: ModelInvocationError | None,
    ) -> ModelInvocationEvent:
        return ModelInvocationEvent(
            type="terminal",
            status=status,  # type: ignore[arg-type]
            error=error,
            usage=usage,
            data={"attempts": attempts, "latency_ms": (time.time() - started) * 1000.0},
        )

    # ------------------------------------------------------------------
    # Usage / reasoning helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _usage_from_mapping(raw: Any) -> ModelUsage | None:
        if not isinstance(raw, dict):
            return None
        values: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw.get(key)
            if isinstance(value, int):
                values[key] = value
        if not values:
            return None
        return ModelUsage(
            prompt_tokens=values.get("prompt_tokens", 0),
            completion_tokens=values.get("completion_tokens", 0),
            total_tokens=values.get("total_tokens", 0),
        )

    @classmethod
    def _usage_mapping_from_chunk(cls, chunk: dict[str, Any]) -> dict[str, int]:
        raw = chunk.get("usage")
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, int)}

    @staticmethod
    def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
        if not isinstance(raw, list):
            return None
        calls = [_dump_compat(call) for call in raw]
        return [call for call in calls if isinstance(call, dict)]

    @staticmethod
    def _extract_reasoning_content(message_or_delta: Any) -> str | None:
        if message_or_delta is None:
            return None
        if isinstance(message_or_delta, dict):
            for field_name in ("reasoning_content", "reasoning"):
                value = message_or_delta.get(field_name)
                if value:
                    return str(value)
            return None
        for field_name in ("reasoning_content", "reasoning"):
            value = getattr(message_or_delta, field_name, None)
            if value:
                return str(value)
        if hasattr(message_or_delta, "model_dump"):
            dumped = message_or_delta.model_dump()
            if isinstance(dumped, dict):
                for field_name in ("reasoning_content", "reasoning"):
                    value = dumped.get(field_name)
                    if value:
                        return str(value)
        return None

    # ------------------------------------------------------------------
    # OTel / llm_call recording
    # ------------------------------------------------------------------
    @contextmanager
    def _llm_outbound_span(self, call_kind: str):
        tracer = otel_trace.get_tracer("map.llm")
        with tracer.start_as_current_span(
            f"{call_kind} {self.config.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "openinference.span.kind": "LLM",
                "llm.model_name": str(self.config.model or ""),
                "llm.provider": str(self.config.base_url or ""),
                "map.llm.call_kind": call_kind,
            },
        ) as span:
            yield span

    def _start_llm_span(self, call_kind: str) -> Span:
        tracer = otel_trace.get_tracer("map.llm")
        return tracer.start_span(
            f"{call_kind} {self.config.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "openinference.span.kind": "LLM",
                "llm.model_name": str(self.config.model or ""),
                "llm.provider": str(self.config.base_url or ""),
                "map.llm.call_kind": call_kind,
            },
        )

    @staticmethod
    def _fail_llm_span(span: Span | None, exc: BaseException) -> None:
        if span is None:
            return
        span.record_exception(exc)
        span.set_status(OtelStatusCode.ERROR, str(exc))
        span.end()

    @staticmethod
    def _end_llm_span(span: Span | None, *, usage: ModelUsage | None) -> None:
        if span is None:
            return
        if usage is not None:
            span.set_attribute("llm.token_count.prompt", usage.prompt_tokens)
            span.set_attribute(
                "llm.token_count.completion", usage.completion_tokens
            )
        span.end()

    async def _sleep_before_retry(self, attempt: int) -> None:
        delay = self._retry_wait(_RetryStateStub(attempt))
        await asyncio.sleep(delay)

    def _call_before_retry(
        self, attempt: int, exc: BaseException | None
    ) -> None:
        if self.before_retry:
            try:
                self.before_retry(attempt, exc)
            except Exception:  # pragma: no cover
                self.logger.debug("before_retry callback failed", exc_info=True)
        if exc:
            self.logger.warning(
                f"Retrying LLM attempt {attempt} due to: {exc}"
            )

    def _record_llm_call(
        self,
        *,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        started_at: float,
        status: str,
        call_kind: str,
        outcome: ModelInvocationOutcome | None = None,
        error: BaseException | ModelInvocationError | str | None = None,
        usage: ModelUsage | dict[str, int] | None = None,
        otel_span: Span | None = None,
    ) -> None:
        trace_context = get_llm_trace_context()
        payload = self._build_llm_call_payload(
            trace_context=trace_context,
            messages=messages,
            params=params,
            started_at=started_at,
            status=status,
            call_kind=call_kind,
            outcome=outcome,
            error=error,
            usage=usage,
        )
        self._attach_llm_span_context(payload, otel_span)
        event_type = {
            "success": "model.invocation_succeeded",
            "failed": "model.invocation_failed",
            "cancelled": "model.invocation_failed",
            "unknown": "model.invocation_unknown",
        }.get(status, "model.invocation_unknown")
        try:
            from ...service.execution_event import ExecutionEventEmitter

            ExecutionEventEmitter.current().emit(event_type, data=payload)
        except Exception:
            self.logger.debug("LLM trace recording skipped", exc_info=True)

    def _build_llm_call_payload(
        self,
        *,
        trace_context: dict[str, Any],
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        started_at: float,
        status: str,
        call_kind: str,
        outcome: ModelInvocationOutcome | None,
        error: BaseException | ModelInvocationError | str | None,
        usage: ModelUsage | dict[str, int] | None,
    ) -> dict[str, Any]:
        end_ts = now_shanghai()
        started_dt = datetime_from_epoch(started_at)
        usage_dict = self._normalize_usage_for_event(usage)
        return {
            "request_id": trace_context.get("request_id"),
            "session_id": trace_context.get("session_id"),
            "staff_code": trace_context.get("staff_code"),
            "agent_code": trace_context.get("agent_code"),
            "agent_name": trace_context.get("agent_name"),
            "component": trace_context.get("component")
            or trace_context.get("agent_code")
            or call_kind,
            "phase": trace_context.get("phase"),
            "step": trace_context.get("step"),
            "call_kind": trace_context.get("call_kind") or call_kind,
            "model": (
                getattr(outcome, "model", None)
                or params.get("model")
                or self.config.model
            ),
            "provider_request_id": getattr(outcome, "request_id", None),
            "start_ts": started_dt,
            "end_ts": end_ts,
            "duration_s": time.time() - started_at,
            "status": status,
            "usage": usage_dict or getattr(outcome, "usage", None),
            "error": str(error) if error is not None else None,
            "finish_reason": getattr(outcome, "finish_reason", None),
            "prompt_summary": summarize_llm_messages(list(messages)),
            "tool_names": self._tool_names_from_params(params),
        }

    @staticmethod
    def _normalize_usage_for_event(
        usage: ModelUsage | dict[str, int] | None,
    ) -> dict[str, int] | None:
        if isinstance(usage, ModelUsage):
            return usage.to_dict()
        if isinstance(usage, dict):
            return {
                str(k): int(v) for k, v in usage.items() if isinstance(v, int)
            }
        return None

    @staticmethod
    def _tool_names_from_params(params: dict[str, Any]) -> list[str] | None:
        tools = params.get("tools")
        if not isinstance(tools, list):
            return None
        tool_names: list[str] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            fn = item.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                tool_names.append(str(fn["name"]))
        return tool_names

    def _attach_llm_span_context(
        self,
        payload: dict[str, Any],
        otel_span: Span | None,
    ) -> None:
        from ...observability import current_trace_context

        otel_ctx: dict[str, str] = {}
        if otel_span is not None:
            span_ctx = otel_span.get_span_context()
            if span_ctx is not None and span_ctx.is_valid:
                otel_ctx = {
                    "trace_id": format(span_ctx.trace_id, "032x"),
                    "span_id": format(span_ctx.span_id, "016x"),
                }
        if not otel_ctx:
            otel_ctx = current_trace_context()
        if otel_ctx:
            payload["trace_id"] = otel_ctx.get("trace_id")
            payload["span_id"] = otel_ctx.get("span_id")
