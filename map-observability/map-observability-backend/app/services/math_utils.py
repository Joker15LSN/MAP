from __future__ import annotations

import math
from collections.abc import Iterable


def safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def to_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value):
            return default
        return float(value)
    try:
        return float(value)
    except Exception:  # noqa: BLE001 - boundary catch
        return default


def percentile(values: Iterable[float], ratio: float) -> float:
    sorted_values = sorted(float(v) for v in values)
    if not sorted_values:
        return 0.0

    if ratio <= 0:
        return sorted_values[0]
    if ratio >= 1:
        return sorted_values[-1]

    pos = (len(sorted_values) - 1) * ratio
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return sorted_values[int(pos)]

    lower_val = sorted_values[lower]
    upper_val = sorted_values[upper]
    weight = pos - lower
    return lower_val * (1 - weight) + upper_val * weight


def average(values: Iterable[float]) -> float:
    nums = [float(v) for v in values]
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def compact_confidences(confidences: Iterable[float | None]) -> list[float]:
    values = []
    for value in confidences:
        if value is None:
            continue
        try:
            values.append(float(value))
        except Exception:  # noqa: BLE001 - boundary catch
            continue
    return values
