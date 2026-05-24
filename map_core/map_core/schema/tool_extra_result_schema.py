from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolExtraResultSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    extra_result: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str | None = None
    step: int | None = None
