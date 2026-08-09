"""Pure serialization / formatting helpers shared across analytics domains.

These functions carry no service state (no Mongo / Loki handles) so they can be
unit-tested in isolation and reused by every domain repository/facade.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.services.log_parser import parse_log_context, resolve_correlation_id
from app.services.math_utils import compact_confidences, to_float


def to_token_total(doc: Dict) -> float:
    """Extract the total token usage from a request document."""
    usage = doc.get("token_usage_total") or {}
    total = usage.get("total") if isinstance(usage, dict) else {}
    if not isinstance(total, dict):
        total = {}
    return to_float(total.get("total_tokens"), 0.0)


def to_scene_confidences(doc: Dict) -> Dict[str, List[float]]:
    """Extract big-scene / sub-scene confidence lists from a request document."""
    scene_result = doc.get("scene_result") or {}
    big_scenes = scene_result.get("big_scenes") if isinstance(scene_result, dict) else []
    sub_scenes = scene_result.get("sub_scenes") if isinstance(scene_result, dict) else []
    if not isinstance(big_scenes, list):
        big_scenes = []
    if not isinstance(sub_scenes, list):
        sub_scenes = []

    big_conf = compact_confidences(
        item.get("confidence") for item in big_scenes if isinstance(item, dict)
    )
    sub_conf = compact_confidences(
        item.get("confidence") for item in sub_scenes if isinstance(item, dict)
    )
    return {"big": big_conf, "sub": sub_conf}


def tool_status(raw_status: Optional[str]) -> str:
    """Normalize a tool call status to success / failed / unknown."""
    if raw_status is None:
        return "unknown"

    status = str(raw_status).strip().lower()
    if not status:
        return "unknown"
    if status in {"success", "ok", "done"}:
        return "success"
    return "failed"


def to_utc_dt(value: object) -> Optional[datetime]:
    """Best-effort conversion of a datetime / ISO string to an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def to_ns(value: datetime) -> int:
    """Convert an aware datetime to nanoseconds since epoch."""
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000_000)


def json_default(value: object) -> str:
    """JSON default encoder: datetimes become ISO-8601 UTC (Z suffix)."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def is_retryable_loki_error(exc: RuntimeError) -> bool:
    """Whether a Loki query error should be treated as retryable (timeouts / 5xx)."""
    detail = str(exc).strip().lower()
    return any(
        token in detail
        for token in (
            "timed out",
            "timeout",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def extract_request_ids_from_rows(rows: List[Dict]) -> set[str]:
    """Parse request ids (rid/task_id/req_id...) out of raw Loki log rows."""
    result: set[str] = set()
    for row in rows:
        line = str(row.get("line") or "")
        stream = row.get("stream") if isinstance(row.get("stream"), dict) else {}
        parsed = parse_log_context(line, stream=stream)
        resolved = resolve_correlation_id(parsed)
        resolved_id = resolved.get("id_value")
        if resolved_id:
            result.add(str(resolved_id))
    return result


def tool_call_identity(row: Dict) -> Tuple[str, str, str, int]:
    """Identity key used to merge duplicated tool-call rows."""
    agent_code = str(row.get("agent_code") or "unknown_agent")
    tool = str(row.get("tool") or "unknown_tool")
    tool_id = str(row.get("tool_id") or "unknown_id")
    step_raw = row.get("step")
    try:
        step = int(step_raw) if step_raw is not None else -1
    except (TypeError, ValueError):
        step = -1
    return agent_code, tool, tool_id, step


def merge_tool_call_rows(rows: List[Dict]) -> List[Dict]:
    """Merge duplicate tool-call rows sharing the same identity into one."""
    merged: Dict[Tuple[str, str, str, int], Dict] = {}

    for row in rows:
        key = tool_call_identity(row)
        current = merged.get(key)
        if current is None:
            merged[key] = dict(row)
            continue

        status = row.get("status")
        if status not in (None, ""):
            current["status"] = status

        if current.get("args") is None and row.get("args") is not None:
            current["args"] = row.get("args")
        if current.get("output") is None and row.get("output") is not None:
            current["output"] = row.get("output")

        if row.get("duration_s") is not None:
            duration = to_float(row.get("duration_s"))
            current["duration_s"] = duration

        row_ts = to_utc_dt(row.get("ts"))
        cur_ts = to_utc_dt(current.get("ts"))
        if row_ts and (cur_ts is None or row_ts < cur_ts):
            current["ts"] = row.get("ts")

        row_end_ts = to_utc_dt(row.get("end_ts")) or to_utc_dt(row.get("ts"))
        cur_end_ts = to_utc_dt(current.get("end_ts")) or to_utc_dt(current.get("ts"))
        if row_end_ts and (cur_end_ts is None or row_end_ts > cur_end_ts):
            current["end_ts"] = row.get("end_ts") or row.get("ts")

    merged_rows = list(merged.values())
    min_utc = datetime.min.replace(tzinfo=timezone.utc)
    merged_rows.sort(key=lambda item: (to_utc_dt(item.get("ts")) or min_utc, tool_call_identity(item)))
    return merged_rows


def build_agent_timeline(events: List[Dict]) -> List[Dict]:
    """Turn agent execution events into a merged start/end timeline."""
    timeline: List[Dict] = []
    open_items: Dict[tuple, Dict] = {}

    for event in events:
        key = (
            event.get("state_id"),
            event.get("request_id"),
            event.get("agent_code"),
            event.get("event_type"),
            event.get("component"),
        )

        stage = str(event.get("stage") or "").lower()
        ts = event.get("ts")
        item = {
            "state_id": event.get("state_id"),
            "request_id": event.get("request_id"),
            "agent_code": event.get("agent_code"),
            "agent_name": event.get("agent_name"),
            "seq": event.get("seq", 0),
            "event_type": event.get("event_type"),
            "component": event.get("component"),
            "start_ts": None,
            "end_ts": None,
            "status": None,
        }

        if stage == "start":
            item["start_ts"] = ts
            open_items[key] = item
            timeline.append(item)
            continue

        if stage == "end":
            item = open_items.pop(key, item)
            if item not in timeline:
                timeline.append(item)
            item["end_ts"] = ts

        if event.get("status") is not None:
            item["status"] = event.get("status")

        if stage not in {"start", "end"} and event.get("event_type") == "token_usage":
            continue
        if stage not in {"start", "end"} and item not in timeline:
            timeline.append(item)

    for item in timeline:
        start_ts = item.get("start_ts")
        end_ts = item.get("end_ts")
        duration_s = 0.0
        if isinstance(start_ts, datetime) and isinstance(end_ts, datetime):
            duration_s = max((end_ts - start_ts).total_seconds(), 0.0)

        item["duration_s"] = duration_s

    min_utc = datetime.min.replace(tzinfo=timezone.utc)
    timeline.sort(key=lambda row: ((row.get("start_ts") or row.get("end_ts") or min_utc), row.get("seq", 0)))
    return timeline
