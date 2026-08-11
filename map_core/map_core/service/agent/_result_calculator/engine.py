import json
from datetime import datetime
from typing import Any

from dateutil.relativedelta import relativedelta

from .extractor import CalculationIntent


def parse_time(time_str: str) -> datetime | None:
    """Parse string time into datetime based on expected length/format."""
    try:
        if len(time_str) == 4:
            return datetime.strptime(time_str, "%Y")
        elif len(time_str) == 7:
            if "-Q" in time_str:
                year, q = time_str.split("-Q")
                month = (int(q) - 1) * 3 + 1
                return datetime(int(year), month, 1)
            else:
                return datetime.strptime(time_str, "%Y-%m")
        elif len(time_str) == 10:
            return datetime.strptime(time_str, "%Y-%m-%d")
        else:
            return None
    except ValueError:
        return None


def get_previous_time_str(time_str: str, calculation_type: str) -> str | None:
    """Calculate the previous time string based on the current time and calculation type."""
    dt = parse_time(time_str)
    if not dt:
        return None

    delta = None
    if calculation_type in ("yoy", "yoy_monthly", "yoy_quarterly", "yoy_daily"):
        delta = relativedelta(years=-1)
    elif calculation_type == "mom":
        delta = relativedelta(months=-1)
    elif calculation_type == "qoq":
        delta = relativedelta(months=-3)
    elif calculation_type == "dod":
        delta = relativedelta(days=-1)

    if delta is None:
        return None

    prev_dt = dt + delta

    # Format it back based on original string format
    if "-Q" in time_str:
        quarter = (prev_dt.month - 1) // 3 + 1
        return f"{prev_dt.year}-Q{quarter}"
    elif len(time_str) == 4:
        return prev_dt.strftime("%Y")
    elif len(time_str) == 7:
        return prev_dt.strftime("%Y-%m")
    elif len(time_str) == 10:
        return prev_dt.strftime("%Y-%m-%d")

    return None


def aggregate_raw_data(
    raw_data: list[dict[str, Any]], calc_type: str
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """Aggregate raw data to the appropriate granularity based on calculation type.
    Returns the aggregated data and a map of the components (e.g., months in a year) that made up the aggregation.
    """
    granularity = None
    if calc_type == "yoy":
        granularity = "year"
    elif calc_type in ("yoy_monthly", "mom"):
        granularity = "month"
    elif calc_type in ("yoy_quarterly", "qoq"):
        granularity = "quarter"
    elif calc_type in ("yoy_daily", "dod"):
        granularity = "day"
    else:
        return raw_data, {}

    aggregated = {}
    components: dict[str, set[str]] = {}
    for item in raw_data:
        t_str = item.get("time")
        val = item.get("value")
        if not t_str or not isinstance(val, (int, float)):
            continue

        dt = parse_time(t_str)
        if not dt:
            continue

        agg_key = None
        sub_key = None
        if granularity == "year":
            agg_key = dt.strftime("%Y")
            sub_key = dt.strftime("%m-%d") if len(t_str) == 10 else (dt.strftime("%m") if len(t_str) == 7 else "whole_year")
        elif granularity == "month":
            agg_key = dt.strftime("%Y-%m")
            sub_key = dt.strftime("%d") if len(t_str) == 10 else "whole_month"
        elif granularity == "quarter":
            quarter = (dt.month - 1) // 3 + 1
            agg_key = f"{dt.year}-Q{quarter}"
            rel_month = (dt.month - 1) % 3 + 1
            sub_key = f"{rel_month}-{dt.strftime('%d')}" if len(t_str) == 10 else (str(rel_month) if len(t_str) == 7 else "whole_quarter")
        elif granularity == "day":
            agg_key = dt.strftime("%Y-%m-%d")
            sub_key = "whole_day"

        if agg_key:
            if agg_key not in aggregated:
                aggregated[agg_key] = 0
                components[agg_key] = set()
            aggregated[agg_key] += val
            if sub_key:
                components[agg_key].add(sub_key)

    return [{"time": k, "value": v} for k, v in aggregated.items()], components


def run_calculation(
    intent: CalculationIntent, raw_data: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute the calculation on raw data based on the extracted intent."""
    calc_type = intent.calculation_type
    results = {}

    # Clean raw_data and cast values to float
    clean_data = []
    for item in raw_data:
        t_str = item.get("time")
        val = item.get("value")
        if val is None:
            continue
        try:
            clean_data.append({
                "time": t_str,
                "value": float(val),
                "extra": item.get("extra"),
                "type": item.get("type")
            })
        except (ValueError, TypeError):
            pass

    # 1. First aggregate the data to the correct granularity
    aggregated_data, components_map = aggregate_raw_data(clean_data, calc_type)

    if calc_type == "sum":
        total = sum(
            item.get("value", 0)
            for item in aggregated_data
            if isinstance(item.get("value"), (int, float))
        )
        results["sum"] = total
        return results

    if calc_type == "avg":
        values = [
            item.get("value")
            for item in aggregated_data
            if isinstance(item.get("value"), (int, float))
        ]
        if values:
            results["avg"] = sum(values) / len(values)
        return results

    if calc_type == "percentage":
        sum_item = next((item for item in aggregated_data if item.get("type") == "sum"), None)
        if sum_item is not None:
            total = sum_item.get("value", 0)
        else:
            total = sum(
                item.get("value", 0)
                for item in aggregated_data
                if isinstance(item.get("value"), (int, float))
            )

        if total == 0:
            return {}

        for item in aggregated_data:
            if item.get("type") == "sum":
                continue

            val = item.get("value")
            if isinstance(val, (int, float)):
                extra = item.get("extra")
                part = json.dumps(extra, ensure_ascii=False) if extra else str(item.get("time"))
                results[f"percentage_{part}"] = (val / total) * 100
        return results

    # For relative calculations (YoY, MoM, etc.)
    # Build a lookup map of time to value
    value_map = {
        item["time"]: item["value"]
        for item in aggregated_data
        if "time" in item
        and "value" in item
        and isinstance(item["value"], (int, float))
    }

    for item in aggregated_data:
        t = item.get("time")
        val = item.get("value")
        if not t or not isinstance(val, (int, float)):
            continue

        prev_t = get_previous_time_str(t, calc_type)
        if prev_t and prev_t in value_map:
            # Check if components match perfectly (e.g., both 2025 and 2026 contain exactly Jan and Feb)
            if components_map and t in components_map and prev_t in components_map:
                if components_map[t] != components_map[prev_t]:
                    continue

            prev_val = value_map[prev_t]
            if prev_val != 0:
                growth = ((val - prev_val) / prev_val) * 100
                if calc_type == "yoy_monthly":
                    results[f"yoy_monthly_{t}"] = growth
                elif calc_type == "yoy_quarterly":
                    results[f"yoy_quarterly_{t}"] = growth
                else:
                    results[f"{calc_type}_{t}"] = growth

    return results
