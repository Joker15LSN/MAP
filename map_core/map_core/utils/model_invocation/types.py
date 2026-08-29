"""Typed models for the ModelInvocation module (design A)."""

from __future__ import annotations

import asyncio
from typing import Any, Generic, Literal, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field

S = TypeVar("S")


class ModelMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None  # role=="tool"
    tool_calls: Any | None = None  # assistant tool_calls
    function_call: Any | None = None  # legacy assistant function_call


class ToolSpec(BaseModel):
    type: Literal["function"] = "function"
    function: dict[str, Any]  # {"name","description","parameters"}


ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]


class StructuredOutput(BaseModel):
    # ``schema`` is kept as the public constructor keyword (design A) while
    # the storage field avoids shadowing the deprecated BaseModel.schema.
    model_config = ConfigDict(populate_by_name=True)

    json_schema: dict[str, Any] = Field(alias="schema")
    name: str = "response_schema"
    strict: bool = False
    parse: bool = True  # False preserves legacy shell behavior: caller parses content itself


class ProviderParams(BaseModel):
    """Caller-facing parameters that were actually used via _prepare_params."""

    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    extra_body: dict[str, Any] | None = None  # thinking / chat_template_kwargs
    stream_options: dict[str, Any] | None = None


class ModelInvocationRequest(BaseModel, Generic[S]):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: Sequence[ModelMessage | dict[str, Any]]
    stream: S = False
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: Sequence[ToolSpec | dict[str, Any]] | None = None
    tool_choice: ToolChoice = "auto"
    structured: StructuredOutput | None = None  # mutually exclusive with tools
    provider_params: ProviderParams | None = None
    timeout: float | None = None
    cancel: asyncio.Event | None = None
    raw_compat: bool = True  # False 时 outcome.raw=None 省内存


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class ModelInvocationError(BaseModel):
    code: Literal[
        "invalid_request",
        "rate_limited",
        "timeout",
        "provider_error",
        "stream_parse",
        "invalid_response",
        "structured_refused",
        "cancelled",
        "unknown",
    ]
    message: str
    retryable: bool
    provider_status: int | None = None
    body: dict[str, Any] | None = None


class ModelInvocationOutcome(BaseModel):
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None  # neutral tool call dicts
    structured: Any | None = None
    usage: ModelUsage | None = None
    finish_reason: str | None = None
    model: str | None = None
    request_id: str | None = None
    attempts: int
    latency_ms: float
    raw: dict[str, Any] | None = None  # _dump_compat 后的中性 dict
    error: ModelInvocationError | None = None


class ModelInvocationEvent(BaseModel):
    type: Literal["content", "reasoning", "usage", "terminal"]
    data: dict[str, Any] | None = None  # content: 保留旧 astream chunk 信息
    status: Literal["succeeded", "failed", "cancelled", "unknown"] | None = None
    error: ModelInvocationError | None = None  # terminal failed/cancelled 时
    usage: ModelUsage | None = None  # terminal succeeded 时
