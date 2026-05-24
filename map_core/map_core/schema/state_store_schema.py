from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class AgentExecutionDocument:
    """Mongo schema for the agent_executions collection."""

    state_id: str
    request_id: str | None = None
    session_id: str | None = None
    staff_code: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    agent_code: str | None = None
    agent_name: str | None = None
    seq: int = 0
    event_type: str = ""
    component: str | None = None
    stage: str | None = None
    status: str | None = None
    payload: Any = None
    ts: datetime = field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )


@dataclass
class ToolCallRecordDocument:
    """Mongo schema for the tool_call_records collection."""

    event_type: str
    state_id: str
    request_id: str | None = None
    session_id: str | None = None
    ts: datetime = field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    agent_code: str | None = None
    agent_name: str | None = None
    agent_id: str | None = None
    tool: str | None = None
    tool_id: str | None = None
    step: int | None = None
    args: Any = None
    output: Any = None
    status: str | None = None


@dataclass
class RequestRecordDocument:
    """Mongo schema for the request_records collection."""

    state_id: str
    request_id: str
    session_id: str | None = None
    staff_code: str | None = None
    query: str | None = None
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    status: str | None = None
    duration_s: float | None = None
    scene_result: Any = None
    agents_called: list[str] | Any = None
    token_usage_total: Any = None
    error: Any = None
