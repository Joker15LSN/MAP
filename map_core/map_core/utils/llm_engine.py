"""Backward-compatible thin shell around the typed ModelInvocation module.

Public symbols and method signatures are preserved for existing callers.
All provider access, retry, usage and OTel logic lives in
``map_core.utils.model_invocation``; this module only translates between the
legacy models/methods and ``ModelInvocation.invoke``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any, Awaitable, Callable, Literal, Sequence, overload

from loguru import logger as loguru_logger
from pydantic import BaseModel

from ..config.config_schema import LLMConfig
from ..schema.agent_schema import Function, Message, ToolCall
from .model_invocation import (
    ModelInvocation,
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelMessage,
    ProviderParams,
    StructuredOutput,
)


class _ShellInvocationError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class LLMMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"role": self.role, "content": self.content}
        if self.name:
            result["name"] = self.name
        return result


class LLMResponse(BaseModel):
    """Normalized response object for a single completion."""

    content: str
    reasoning_content: str | None = None
    logprobs: Any | None = None
    prompt_token_ids: list[int] | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    response_time: float = 0.0
    request_id: str | None = None
    raw: Any | None = None


class ToolCallResponse(BaseModel):
    """Normalized response for tool-calling chats."""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    response_time: float = 0.0
    request_id: str | None = None
    raw: Any | None = None


class RetryState:
    attempt: int
    last_exception: BaseException | None

    def __init__(
        self, attempt: int, last_exception: BaseException | None = None
    ) -> None:
        self.attempt = attempt
        self.last_exception = last_exception


class LLMEngine:
    """Legacy LLM engine API, now translated to ``ModelInvocation``."""

    def __init__(
        self,
        config: LLMConfig,
        logger: Any | None = None,
        *,
        before_retry: Callable[[RetryState], None] | None = None,
        after_success: Callable[[LLMResponse], None] | None = None,
    ):
        self.config = config
        self.logger = logger or loguru_logger
        self.before_retry = before_retry
        self.after_success = after_success
        self._invocation = ModelInvocation(
            config,
            logger=self.logger,
            before_retry=self._before_retry_adapter if before_retry else None,
        )

    @classmethod
    def create(
        cls,
        *,
        base_url: str,
        api_key: str = "",
        model: str | None = None,
        temperature: float = 0.7,
        logger: Any | None = None,
        **kwargs,
    ) -> "LLMEngine":
        """Initialize LLMEngine with minimal parameters."""
        config = LLMConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            **kwargs,
        )
        return cls(config, logger)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def invoke(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> LLMResponse:
        """Synchronous single completion."""
        req = self._build_request(messages, stream=False, **kwargs)
        outcome = asyncio.run(self._invocation.invoke(req))
        return self._outcome_to_llm_response(outcome)

    async def ainvoke(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> LLMResponse:
        req = self._build_request(messages, stream=False, **kwargs)
        outcome = await self._invocation.invoke(req)
        return self._outcome_to_llm_response(outcome)

    def stream(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> Generator[str, None, None]:
        """Synchronous streaming (yields plain text chunks)."""
        req = self._build_request(messages, stream=True, **kwargs)

        async def _consume() -> list[str]:
            parts: list[str] = []
            stream_obj = await self._invocation.invoke(req)
            async for event in stream_obj:
                if event.type == "content":
                    data = event.data or {}
                    token = data.get("data", "")
                    if token:
                        parts.append(str(token))
                elif event.type == "terminal":
                    self._raise_for_terminal(event)
            return parts

        for token in asyncio.run(_consume()):
            yield token

    async def astream(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> AsyncGenerator[dict[str, str | dict[str, int]], None]:
        req = self._build_request(messages, stream=True, **kwargs)
        stream_obj = await self._invocation.invoke(req)
        async for event in stream_obj:
            if event.type in ("content", "reasoning", "usage"):
                if event.data is not None:
                    yield event.data
            elif event.type == "terminal":
                self._raise_for_terminal(event)

    async def ask_tool(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        system_msgs: Sequence[LLMMessage | dict[str, Any]] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        **kwargs,
    ) -> ToolCallResponse:
        """Async tool-calling helper using OpenAI-compatible schema."""
        all_messages: list[LLMMessage | dict[str, Any]] = []
        if system_msgs:
            all_messages.extend(list(system_msgs))
        all_messages.extend(list(messages))

        if tool_choice is not None and hasattr(tool_choice, "value"):
            tool_choice = tool_choice.value
        req = self._build_request(
            all_messages,
            stream=False,
            tools=list(tools) if tools is not None else None,
            tool_choice=tool_choice,
            **kwargs,
        )
        outcome = await self._invocation.invoke(req)
        self._raise_for_outcome(outcome)
        return self._outcome_to_tool_response(outcome)

    def chat(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse | Generator[str, None, None]:
        """Backward compatible wrapper around invoke/stream."""
        if stream:
            return self.stream(messages, **kwargs)
        return self.invoke(messages, **kwargs)

    @overload
    def achat(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        stream: Literal[True],
        **kwargs,
    ) -> AsyncGenerator[dict[str, str | dict[str, int]], None]: ...

    @overload
    def achat(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        stream: Literal[False] = False,
        **kwargs,
    ) -> Awaitable[LLMResponse]: ...

    def achat(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        stream: bool = False,
        timeout: float | None = None,
        **kwargs,
    ) -> Awaitable[LLMResponse] | AsyncGenerator[dict[str, str | dict[str, int]], None]:
        """Backward compatible wrapper around ainvoke/astream.

        This is intentionally a non-async function.
        """
        if stream:
            if "timeout" in kwargs:
                kwargs.pop("timeout", None)
            return self.astream(messages, **kwargs)
        if timeout is None and "timeout" in kwargs:
            timeout = kwargs.pop("timeout", None)
        if timeout is None:
            return self.ainvoke(messages, **kwargs)

        async def _with_timeout() -> LLMResponse:
            return await asyncio.wait_for(
                self.ainvoke(messages, **kwargs), timeout=timeout
            )

        return _with_timeout()

    def simple_chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response_schema",
        timeout: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Synchronous convenience call (non-stream)."""
        if timeout is not None:
            kwargs = dict(kwargs)
            kwargs["timeout"] = timeout
        messages, kwargs = self._build_basic_messages(
            prompt, system_prompt, json_schema, schema_name, kwargs
        )
        result = self.chat(messages, **kwargs)
        if isinstance(result, LLMResponse):
            return result
        return LLMResponse(
            content="".join(result), model=self.config.model, response_time=0.0
        )

    def simple_chat_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response_schema",
        **kwargs,
    ) -> Generator[str, None, None]:
        """Synchronous streaming convenience call (yields plain text chunks)."""
        messages, kwargs = self._build_basic_messages(
            prompt, system_prompt, json_schema, schema_name, kwargs
        )
        result = self.chat(messages, stream=True, **kwargs)
        if isinstance(result, Generator):
            return result

        def single_yield():
            yield result.content

        return single_yield()

    async def asimple_chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response_schema",
        **kwargs,
    ) -> LLMResponse:
        messages, kwargs = self._build_basic_messages(
            prompt, system_prompt, json_schema, schema_name, kwargs
        )
        result = await self.achat(messages, **kwargs)
        if isinstance(result, LLMResponse):
            return result
        content_parts: list[str] = []
        async for part in result:
            if isinstance(part, dict):
                data = part.get("data") if part.get("type") == "content" else None
                if data:
                    content_parts.append(str(data))
            else:
                content_parts.append(str(part))
        return LLMResponse(
            content="".join(content_parts),
            model=self.config.model,
            response_time=0.0,
        )

    async def asimple_chat_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response_schema",
        **kwargs,
    ) -> AsyncGenerator[dict[str, str | dict[str, int]], None]:
        """Async streaming convenience call (yields dict chunks)."""
        messages, kwargs = self._build_basic_messages(
            prompt, system_prompt, json_schema, schema_name, kwargs
        )
        async for chunk in self.achat(messages, stream=True, **kwargs):
            if isinstance(chunk, str):
                yield {"type": "content", "data": chunk}
            else:
                yield chunk

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self):
        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                loop.create_task(self._invocation.aclose())
            else:
                asyncio.run(self._invocation.aclose())
        except Exception as e:
            self.logger.warning(f"Error closing async client gracefully: {e}")

    async def aclose(self):
        try:
            await self._invocation.aclose()
        except Exception as e:
            self.logger.warning(f"Error closing clients: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    # ------------------------------------------------------------------
    # Translation helpers
    # ------------------------------------------------------------------
    def _build_request(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        *,
        stream: bool,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        **kwargs,
    ) -> ModelInvocationRequest:
        structured: StructuredOutput | None = None
        if "json_schema" in kwargs:
            structured = StructuredOutput(
                schema=kwargs["json_schema"],
                name=kwargs.get("schema_name", "response_schema"),
                strict=kwargs.get("schema_strict", False),
                parse=False,  # legacy callers parse the returned content themselves
            )
        return ModelInvocationRequest(
            messages=self._convert_messages(messages),
            stream=stream,
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
            tools=list(tools) if tools is not None else None,
            tool_choice=tool_choice if tool_choice is not None else "auto",
            structured=structured,
            provider_params=self._build_provider_params(kwargs),
            timeout=kwargs.get("timeout"),
            raw_compat=True,
        )

    def _build_provider_params(self, kwargs: dict[str, Any]) -> ProviderParams:
        extra_body: dict[str, Any] = {}
        thinking = kwargs.get("thinking", self.config.thinking)
        if thinking is not None:
            extra_body["thinking"] = thinking
        if self.config.chat_template_kwargs:
            extra_body["chat_template_kwargs"] = self.config.chat_template_kwargs
        return ProviderParams(
            top_p=kwargs.get("top_p"),
            frequency_penalty=kwargs.get("frequency_penalty"),
            presence_penalty=kwargs.get("presence_penalty"),
            logprobs=kwargs.get("logprobs"),
            top_logprobs=kwargs.get("top_logprobs"),
            extra_body=extra_body if extra_body else None,
            stream_options=kwargs.get("stream_options"),
        )

    @staticmethod
    def _convert_messages(
        messages: Sequence[LLMMessage | dict[str, Any]],
    ) -> list[ModelMessage | dict[str, Any]]:
        converted: list[ModelMessage | dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, LLMMessage):
                converted.append(msg.to_dict())
            elif isinstance(msg, Message):
                converted.append(msg.to_dict())
            elif isinstance(msg, dict):
                converted.append(msg)
            else:
                raise TypeError(f"Unsupported message type: {type(msg)}")
        return converted

    def _outcome_to_llm_response(self, outcome: ModelInvocationOutcome) -> LLMResponse:
        self._raise_for_outcome(outcome)
        llm_resp = LLMResponse(
            content=outcome.content or "",
            reasoning_content=outcome.reasoning_content,
            logprobs=self._raw_logprobs(outcome),
            prompt_token_ids=self._raw_prompt_token_ids(outcome),
            model=outcome.model,
            usage=outcome.usage.to_dict() if outcome.usage else None,
            finish_reason=outcome.finish_reason,
            response_time=outcome.latency_ms / 1000.0,
            request_id=outcome.request_id,
            raw=outcome.raw,
        )
        self._call_after_success(llm_resp)
        return llm_resp

    def _outcome_to_tool_response(
        self, outcome: ModelInvocationOutcome
    ) -> ToolCallResponse:
        tool_calls: list[ToolCall] = []
        for call in outcome.tool_calls or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                tool_calls.append(
                    ToolCall(
                        id=call.get("id") or "",
                        function=Function(
                            name=fn.get("name") or "",
                            arguments=str(fn.get("arguments") or ""),
                        ),
                    )
                )
        return ToolCallResponse(
            content=outcome.content or "",
            tool_calls=tool_calls,
            model=outcome.model,
            usage=outcome.usage.to_dict() if outcome.usage else None,
            finish_reason=outcome.finish_reason,
            response_time=outcome.latency_ms / 1000.0,
            request_id=outcome.request_id,
            raw=outcome.raw,
        )

    def _raise_for_outcome(self, outcome: ModelInvocationOutcome) -> None:
        if outcome.status == "succeeded":
            return
        error = outcome.error
        code = error.code if error else outcome.status
        message = error.message if error else outcome.status
        raise _ShellInvocationError(
            f"LLM invocation {outcome.status} (code={code}): {message}", code=code
        )

    def _raise_for_terminal(self, event: Any) -> None:
        if getattr(event, "status", None) == "succeeded":
            return
        error = getattr(event, "error", None)
        code = error.code if error else getattr(event, "status", "unknown")
        message = error.message if error else getattr(event, "status", "unknown")
        raise _ShellInvocationError(
            f"LLM stream {getattr(event, 'status', 'unknown')} (code={code}): {message}",
            code=code,
        )

    @staticmethod
    def _raw_logprobs(outcome: ModelInvocationOutcome) -> Any | None:
        raw = outcome.raw
        if not isinstance(raw, dict):
            return None
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        return first.get("logprobs") if isinstance(first, dict) else None

    @staticmethod
    def _raw_prompt_token_ids(
        outcome: ModelInvocationOutcome,
    ) -> list[int] | None:
        raw = outcome.raw
        if not isinstance(raw, dict):
            return None
        token_ids = raw.get("prompt_token_ids")
        if not isinstance(token_ids, list):
            return None
        try:
            return [int(token_id) for token_id in token_ids]
        except (TypeError, ValueError):
            return None

    def _call_after_success(self, llm_resp: LLMResponse) -> None:
        if self.after_success:
            try:
                self.after_success(llm_resp)
            except Exception:  # pragma: no cover - defensive
                self.logger.debug("after_success callback failed", exc_info=True)

    def _before_retry_adapter(
        self, attempt: int, last_exception: BaseException | None
    ) -> None:
        if self.before_retry:
            try:
                self.before_retry(
                    RetryState(attempt=attempt, last_exception=last_exception)
                )
            except Exception:  # pragma: no cover
                self.logger.debug("before_retry callback failed", exc_info=True)

    def _build_basic_messages(
        self,
        prompt: str,
        system_prompt: str | None,
        json_schema: dict[str, Any] | None,
        schema_name: str,
        kwargs: dict[str, Any],
    ) -> tuple[list[LLMMessage], dict[str, Any]]:
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(LLMMessage(role="system", content=system_prompt))
        messages.append(LLMMessage(role="user", content=prompt))
        if json_schema is not None:
            kwargs = dict(kwargs)
            kwargs["json_schema"] = json_schema
            kwargs["schema_name"] = schema_name
        return messages, kwargs
