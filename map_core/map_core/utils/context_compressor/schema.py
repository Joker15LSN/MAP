from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class ContextCompressorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    trigger_message_count: PositiveInt = 20
    trigger_char_count: PositiveInt = 24_000
    preserve_recent_messages: int = Field(default=8, ge=0)
    max_render_chars_per_message: PositiveInt = 4_000
    max_input_chars: PositiveInt = 60_000
    max_summary_chars: PositiveInt = 6_000
    timeout: float = Field(default=60.0, gt=0)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: PositiveInt = 2_048
    raise_on_error: bool = False


class ContextCompressionLLMOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str = ""
    user_preferences: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ContextCompressionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compressed_history: list[dict[str, Any]]
    summary: str | None = None
    preserved_messages: list[dict[str, Any]] = Field(default_factory=list)
    original_message_count: int = 0
    compressed_message_count: int = 0
    original_chars: int = 0
    compressed_chars: int = 0
    skipped: bool = False
    reason: str | None = None
    usage: dict[str, int] | None = None
