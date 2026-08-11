import json
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from map_core.database.milvus import MilvusClient
from map_core.utils.llm_engine import LLMEngine

from ._final_result_parser import parse_final_results
from .calculator import calculate
from .extractor import _parse_code_block


class SubQuestionSelection(BaseModel):

    reasoning: str = Field(description="思考过程：分析问题需要哪些数据，以及列表中提供了哪些数据")

    selected_indices: list[int] = Field(description="被选中的执行结果在列表中的索引（0-indexed）")

    sum_index: int | None = Field(
        default=None,
        description="如果问题是计算占比（如“A部门占总合同额的比例”），并且子问题列表中包含“总计/总体”数据的子问题，请将该总计数据对应的索引填入 sum_index。同时，请注意必须从 selected_indices 中排除该 sum_index。"
    )



async def pick_necessary_results(original_question: str, executed_results: list[dict], llm: LLMEngine) -> list[dict]:
    if not executed_results:
        return []

    results_summary = []
    for i, res in enumerate(executed_results):
        results_summary.append({
            "index": i,
            "sub_question": res.get("question"),
            "has_data": bool(res.get("data")),
            "error": res.get("error")
        })

    logger.debug(f"Result summary: {results_summary}")
    prompt = f"""给定的原始问题是："{original_question}"

为了计算这个问题的最终结果，我们已经执行了若干个子问题。以下是执行的子问题列表及其状态：
{json.dumps(results_summary, indent=2, ensure_ascii=False)}

请分析原始问题，并从上面的子问题列表中，挑选出为了计算最终结果所**必须**的子问题。
返回你挑选出的子问题对应的 index 列表。如果没有任何子问题是有用的，返回空列表。
注意：如果子问题有 error 或者 has_data 为 false，通常代表它没有成功获取数据，请谨慎选择。

请务必以 JSON 格式输出结果，严格遵循以下结构：
{{
    "reasoning": "思考过程：分析问题需要哪些数据，以及列表中提供了哪些数据",
    "selected_indices": [],
    "sum_index": null
}}

说明：
1. selected_indices 表示 被选中的执行结果在列表中的索引（0-indexed）
2. sum_index 表示 如果问题是计算占比，并且子问题列表中包含“总计/总体”数据的子问题，请将该总计数据对应的索引填入 sum_index。如果最终没有总体数据的子问题，或者问题不是计算占比，则 sum_index 填 null。注意：如果有 sum_index，必须将其从 selected_indices 中排除！
3. 如果原问题为“查询收入”，而子问题中包括“查询公司销售收入”和“查询各个行业的销售收入”，只需要选择“查询公司销售收入”即可，因为“查询各个行业的销售收入”是“查询公司销售收入”的细分，选择会导致重复计算！
"""
    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        content = response.content
        parsed_content = _parse_code_block(content)
        data = json.loads(parsed_content)
        selection = SubQuestionSelection.model_validate(data)
    except Exception as e:
        logger.error(f"Failed to parse LLM response for pick necessary results: {e}")
        return []

    logger.info(
        f"[PickNecessaryResults] Selected indices: {selection.selected_indices} "
        f"| Reasoning: {selection.reasoning} "
        f"| Sum Index: {selection.sum_index}"
    )

    picked = []
    for idx in selection.selected_indices:
        if 0 <= idx < len(executed_results):
            picked.append(executed_results[idx])

    if selection.sum_index is not None and 0 <= selection.sum_index < len(executed_results):
        sum_result = executed_results[selection.sum_index].copy()
        sum_result["type"] = "sum"
        picked.append(sum_result)

    return picked


async def calculate_final_metric_result(
    original_question: str,
    executed_results: list[dict],
    milvus_client: MilvusClient,
    llm: LLMEngine,
    query_mode: Literal["draft", "published"],
) -> dict[str, Any]:
    """
    Given original question and executed result list, pick necessary sub-questions in result list, parse them, and calculate.
    """
    # 1. Pick necessary sub-questions
    picked_results = await pick_necessary_results(original_question, executed_results, llm)
    if not picked_results:
        return {"error": "Refused: LLM selected no valid sub-questions for calculation."}

    # 2. Parse final results
    parsed_data = await parse_final_results(milvus_client, picked_results, query_mode)

    debug_msg = f"parsed_data: {parsed_data}"
    logger.debug(debug_msg)
    if not parsed_data:
        return {"error": "Refused: Parsed raw data is empty."}

    # 3. Calculate the metric
    final_result = await calculate(original_question, parsed_data, llm)

    return final_result