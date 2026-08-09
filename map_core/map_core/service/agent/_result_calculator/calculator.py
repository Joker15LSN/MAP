from typing import Any

from map_core.utils.llm_engine import LLMEngine

from .engine import run_calculation
from .extractor import extract_intent


async def calculate(question: str, raw_data: list[dict[str, Any]], llm: LLMEngine) -> dict[str, Any]:
    """
    根据问题和原始数据计算结果。

    Args:
        question: 用户关于指标的自然语言问题。
        raw_data: 原始数据列表，例如 `[{"time": "2026-01", "value": 100}]`。
        llm: LLM 引擎实例。

    Returns:
        包含 `metric`, `calculation_type` 和 `results` 的字典。如果拒绝计算，则返回包含 `error` 的字典。
    """
    # 1. 解析意图
    intent = await extract_intent(question, llm)

    print(intent)

    # 一个问题只能包含一种 calculation_type
    if intent.has_multiple_types:
        return {"error": "Refused: Multiple calculation types requested."}

    if intent.calculation_type == "unknown":
        return {"error": "Refused: Unknown calculation type."}

    # 2. 根据解析结果进行计算
    results = run_calculation(intent, raw_data)

    return {
        "metric": intent.metric,
        "calculation_type": intent.calculation_type,
        "results": results,
    }
