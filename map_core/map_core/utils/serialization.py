"""Shared JSON-safe serialization helper.

``safe_serialize`` recursively converts Pydantic models, lists, tuples and
dicts into canonical-JSON-friendly values so event payloads and logs never
carry live model objects.  It lives in the neutral ``utils`` layer so core
modules can use it without taking a dependency on the legacy store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def safe_serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return safe_serialize(value.model_dump())
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple)):
        return [safe_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(k): safe_serialize(v) for k, v in value.items()}
    return str(value)
