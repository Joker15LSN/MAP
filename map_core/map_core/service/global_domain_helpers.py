from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from ..schema.attachment_schema import AttachmentSchema
from ..schema.global_domain_schema import GlobalDomainStreamEvent
from ..schema.state_event_schema import AgentEventSchema
from ..schema.tool_extra_result_schema import ToolExtraResultSchema
from .state_store import fire_and_forget


def build_dispatch_token_meta(results: list[Any]) -> dict[str, Any]:
    """Extract aggregated token_usage from dispatch results for event meta."""
    total: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}

    for res in results or []:
        usage = getattr(res, "meta_data", {}).get("token_usage") or {}
        if usage and hasattr(res, "name"):
            by_agent[res.name] = usage
            for key, value in usage.items():
                total[key] = total.get(key, 0) + value

    if not total:
        return {}
    return {"token_usage": {"total": total, "by_agent": by_agent}}


def serialize_attachment_results(
    attachment_results: list[AttachmentSchema] | None,
) -> list[dict[str, Any]] | None:
    """Produce event payloads by converting attachment models to plain dicts.

    This is used on the event producer side (write-out path), e.g. when building
    SSE `done` event data.
    """
    if attachment_results is None:
        return None
    return [item.model_dump() for item in attachment_results]


def normalize_attachment_results(
    value: Any,
) -> list[AttachmentSchema] | None:
    """Consume event payloads by converting loose data back to typed models.

    This is used on the event consumer side (read-back path), e.g. when
    `consume_event_stream` reconstructs `attachment_results` from `event.data`.
    """
    if not isinstance(value, list):
        return None

    normalized: list[AttachmentSchema] = []
    for item in value:
        try:
            normalized.append(AttachmentSchema.model_validate(item))
        except ValidationError:
            continue

    return normalized or None


def serialize_tool_extra_results(
    tool_extra_results: list[ToolExtraResultSchema] | None,
) -> list[dict[str, Any]] | None:
    if tool_extra_results is None:
        return None
    return [item.model_dump() for item in tool_extra_results]


def normalize_tool_extra_results(
    value: Any,
) -> list[ToolExtraResultSchema] | None:
    if not isinstance(value, list):
        return None

    normalized: list[ToolExtraResultSchema] = []
    for item in value:
        try:
            normalized.append(ToolExtraResultSchema.model_validate(item))
        except ValidationError:
            continue

    return normalized or None


def stream_event_data_as_dict(event: GlobalDomainStreamEvent) -> dict[str, Any]:
    data = event.data
    if isinstance(data, dict):
        return data
    if isinstance(data, BaseModel):
        return data.model_dump()
    return dict(data)


def record_summarize_start(
    *,
    state_store: Any,
    state_id: str,
    base_state: dict[str, Any],
    summarize_input: dict[str, Any],
) -> None:
    fire_and_forget(
        state_store.record_event(
            state_id=state_id,
            event_type="summarize_agent",
            payload=AgentEventSchema(
                category="workflow",
                component="summarize_agent",
                stage="start",
                data={"input": summarize_input},
            ).model_dump(),
            base_state=base_state,
        )
    )


def record_summarize_success(
    *,
    state_store: Any,
    state_id: str,
    output: str,
    start_ts: datetime,
    stream: bool,
) -> None:
    end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
    fire_and_forget(
        state_store.record_event(
            state_id=state_id,
            event_type="summarize_agent",
            payload=AgentEventSchema(
                timestamp=end_ts,
                category="workflow",
                component="summarize_agent",
                stage="end",
                status="success",
                data={
                    "output": output,
                    "meta": {
                        "duration_s": (end_ts - start_ts).total_seconds(),
                        "stream": stream,
                    },
                },
            ).model_dump(),
        )
    )


def record_summarize_failure(
    *,
    state_store: Any,
    state_id: str,
    summarize_input: dict[str, Any],
    start_ts: datetime,
    error: Exception | str,
    stream: bool,
) -> None:
    end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
    error_message = str(error)
    error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
    fire_and_forget(
        state_store.record_event(
            state_id=state_id,
            event_type="summarize_agent",
            payload=AgentEventSchema(
                timestamp=end_ts,
                category="error",
                component="summarize_agent",
                stage="end",
                status="failed",
                data={
                    "input": summarize_input,
                    "error": error_message,
                    "error_type": error_type,
                    "meta": {
                        "duration_s": (end_ts - start_ts).total_seconds(),
                        "stream": stream,
                    },
                },
            ).model_dump(),
        )
    )
