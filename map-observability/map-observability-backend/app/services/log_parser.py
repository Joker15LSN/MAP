from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Dict, Optional

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
RID_PATTERN = re.compile(r"\brid\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
TASK_ID_PATTERN = re.compile(r"\btask_id\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
REQUEST_ID_PATTERN = re.compile(r"\brequest_id\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
REQ_ID_PATTERN = re.compile(r"\breq_id\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
SID_PATTERN = re.compile(r"\bsid\s*[:=]\s*([A-Za-z0-9_-]{4,128})", re.IGNORECASE)
AID_PATTERN = re.compile(r"\baid\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
PARID_PATTERN = re.compile(r"\bparid\s*[:=]\s*([A-Za-z0-9_-]{1,128})", re.IGNORECASE)
LEVEL_PIPE_PATTERN = re.compile(r"\|\s*(INFO|WARNING|WARN|ERROR|DEBUG)\s*\|", re.IGNORECASE)
LEVEL_WORD_PATTERN = re.compile(r"\b(INFO|WARNING|WARN|ERROR|DEBUG)\b", re.IGNORECASE)

ALLOWED_LEVELS = ("INFO", "WARNING", "ERROR", "DEBUG", "UNKNOWN")
UNKNOWN_TASK_ID_VALUES = {"UNKNOWN_TASK", "UNKNOWN", "-", "NONE", "NULL"}


def strip_ansi(value: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", value or "")


def normalize_level(value: Optional[str]) -> str:
    if not value:
        return "UNKNOWN"

    normalized = str(value).strip().upper()
    if normalized == "WARN":
        return "WARNING"
    if normalized in ALLOWED_LEVELS:
        return normalized
    return "UNKNOWN"


def extract_level_from_text(line: str) -> str:
    cleaned = strip_ansi(line)
    pipe_match = LEVEL_PIPE_PATTERN.search(cleaned)
    if pipe_match:
        return normalize_level(pipe_match.group(1))

    text_match = LEVEL_WORD_PATTERN.search(cleaned)
    if text_match:
        return normalize_level(text_match.group(1))

    return "UNKNOWN"


def _normalize_token(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _is_unknown_task_id(value: Optional[str]) -> bool:
    normalized = str(value or "").strip().upper()
    return not normalized or normalized in UNKNOWN_TASK_ID_VALUES


def resolve_correlation_id(parsed: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    rid = _normalize_token(parsed.get("rid"))
    task_id = _normalize_token(parsed.get("task_id"))
    request_id = _normalize_token(parsed.get("request_id"))
    req_id = _normalize_token(parsed.get("req_id"))

    if rid:
        return {"id_value": rid, "id_source": "rid"}
    if task_id and not _is_unknown_task_id(task_id):
        return {"id_value": task_id, "id_source": "task_id"}
    if request_id:
        return {"id_value": request_id, "id_source": "request_id"}
    if req_id:
        return {"id_value": req_id, "id_source": "req_id"}
    return {"id_value": None, "id_source": None}


def parse_log_context(line: str, stream: Optional[Dict] = None) -> Dict[str, Optional[str]]:
    cleaned = strip_ansi(line)

    rid_match = RID_PATTERN.search(cleaned)
    task_id_match = TASK_ID_PATTERN.search(cleaned)
    request_id_match = REQUEST_ID_PATTERN.search(cleaned)
    req_id_match = REQ_ID_PATTERN.search(cleaned)
    sid_match = SID_PATTERN.search(cleaned)
    aid_match = AID_PATTERN.search(cleaned)
    parid_match = PARID_PATTERN.search(cleaned)

    stream_level = normalize_level((stream or {}).get("detected_level") if isinstance(stream, dict) else None)
    text_level = extract_level_from_text(cleaned)
    final_level = stream_level if stream_level != "UNKNOWN" else text_level

    parsed = {
        "clean_line": cleaned,
        "rid": rid_match.group(1) if rid_match else None,
        "task_id": task_id_match.group(1) if task_id_match else None,
        "request_id": request_id_match.group(1) if request_id_match else None,
        "req_id": req_id_match.group(1) if req_id_match else None,
        "sid": sid_match.group(1) if sid_match else None,
        "aid": aid_match.group(1) if aid_match else None,
        "parid": parid_match.group(1) if parid_match else None,
        "level": final_level,
    }
    resolved = resolve_correlation_id(parsed)

    return {
        **parsed,
        "correlation_id": resolved["id_value"],
        "correlation_id_source": resolved["id_source"],
    }


def normalize_levels(levels: Optional[Iterable[str]]) -> list[str]:
    if not levels:
        return []

    result: list[str] = []
    seen = set()
    for level in levels:
        normalized = normalize_level(level)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
