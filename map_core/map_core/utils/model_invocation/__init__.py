"""Typed async ModelInvocation module (design A)."""

from .engine import ModelInvocation, ModelInvocationStream
from .openai_compatible import OpenAICompatibleProvider
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
    ModelInvocationFailedError,
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelMessage,
    ModelUsage,
    ProviderParams,
    StructuredOutput,
    ToolChoice,
    ToolSpec,
)

__all__ = [
    "ModelInvocation",
    "ModelInvocationStream",
    "ModelInvocationError",
    "ModelInvocationEvent",
    "ModelInvocationFailedError",
    "ModelInvocationOutcome",
    "ModelInvocationRequest",
    "ModelMessage",
    "ModelProvider",
    "ModelUsage",
    "OpenAICompatibleProvider",
    "PreparedRequest",
    "ProviderError",
    "ProviderParams",
    "ProviderResponse",
    "ProviderStream",
    "StructuredOutput",
    "ToolChoice",
    "ToolSpec",
]
