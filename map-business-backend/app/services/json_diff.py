"""Stable JSON Patch diff (FIX-P1-AUDIT-01).

RFC 6902-style operations with list items aligned by a stable business
key (id/code/name/model_id/...), so a reorder never emits a whole-list
delete/add. Sensitive values are redacted before diffing (the caller
sanitizes the state via :func:`app.core.redaction.redact_payload`).
"""

from __future__ import annotations

from typing import Any

_STABLE_KEYS = (
    "id",
    "code",
    "name",
    "key",
    "model_id",
    "agent_code",
    "agent_id",
    "scenario_id",
    "category",
    "title",
    "version",
)


def _list_key(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in _STABLE_KEYS:
        value = item.get(key)
        if value is not None:
            return f"{key}:{value}"
    return None


def json_patch_diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """Return a stable, redacted JSON Patch between ``before`` and ``after``."""
    operations: list[dict] = []

    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}/{key}"
            if key not in before:
                operations.append({"op": "add", "path": child_path, "value": after[key]})
            elif key not in after:
                operations.append({"op": "remove", "path": child_path})
            elif before[key] != after[key]:
                operations.extend(
                    json_patch_diff(before[key], after[key], child_path)
                )
        return operations

    if isinstance(before, list) and isinstance(after, list):
        before_items = {_list_key(item) or str(i): item for i, item in enumerate(before)}
        after_items = {_list_key(item) or str(i): item for i, item in enumerate(after)}
        for key in sorted(set(before_items) | set(after_items)):
            child_path = f"{path}/{key}"
            if key not in before_items:
                operations.append(
                    {"op": "add", "path": child_path, "value": after_items[key]}
                )
            elif key not in after_items:
                operations.append({"op": "remove", "path": child_path})
            elif before_items[key] != after_items[key]:
                operations.extend(
                    json_patch_diff(before_items[key], after_items[key], child_path)
                )
        return operations

    if before != after:
        operations.append({"op": "replace", "path": path or "/", "value": after})
    return operations
