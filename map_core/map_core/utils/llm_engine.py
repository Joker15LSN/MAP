"""Light-weight LLM engine wrapper (OpenAI compatible) with a LangChain-ish API.

Goals / Design:
* Keep dependency surface minimal (only openai compatible sdk + pydantic).
* Provide a clear, cohesive public API with explicit sync/async & streaming variants:
        - invoke / ainvoke          -> single shot completion returning ``LLMResponse``.
        - stream / astream          -> generators yielding tokens (and reasoning chunks).
* Backwards compatibility: existing ``chat``, ``achat``, ``simple_chat`` family kept
    as thin wrappers around the new API to avoid touching call-sites.
* Internal helpers for: parameter preparation, message normalisation, retries,
    timing, safe closing.
* Extensible: retry policy & hooks (callbacks) can be expanded later without
    changing method signatures.

NOT in scope (keep lean): tool calling, function calling orchestration, caching.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Sequence,
    cast,
    overload,
)

from loguru import logger as loguru_logger
from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, Field, PositiveInt, field_validator
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry,
    stop_after_attempt,
    wait_exponential,
)

from ..config.config_schema import LLMConfig
from ..schema.agent_schema import Function, Message, ToolCall

JSON_SCHEMA_UNAVAILABLE_MESSAGE = "This response_format type is unavailable now"
JSON_OBJECT_OUTPUT_INSTRUCTION = "Use JSON format as output."
JSON_OBJECT_SCHEMA_INSTRUCTION = "Follow this JSON schema:"


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


@dataclass
class RetryState:
    attempt: int
    last_exception: BaseException | None = None


class LLMEngine:
    """LLM Engine encapsulating OpenAI-compatible chat completions.

    Typical usage (async):
        async with LLMEngine(cfg, logger) as engine:
            resp = await engine.ainvoke([LLMMessage(role="user", content="hi")])
            async for chunk in engine.astream([LLMMessage(role="user", content="hi")]):
                ...

    Synchronous usage supported via context manager as well.
    """

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
        self._sync_client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=0,
            default_headers=config.extra_headers or None,
        )
        self._async_client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=0,
            default_headers=config.extra_headers or None,
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
        """Initialize LLMEngine with minimal parameters.

        Required parameters: base_url, api_key, model, temperature.
        Additional LLMConfig fields can be passed via **kwargs.
        """
        config = LLMConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            **kwargs,
        )
        return cls(config, logger)

    def invoke(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> LLMResponse:
        """Synchronous single completion (tenacity retry)."""

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            before_sleep=self._tenacity_before_retry,
            retry_error_callback=self._tenacity_error_callback,
        )
        def _do():
            return self._invoke_once(messages, **kwargs)

        return _do()

    async def ainvoke(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> LLMResponse:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
            before_sleep=self._atenacity_before_retry,
            retry_error_callback=self._tenacity_error_callback,
        ):
            with attempt:
                return await self._ainvoke_once(messages, **kwargs)
        raise RuntimeError("ainvoke retry logic exhausted")

    def stream(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> Generator[str, None, None]:
        """Synchronous streaming with retry.

        On retry, already yielded parts from previous attempts are lost (caller
        only sees final successful attempt). This keeps implementation simple.
        """

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            before_sleep=self._tenacity_before_retry,
            retry_error_callback=self._tenacity_error_callback,
        )
        def _produce():
            return list(self._stream_once(messages, **kwargs))

        for token in _produce():
            yield token

    async def astream(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        **kwargs,
    ) -> AsyncGenerator[dict[str, str | dict[str, int]], None]:
        attempt_number = 0
        max_attempts = self.config.max_retries + 1
        while attempt_number < max_attempts:
            attempt_number += 1
            try:
                async_gen = await self._astream_once(messages, **kwargs)
                async for chunk in async_gen:
                    yield chunk
                break
            except Exception as e:
                if attempt_number >= max_attempts:
                    raise
                if self.before_retry:
                    try:
                        self.before_retry(
                            RetryState(attempt=attempt_number - 1, last_exception=e)
                        )
                    except Exception:
                        self.logger.debug("before_retry callback failed", exc_info=True)
                self.logger.warning(
                    f"Retrying async stream attempt {attempt_number}/{max_attempts} due to: {e}"
                )
                await asyncio.sleep(min(2 ** (attempt_number - 1), 8))

    async def ask_tool(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        system_msgs: Sequence[LLMMessage | dict[str, Any]] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        **kwargs,
    ) -> ToolCallResponse:
        """Async tool-calling helper using OpenAI-compatible schema."""
        attempt_number = 0
        max_attempts = self.config.max_retries + 1
        while attempt_number < max_attempts:
            attempt_number += 1
            try:
                return await self._ask_tool_once(
                    messages,
                    system_msgs=system_msgs,
                    tools=tools,
                    tool_choice=tool_choice,
                    **kwargs,
                )
            except Exception as exc:
                if attempt_number >= max_attempts:
                    raise
                if self.before_retry:
                    try:
                        self.before_retry(
                            RetryState(attempt=attempt_number, last_exception=exc)
                        )
                    except Exception:  # pragma: no cover
                        self.logger.debug("before_retry callback failed", exc_info=True)
                self.logger.warning(
                    "Retrying async tool call attempt {}/{} due to: {}".format(
                        attempt_number + 1, max_attempts, exc
                    )
                )
                await asyncio.sleep(min(2 ** (attempt_number - 1), 8))
        raise RuntimeError("ask_tool retry logic exhausted")

    async def _ask_tool_once(
        self,
        messages: Sequence[LLMMessage | dict[str, Any]],
        system_msgs: Sequence[LLMMessage | dict[str, Any]] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        **kwargs,
    ) -> ToolCallResponse:
        started = time.time()
        all_messages: list[LLMMessage | dict[str, Any]] = []
        if system_msgs:
            all_messages.extend(list(system_msgs))
        all_messages.extend(list(messages))
        msgs = self._prepare_messages(list(all_messages))

        params = self._prepare_params(stream=False, **kwargs)
        if tools is not None:
            params["tools"] = list(tools)
        if tool_choice is not None:
            if hasattr(tool_choice, "value"):
                params["tool_choice"] = tool_choice.value
            else:
                params["tool_choice"] = tool_choice

        try:
            completions = self._async_client.chat.completions
            raw_completions = getattr(completions, "with_raw_response", None)
            if raw_completions is not None:
                raw_response = await raw_completions.create(messages=msgs, **params)
                response = await self._acoerce_raw_chat_completion_response(
                    raw_response
                )
            else:
                response = await completions.create(messages=msgs, **params)
                response = self._coerce_chat_completion_response(response)
        except Exception as e:  # pragma: no cover
            response = self._coerce_chat_completion_exception(e)
            if response is None:
                self.logger.error(f"Async ask_tool failed: {e}")
                raise

        # Normalize response
        if not response.choices:
            llm_resp = self._handle_async_response(response, started_at=started)
            return ToolCallResponse(
                content="",
                tool_calls=[],
                model=llm_resp.model,
                usage=llm_resp.usage,
                finish_reason=llm_resp.finish_reason,
                response_time=llm_resp.response_time,
                request_id=llm_resp.request_id,
                raw=response,
            )

        message = response.choices[0].message
        content = message.content or ""
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for call in raw_tool_calls:
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            arguments = getattr(fn, "arguments", "") if fn else ""
            if arguments is None:
                arguments = ""
            tool_calls.append(
                ToolCall(
                    id=getattr(call, "id", ""),
                    type="function",
                    function=Function(name=name, arguments=str(arguments)),
                )
            )

        llm_resp = self._handle_async_response(response, started_at=started)
        return ToolCallResponse(
            content=content,
            tool_calls=tool_calls,
            model=llm_resp.model,
            usage=llm_resp.usage,
            finish_reason=llm_resp.finish_reason,
            response_time=llm_resp.response_time,
            request_id=llm_resp.request_id,
            raw=response,
        )

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

        Important: This is intentionally a *non-async* function.
        - stream=False: returns an awaitable (caller should `await engine.achat(...)`).
        - stream=True: returns an async generator (caller should `async for ... in engine.achat(stream=True, ...)`).

        This avoids the Python pitfall where an `async def` wrapper would always
        return a coroutine, making `async for` fail with:
        "'async for' requires an object with __aiter__ method, got coroutine".
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
        # Fallback: stream path incorrectly used without stream flag
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

    def _prepare_messages(
        self,
        messages: Sequence[LLMMessage | Message | dict[str, Any]],
    ) -> list[ChatCompletionMessageParam]:
        prepared: list[ChatCompletionMessageParam] = []
        for msg in messages:
            if isinstance(msg, LLMMessage):
                prepared.append(cast(ChatCompletionMessageParam, msg.to_dict()))
            elif isinstance(msg, Message):
                prepared.append(cast(ChatCompletionMessageParam, msg.to_dict()))
            elif isinstance(msg, dict):
                # Normalize/validate OpenAI-compatible message dicts.
                # Tool-calling messages may omit `content` on the assistant role.
                if "role" not in msg:
                    raise ValueError("Message dict must contain 'role'")

                role = msg.get("role")

                # Don't mutate the caller's dict.
                normalized: dict[str, Any] = dict(msg)

                # Back-compat: some call-sites use a legacy tool message shape:
                # {"role":"tool","type":"function_call_output","call_id":...,"output":...}
                if role == "tool":
                    if "tool_call_id" not in normalized and "call_id" in normalized:
                        normalized["tool_call_id"] = normalized.get("call_id")
                    if "content" not in normalized and "output" in normalized:
                        normalized["content"] = normalized.get("output")

                if "content" not in normalized:
                    # OpenAI tool-calling assistant message shape can omit content.
                    if role == "assistant" and (
                        "tool_calls" in normalized or "function_call" in normalized
                    ):
                        normalized["content"] = ""
                    else:
                        raise ValueError(
                            "Message dict must contain 'content' unless it is an assistant tool-calling message"
                        )

                # For tool role, both tool_call_id and content are required.
                if role == "tool":
                    if "tool_call_id" not in normalized:
                        raise ValueError(
                            "Tool message dict must contain 'tool_call_id' and 'content'"
                        )

                # Ensure content is a string when present (OpenAI accepts empty string).
                if normalized.get("content") is None:
                    if role == "assistant":
                        normalized["content"] = ""
                    else:
                        raise ValueError(
                            "Message dict 'content' cannot be None for non-assistant roles"
                        )
                elif not isinstance(normalized.get("content"), str):
                    normalized["content"] = str(normalized.get("content"))

                # Drop non-standard keys that may cause OpenAI SDK validation errors.
                allowed_keys_by_role: dict[str, set[str]] = {
                    "system": {"role", "content", "name"},
                    "user": {"role", "content", "name"},
                    "assistant": {
                        "role",
                        "content",
                        "name",
                        "tool_calls",
                        "function_call",
                    },
                    "tool": {"role", "content", "tool_call_id"},
                }
                allowed_keys = allowed_keys_by_role.get(str(role))
                if allowed_keys is not None:
                    normalized = {
                        k: v for k, v in normalized.items() if k in allowed_keys
                    }

                prepared.append(cast(ChatCompletionMessageParam, normalized))
            else:
                raise TypeError(f"Unsupported message type: {type(msg)}")
        return prepared

    def _prepare_params(self, stream: bool = False, **kwargs) -> dict[str, Any]:
        params = {
            "model": self.config.model,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "logprobs": kwargs.get("logprobs", self.config.logprobs),
            "top_logprobs": kwargs.get("top_logprobs", self.config.top_logprobs),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
            "top_p": kwargs.get("top_p", self.config.top_p),
            "frequency_penalty": kwargs.get(
                "frequency_penalty", self.config.frequency_penalty
            ),
            "presence_penalty": kwargs.get(
                "presence_penalty", self.config.presence_penalty
            ),
            "timeout": kwargs.get("timeout"),
            "stream": stream,
        }

        # support for json_schema parameter using SGLang response_format
        if "json_schema" in kwargs:
            json_schema_data = kwargs["json_schema"]
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": kwargs.get("schema_name", "response_schema"),
                    "schema": json_schema_data,
                    "strict": kwargs.get("schema_strict", False),
                },
            }

        # support for qwen3 thinking mode
        extra_body = {}
        thinking = kwargs.get("thinking", self.config.thinking)
        if thinking is not None:
            extra_body["thinking"] = thinking
        if self.config.chat_template_kwargs:
            extra_body["chat_template_kwargs"] = self.config.chat_template_kwargs

        if extra_body:
            params["extra_body"] = extra_body

        if "stream_options" in kwargs:
            params["stream_options"] = kwargs["stream_options"]

        params = {k: v for k, v in params.items() if v is not None}

        return params

    @staticmethod
    def _uses_json_schema_response_format(params: dict[str, Any]) -> bool:
        response_format = params.get("response_format")
        return (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
        )

    @classmethod
    def _parse_exception_payload_candidate(cls, candidate: Any) -> dict[str, Any] | None:
        if callable(candidate):
            candidate = candidate()
        if inspect.isawaitable(candidate):
            return None
        if isinstance(candidate, bytes):
            candidate = candidate.decode("utf-8")
        if isinstance(candidate, str):
            return cls._parse_non_stream_payload(candidate)
        if isinstance(candidate, dict):
            return candidate
        return None

    @classmethod
    def _iter_exception_payloads(cls, exc: BaseException) -> Generator[dict[str, Any]]:
        response = getattr(exc, "response", None)
        candidates = [
            getattr(exc, "body", None),
            getattr(response, "text", None),
            getattr(response, "content", None),
        ]
        for candidate in candidates:
            payload = cls._parse_exception_payload_candidate(candidate)
            if payload is not None:
                yield payload

    @classmethod
    def _is_json_schema_unavailable_error(cls, exc: BaseException) -> bool:
        for payload in cls._iter_exception_payloads(exc):
            error = payload.get("error")
            if not isinstance(error, dict):
                continue
            if error.get("message") == JSON_SCHEMA_UNAVAILABLE_MESSAGE:
                return True
        return False

    @staticmethod
    def _append_json_output_instruction(
        messages: Sequence[ChatCompletionMessageParam],
        json_schema: dict[str, Any] | None = None,
    ) -> list[ChatCompletionMessageParam]:
        fallback_messages = [dict(cast(dict[str, Any], message)) for message in messages]
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
            return cast(list[ChatCompletionMessageParam], fallback_messages)

        system_message = dict(fallback_messages[last_system_index])
        content = str(system_message.get("content") or "")
        separator = "\n\n" if content else ""
        system_message["content"] = f"{content}{separator}{instruction}"
        fallback_messages[last_system_index] = system_message
        return cast(list[ChatCompletionMessageParam], fallback_messages)

    @classmethod
    def _build_json_object_fallback_request(
        cls,
        messages: Sequence[ChatCompletionMessageParam],
        params: dict[str, Any],
    ) -> tuple[list[ChatCompletionMessageParam], dict[str, Any]]:
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

    def _should_fallback_to_json_object(
        self, exc: BaseException, params: dict[str, Any]
    ) -> bool:
        return self._uses_json_schema_response_format(
            params
        ) and self._is_json_schema_unavailable_error(exc)

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
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
            kwargs = dict(kwargs)  # shallow copy to avoid side effects
            kwargs["json_schema"] = json_schema
            kwargs["schema_name"] = schema_name
        return messages, kwargs

    @staticmethod
    def _extract_reasoning_content(message_or_delta: Any) -> str | None:
        """Read reasoning content from OpenAI-compatible response objects."""
        if message_or_delta is None:
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

        if isinstance(message_or_delta, dict):
            for field_name in ("reasoning_content", "reasoning"):
                value = message_or_delta.get(field_name)
                if value:
                    return str(value)

        return None

    def _handle_sync_response(
        self, response: ChatCompletion, *, started_at: float | None = None
    ) -> LLMResponse:
        response = self._coerce_chat_completion_response(response)
        choice = response.choices[0]
        prompt_token_ids = getattr(response, "prompt_token_ids", None)
        if prompt_token_ids is not None:
            prompt_token_ids = [int(token_id) for token_id in prompt_token_ids]

        usage = None
        if response.usage:
            usage_dict = response.usage.model_dump()
            usage = {
                k: v
                for k, v in usage_dict.items()
                if v is not None and isinstance(v, int)
            }
        response_time = 0.0
        if started_at is not None:
            response_time = time.time() - started_at

        llm_resp = LLMResponse(
            content=choice.message.content or "",
            reasoning_content=self._extract_reasoning_content(choice.message),
            logprobs=self._dump_compat(getattr(choice, "logprobs", None)),
            prompt_token_ids=prompt_token_ids,
            model=response.model,
            usage=usage if usage else None,
            finish_reason=choice.finish_reason,
            request_id=getattr(response, "id", None),
            response_time=response_time,
            raw=response,
        )
        if self.after_success:
            try:
                self.after_success(llm_resp)
            except Exception:  # pragma: no cover - defensive
                self.logger.debug("after_success callback failed", exc_info=True)
        return llm_resp

    def _handle_async_response(
        self, response: ChatCompletion, *, started_at: float | None = None
    ) -> LLMResponse:
        return self._handle_sync_response(response, started_at=started_at)

    def _handle_sync_stream(self, response) -> Generator[str, None, None]:
        try:
            for chunk in response:
                if not chunk.choices or not chunk.choices[0].delta:
                    continue
                content = getattr(chunk.choices[0].delta, "content", None)
                if content:
                    yield content
        except Exception as e:
            self.logger.error(f"Error in sync stream: {e}")
            raise

    async def _handle_async_stream(
        self, response
    ) -> AsyncGenerator[dict[str, Any], None]:
        try:
            async for chunk in response:
                serialized_chunk = self._dump_compat(chunk)
                if not chunk.choices:
                    # Usage-only chunk emitted by the API when
                    # stream_options={"include_usage": True} is set.
                    if chunk.usage:
                        usage_dict = {
                            k: v
                            for k, v in chunk.usage.model_dump().items()
                            if isinstance(v, int)
                        }
                        if usage_dict:
                            yield {"type": "usage", "data": usage_dict}
                    continue
                if not chunk.choices[0].delta:
                    continue
                delta = chunk.choices[0].delta
                choice_payload = {}
                if isinstance(serialized_chunk, dict):
                    raw_choices = serialized_chunk.get("choices")
                    if isinstance(raw_choices, list) and raw_choices:
                        first_choice = raw_choices[0]
                        if isinstance(first_choice, dict):
                            choice_payload = first_choice
                delta_payload = choice_payload.get("delta", {})
                content = getattr(delta, "content", None)
                logprobs = choice_payload.get("logprobs")
                has_content_event = (
                    "content" in delta_payload
                    or "role" in delta_payload
                    or "logprobs" in choice_payload
                )
                if has_content_event:
                    yield {
                        "type": "content",
                        "data": "" if content is None else str(content),
                        "id": serialized_chunk.get("id")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "object": serialized_chunk.get("object")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "created": serialized_chunk.get("created")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "model": serialized_chunk.get("model")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "choices": serialized_chunk.get("choices")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "prompt_token_ids": serialized_chunk.get("prompt_token_ids")
                        if isinstance(serialized_chunk, dict)
                        else None,
                        "logprobs": logprobs,
                        "raw_chunk": serialized_chunk,
                    }
                reasoning_chunk = self._extract_reasoning_content(delta)
                if reasoning_chunk:
                    yield {"type": "reasoning", "data": reasoning_chunk}
        except Exception as e:
            self.logger.error(f"Error in async stream: {e}")
            raise

    @classmethod
    def _dump_compat(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [cls._dump_compat(item) for item in value]
        if isinstance(value, tuple):
            return [cls._dump_compat(item) for item in value]
        if isinstance(value, dict):
            return {str(k): cls._dump_compat(v) for k, v in value.items()}
        if hasattr(value, "model_dump") and callable(value.model_dump):
            dumped = value.model_dump()
            return cls._dump_compat(dumped)
        if hasattr(value, "__dict__"):
            return {
                str(k): cls._dump_compat(v)
                for k, v in vars(value).items()
                if not k.startswith("_")
            }
        return value

    @classmethod
    def _coerce_chat_completion_response(cls, response: Any) -> ChatCompletion:
        """Accept OpenAI-compatible non-stream responses wrapped as SSE text."""
        if isinstance(response, ChatCompletion):
            return response

        payload: dict[str, Any] | None = None
        if isinstance(response, bytes):
            response = response.decode("utf-8")
        if isinstance(response, str):
            payload = cls._parse_non_stream_payload(response)
        elif isinstance(response, dict):
            payload = response

        if payload is None:
            return cast(ChatCompletion, response)
        return ChatCompletion.model_validate(payload)

    @staticmethod
    def _parse_non_stream_payload(text: str) -> dict[str, Any] | None:
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

    @staticmethod
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

    @staticmethod
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

    @classmethod
    def _coerce_raw_chat_completion_response(cls, raw_response: Any) -> ChatCompletion:
        text = cls._raw_response_text(raw_response)
        if text is not None:
            payload = cls._parse_non_stream_payload(text)
            if payload is not None:
                return ChatCompletion.model_validate(payload)

        parsed = raw_response.parse()
        return cls._coerce_chat_completion_response(parsed)

    @classmethod
    async def _acoerce_raw_chat_completion_response(
        cls, raw_response: Any
    ) -> ChatCompletion:
        text = await cls._araw_response_text(raw_response)
        if text is not None:
            payload = cls._parse_non_stream_payload(text)
            if payload is not None:
                return ChatCompletion.model_validate(payload)

        parsed = raw_response.parse()
        if inspect.isawaitable(parsed):
            parsed = await parsed
        return cls._coerce_chat_completion_response(parsed)

    @classmethod
    def _coerce_chat_completion_exception(
        cls, exc: BaseException
    ) -> ChatCompletion | None:
        for payload in cls._iter_exception_payloads(exc):
            try:
                return ChatCompletion.model_validate(payload)
            except Exception:
                continue
        return None

    def _create_sync_chat_completion(
        self,
        *,
        messages: Sequence[ChatCompletionMessageParam],
        params: dict[str, Any],
    ) -> ChatCompletion:
        completions = self._sync_client.chat.completions
        raw_completions = getattr(completions, "with_raw_response", None)
        if raw_completions is not None:
            raw_response = raw_completions.create(messages=messages, **params)
            return self._coerce_raw_chat_completion_response(raw_response)
        response = completions.create(messages=messages, **params)
        return self._coerce_chat_completion_response(response)

    async def _create_async_chat_completion(
        self,
        *,
        messages: Sequence[ChatCompletionMessageParam],
        params: dict[str, Any],
    ) -> ChatCompletion:
        completions = self._async_client.chat.completions
        raw_completions = getattr(completions, "with_raw_response", None)
        if raw_completions is not None:
            raw_response = await raw_completions.create(messages=messages, **params)
            return await self._acoerce_raw_chat_completion_response(raw_response)
        response = await completions.create(messages=messages, **params)
        return self._coerce_chat_completion_response(response)

    def close(self):
        try:
            self._sync_client.close()
        except Exception as e:
            self.logger.warning(f"Error closing sync client: {e}")

        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                loop.create_task(self._async_client.close())
            else:
                asyncio.run(self._async_client.close())
        except Exception as e:
            self.logger.warning(f"Error closing async client gracefully: {e}")

    async def aclose(self):
        try:
            self._sync_client.close()
            await self._async_client.close()
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
    # Internal single-attempt operations (no retries)
    # ------------------------------------------------------------------
    def _invoke_once(
        self, messages: Sequence[LLMMessage | dict[str, Any]], **kwargs
    ) -> LLMResponse:
        started = time.time()
        msgs = self._prepare_messages(list(messages))
        params = self._prepare_params(stream=False, **kwargs)
        try:
            response = self._create_sync_chat_completion(messages=msgs, params=params)
            return self._handle_sync_response(response, started_at=started)
        except Exception as e:
            if self._should_fallback_to_json_object(e, params):
                self.logger.warning(
                    "LLM upstream does not support json_schema response_format; "
                    "retrying once with json_object."
                )
                fallback_msgs, fallback_params = self._build_json_object_fallback_request(
                    msgs, params
                )
                try:
                    response = self._create_sync_chat_completion(
                        messages=fallback_msgs,
                        params=fallback_params,
                    )
                    return self._handle_sync_response(response, started_at=started)
                except Exception as fallback_error:
                    response = self._coerce_chat_completion_exception(fallback_error)
                    if response is not None:
                        return self._handle_sync_response(
                            response, started_at=started
                        )
                    self.logger.error(
                        f"Sync invoke json_object fallback failed: {fallback_error}"
                    )
                    raise
            response = self._coerce_chat_completion_exception(e)
            if response is not None:
                return self._handle_sync_response(response, started_at=started)
            self.logger.error(f"Sync invoke failed: {e}")
            raise

    async def _ainvoke_once(
        self, messages: Sequence[LLMMessage | dict[str, Any]], **kwargs
    ) -> LLMResponse:
        started = time.time()
        msgs = self._prepare_messages(list(messages))
        params = self._prepare_params(stream=False, **kwargs)

        try:
            response = await self._create_async_chat_completion(
                messages=msgs,
                params=params,
            )
            return self._handle_async_response(response, started_at=started)
        except Exception as e:
            if self._should_fallback_to_json_object(e, params):
                self.logger.warning(
                    "LLM upstream does not support json_schema response_format; "
                    "retrying once with json_object."
                )
                fallback_msgs, fallback_params = self._build_json_object_fallback_request(
                    msgs, params
                )
                try:
                    response = await self._create_async_chat_completion(
                        messages=fallback_msgs,
                        params=fallback_params,
                    )
                    return self._handle_async_response(response, started_at=started)
                except Exception as fallback_error:
                    response = self._coerce_chat_completion_exception(fallback_error)
                    if response is not None:
                        return self._handle_async_response(
                            response, started_at=started
                        )
                    self.logger.error(
                        f"Async invoke json_object fallback failed: {fallback_error}"
                    )
                    raise
            response = self._coerce_chat_completion_exception(e)
            if response is not None:
                return self._handle_async_response(response, started_at=started)
            self.logger.error(
                f"Async invoke failed after {time.time() - started:.2f}s: "
                f"{type(e).__name__}: {e}"
            )
            raise

    def _stream_once(
        self, messages: Sequence[LLMMessage | dict[str, Any]], **kwargs
    ) -> Generator[str, None, None]:
        msgs = self._prepare_messages(list(messages))
        params = self._prepare_params(stream=True, **kwargs)
        try:
            response = self._sync_client.chat.completions.create(
                messages=msgs, **params
            )
            return self._handle_sync_stream(response)
        except Exception as e:
            self.logger.error(f"Sync stream failed: {e}")
            raise

    async def _astream_once(
        self, messages: Sequence[LLMMessage | dict[str, Any]], **kwargs
    ) -> AsyncGenerator[dict[str, Any], None]:
        msgs = self._prepare_messages(list(messages))
        params = self._prepare_params(stream=True, **kwargs)
        # Request usage stats in the final streaming chunk so callers can
        # accumulate token counts even in streaming mode.
        stream_options = params.get("stream_options")
        if isinstance(stream_options, dict):
            params["stream_options"] = {
                **stream_options,
                "include_usage": stream_options.get("include_usage", True),
            }
        else:
            params.setdefault("stream_options", {"include_usage": True})
        try:
            response = await self._async_client.chat.completions.create(
                messages=msgs, **params
            )
            return self._handle_async_stream(response)
        except Exception as e:
            self.logger.error(f"Async stream failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Tenacity hooks
    # ------------------------------------------------------------------
    def _tenacity_before_retry(self, retry_state: RetryCallState):  # sync
        if retry_state.attempt_number == 1:
            return
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if self.before_retry:
            try:
                self.before_retry(
                    RetryState(
                        attempt=retry_state.attempt_number - 1, last_exception=exc
                    )
                )
            except Exception:  # pragma: no cover
                self.logger.debug("before_retry callback failed", exc_info=True)
        if exc:
            self.logger.warning(
                f"Retrying sync attempt {retry_state.attempt_number} due to: {exc}"
            )

    def _atenacity_before_retry(self, retry_state: RetryCallState):  # async path
        if retry_state.attempt_number == 1:
            return
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if self.before_retry:
            try:
                self.before_retry(
                    RetryState(
                        attempt=retry_state.attempt_number - 1, last_exception=exc
                    )
                )
            except Exception:  # pragma: no cover
                self.logger.debug("before_retry callback failed", exc_info=True)
        if exc:
            self.logger.warning(
                f"Retrying async attempt {retry_state.attempt_number} due to: {exc}"
            )

    def _tenacity_error_callback(self, retry_state: RetryCallState):
        # Final failure callback: re-raise original exception if present
        if retry_state.outcome:
            if retry_state.outcome.failed:
                exc = retry_state.outcome.exception()
                if exc is not None:
                    raise exc
            else:
                return retry_state.outcome.result()
        return None


if __name__ == "__main__":
    import asyncio

    from loguru import logger

    from ..config.common import DEEPSEEKV3_LOCAL_CONFIG, DS_V4_FLASH_LLM_CONFIG

    # cfg = LLMConfig(
    #     base_url="http://10.16.12.25:11112/v1/",
    #     model="qwen3.6",
    #     temperature=0.1,
    #     chat_template_kwargs={"enable_thinking": True},
    # )
    cfg = DS_V4_FLASH_LLM_CONFIG

    async def test_async():
        async with LLMEngine(cfg, logger) as engine:
            resp = await engine.ainvoke(
                [LLMMessage(role="user", content="Hello, how are you?")]
            )
            print("Async invoke response:", resp)

            print("Async stream response:")
            async for chunk in engine.astream(
                [LLMMessage(role="user", content="Tell me a joke.")]
            ):
                print(chunk, end="", flush=True)
            print()

    def test_sync():
        with LLMEngine(cfg, logger) as engine:
            resp = engine.invoke(
                [LLMMessage(role="user", content="Hello, how are you?")]
            )
            print("Sync invoke response:", resp)

            print("Sync stream response:")
            for chunk in engine.stream(
                [LLMMessage(role="user", content="Tell me a joke.")]
            ):
                print(chunk, end="", flush=True)
            print()

    # test_sync()
    asyncio.run(test_async())
