from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class AgentEventSchema(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    category: Literal[
        "lifecycle", "system", "workflow", "agent", "tool", "llm", "error"
    ]
    component: str
    stage: Literal["start", "end"] | None = None
    status: Literal["success", "failed"] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ToolEventData(BaseModel):
    model_config = ConfigDict(extra="allow")

    tool: str
    step: int | None = None
    tool_id: str | None = None
    tool_name: str | None = None


class ToolCallData(ToolEventData):
    args: Any = None


class ToolResultData(ToolEventData):
    output: Any = None
