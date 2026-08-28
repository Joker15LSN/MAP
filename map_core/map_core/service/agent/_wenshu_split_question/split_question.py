import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from loguru import logger

from map_core.database.milvus import MilvusClient
from map_core.utils.llm_engine import LLMEngine

from ._match_dimension_values import find_matched_dimension_values
from ._prompts import (
    BUSINESS_KNOWLEDGE_CONTEXT,
    GENERATE_SUB_QUESTIONS_SYSTEM_PROMPT,
    GENERATE_SUB_QUESTIONS_USER_PROMPT,
    IDENTIFY_METRICS_SYSTEM_PROMPT,
    IDENTIFY_METRICS_USER_PROMPT,
    SPLIT_QUESTION_EXAMPLES,
    SPLIT_QUESTION_INSTRUCTIONS,
)
from ._schema import (
    DIMENSION_DETAIL_COLLECTION_PREFIX,
    DIMENSION_INFO_DRAFT_COLLECTION,
    DIMENSION_INFO_PUBLISHED_COLLECTION,
    METRIC_DEFINITION_FIELD_NAME,
    METRIC_INFO_DRAFT_COLLECTION,
    METRIC_INFO_PUBLISHED_COLLECTION,
    VERSION,
    IdentifyMetricsResponse,
    SubQuestionResponse,
)

METRIC_OUTPUT_FIELDS = [
    "metric_code",
    "metric_name",
    METRIC_DEFINITION_FIELD_NAME,
    "dimension_codes",
]

if VERSION == "v1":
    # Constants
    METRIC_OUTPUT_FIELDS = [
        "metric_code",
        "metric_name",
        METRIC_DEFINITION_FIELD_NAME,
        "dimension_code",
    ]
DIMENSION_OUTPUT_FIELDS = ["dimension_code", "dimension_name"]

MSG_HEADER = "[WenshuAgent]:split_question"

def _parse_code_block(content: str) -> str:
    # 提取多行 markdown json block 中的内容
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


async def _get_all_metrics_info(
    milvus_client: MilvusClient,
    query_mode: Literal["publish", "edit"],
) -> list[dict[str, Any]]:
    """Retrieve all unique metrics."""
    _aclient = milvus_client._client
    if not _aclient:
        return []

    metric_collection_name = (
        METRIC_INFO_PUBLISHED_COLLECTION
        if query_mode == "publish"
        else METRIC_INFO_DRAFT_COLLECTION
    )

    metric_results = await _aclient.query(
        collection_name=metric_collection_name,
        filter="",
        output_fields=METRIC_OUTPUT_FIELDS,
        limit=10000,
    )

    unique_metrics: list[dict[str, Any]] = []
    seen_metric_codes: set[str] = set()

    for metric in metric_results:
        metric_code = metric.get("metric_code")
        if not metric_code or metric_code in seen_metric_codes:
            continue

        seen_metric_codes.add(metric_code)
        if VERSION == "v1":
            dimension_code_str: str = metric.get("dimension_code", "")
            dimension_codes: list[str] = [
                dc.strip() for dc in dimension_code_str.split("|") if dc.strip()
            ]
        else:
            dimension_codes: list[str] = metric.get("dimension_codes", [])

        unique_metrics.append(
            {
                "metric_code": metric_code,
                "metric_name": metric.get("metric_name"),
                "metric_meaning": metric.get(METRIC_DEFINITION_FIELD_NAME),
                "dimension_codes": dimension_codes,
            }
        )

    return unique_metrics


async def _get_all_dimensions_info(
    milvus_client: MilvusClient,
    query_mode: Literal["publish", "edit"],
) -> dict[str, dict[str, Any]]:
    """Retrieve all unique dimensions."""
    _aclient = milvus_client._client
    if not _aclient:
        return {}

    dimension_collection_name = (
        DIMENSION_INFO_PUBLISHED_COLLECTION
        if query_mode == "publish"
        else DIMENSION_INFO_DRAFT_COLLECTION
    )

    dimension_results = await _aclient.query(
        collection_name=dimension_collection_name,
        filter="",
        output_fields=DIMENSION_OUTPUT_FIELDS,
        limit=10000,
    )

    dimension_code_to_name = {
        d["dimension_code"]: d["dimension_name"] for d in dimension_results
    }

    dimension_codes = sorted({d["dimension_code"] for d in dimension_results})

    async def _get_dimension_values(dimension_code: str) -> list[str]:
        dimension_collection = f"{DIMENSION_DETAIL_COLLECTION_PREFIX}{dimension_code}"
        try:
            if not await _aclient.has_collection(dimension_collection):
                # logger.debug(f"Dimension collection {dimension_collection} does not exist")
                return []
            dimension_value_results = await _aclient.query(
                collection_name=dimension_collection,
                filter="dimension_type == 'type/Category'",
                output_fields=["dimension_value"],
            )
            dimension_values = [_r["dimension_value"] for _r in dimension_value_results]
            dimension_values = [v.strip() for v in dimension_values if v.strip()]
            dimension_values.sort()
            return dimension_values
        except Exception as e:
            logger.warning(f"{MSG_HEADER} Error getting dimension values for {dimension_code}: {e}")
            return []

    tasks = [
        _get_dimension_values(dimension_code) for dimension_code in dimension_codes
    ]
    dimension_values_list = await asyncio.gather(*tasks, return_exceptions=True)

    all_dimensions_info: dict[str, dict] = {}
    for dimension_code, dimension_values in zip(
        dimension_codes, dimension_values_list
    ):
        if isinstance(dimension_values, BaseException):
            continue
        if len(dimension_values) > 100 or len(dimension_values) == 0:
            continue
        all_dimensions_info[dimension_code] = {
            "dimension_name": dimension_code_to_name[dimension_code],
            "dimension_values": dimension_values,
        }

    return all_dimensions_info


async def _get_matching_dimension_info(
    question: str,
    all_dimensions: dict[str, dict[str, Any]],
    milvus_client: MilvusClient,
    query_mode: Literal["publish", "edit"],
) -> list[dict[str, Any]]:
    """Find dimension values that match the question using semantic search."""

    dimension_codes = sorted(all_dimensions.keys())

    tasks = [
        find_matched_dimension_values(
            question=question,
            dimension_code=dimension_code,
            dimension_name=all_dimensions[dimension_code]["dimension_name"],
            milvus_client=milvus_client,
            query_mode=query_mode,
        )
        for dimension_code in dimension_codes
    ]

    start_time = time.perf_counter()
    retrieved_results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - start_time
    logger.debug(f"{MSG_HEADER} find matched dimension values time: {elapsed:.2f}s")

    matched_dimensions: list[dict[str, Any]] = [
        result
        for result in retrieved_results
        if result is not None and not isinstance(result, BaseException)
    ]

    return matched_dimensions


def _build_dimension_context(matched_dimensions: list[dict[str, Any]]) -> str:
    """Build a formatted string describing matched dimensions for LLM context."""
    if not matched_dimensions:
        return ""

    dimension_lines = []
    for dim in matched_dimensions:
        dim_values_str = "、".join(dim["dimension_values"])
        dimension_lines.append(f"{dim['dimension_name']}: {dim_values_str}")

    context = "\n    Matched dimensions in question:\n    " + "\n    ".join(
        dimension_lines
    )
    logger.debug(f"{MSG_HEADER} Dimension context: {context}")
    return context


def _build_dimension_instruction(matched_dimensions: list[dict[str, Any]]) -> str:
    """Build dimension handling instructions for sub-question generation."""
    if not matched_dimensions:
        return ""

    instructions = []
    for dim in matched_dimensions:
        for dim_value in dim["dimension_values"]:
            instructions.append(
                f"    - 如果问题中包含维度值【{dim_value}】"
                f"（对应维度：{dim['dimension_name']}），"
                f"请在子问题中添加【{dim_value}】，不要包含维度名称"
            )

    return (
        "\n\n    Dimension handling instructions:\n" + "\n".join(instructions)
        if instructions
        else ""
    )


async def _identify_metrics_with_llm(
    question: str,
    dimension_context: str,
    all_metrics: list[dict[str, Any]],
    llm: LLMEngine,
    split_question_examples: str | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> dict[str, Any]:
    """Use LLM to identify relevant metrics for the user's question."""
    metrics_list = [
        f"{m['metric_code']}: {m['metric_name']} - {m['metric_meaning']}"
        for m in all_metrics
    ]

    system_prompt = IDENTIFY_METRICS_SYSTEM_PROMPT.format(
        business_knowledge_context=BUSINESS_KNOWLEDGE_CONTEXT,
    )
    user_prompt = IDENTIFY_METRICS_USER_PROMPT.format(
        question=question,
        dimension_context=dimension_context,
        metrics_list=json.dumps(metrics_list, ensure_ascii=False, indent=2),
        split_question_examples=split_question_examples or "",
    )
    prompt = f"{system_prompt}\n{user_prompt}"

    logger.debug(f"{MSG_HEADER} Sending metrics identification prompt to LLM...")
    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        if usage_callback is not None:
            usage_callback(response.usage)
        content = response.content
        parsed_content = _parse_code_block(content)
        data = json.loads(parsed_content)
        result = IdentifyMetricsResponse.model_validate(data)
        logger.debug(f"{MSG_HEADER} LLM identified metrics: {result.metrics}")
        return {"metrics": result.metrics, "reasoning": result.reasoning}
    except Exception as e:
        logger.error(f"{MSG_HEADER} Failed to parse LLM response for identify metrics: {e}")
        return {"metrics": []}


async def _generate_sub_question_list(
    question: str,
    metric_info: dict[str, Any],
    dimension_context: str,
    dimension_instruction: str,
    llm: LLMEngine,
    split_question_instructions: str | None = None,
    split_question_examples: str | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[str]:
    """Generate a sub-question for a specific metric using LLM."""

    system_prompt = GENERATE_SUB_QUESTIONS_SYSTEM_PROMPT.format(
        current_date=datetime.now().strftime("%Y-%m-%d"),
        business_knowledge_context=BUSINESS_KNOWLEDGE_CONTEXT,
        split_question_instructions=split_question_instructions,
    )
    user_prompt = GENERATE_SUB_QUESTIONS_USER_PROMPT.format(
        question=question,
        metric_name=metric_info["metric_name"],
        metric_code=metric_info["metric_code"],
        metric_meaning=metric_info["metric_meaning"],
        dimension_list=metric_info["dimension_list"],
        dimension_context=dimension_context if dimension_context else "",
        dimension_instruction=dimension_instruction,
        split_question_examples=split_question_examples,
    )
    prompt = f"{system_prompt}\n{user_prompt}"


    try:
        messages: list[dict[str, str]] = []
        if isinstance(system_prompt, str) and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})

        if isinstance(user_prompt, str) and user_prompt.strip():
            rendered_user_prompt = user_prompt.replace("{query}", question).replace(
                "{current_time}", datetime.now().strftime("%Y-%m-%d")
            )
            prompt = (
                f"{rendered_user_prompt}\n\n"
                "以下是当前目标指标和拆解约束，请严格基于这些信息生成子问题。\n\n"
                f"{prompt}"
            )

        messages.append({"role": "user", "content": prompt})
        response = await llm.ainvoke(messages)
        if usage_callback is not None:
            usage_callback(response.usage)
        content = response.content
        parsed_content = _parse_code_block(content)
        data = json.loads(parsed_content)
        result = SubQuestionResponse.model_validate(data)
        return result.sub_questions
    except Exception as e:
        logger.error(
            f"{MSG_HEADER} Failed to generate sub-question for {metric_info['metric_code']}: {e}"
        )
        return [f"查询{metric_info['metric_name']}"]


async def split_question(
    query: str,
    query_mode: Literal["publish", "edit"],
    milvus_client: MilvusClient,
    llm: LLMEngine,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    usage_callback: Callable[[dict[str, int] | None], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Split a complex question into sub-questions, each for one metric.

    Args:
        query: User's natural language question
        milvus_client: Shared milvus client.
        llm: Shared LLMEngine.

    Returns:
        List of dictionaries containing following fields:
            - `metric_code`: metric code
            - `metric_name`: metric name
            - `sub_questions`: list of sub-questions
            - `original_question`: original question
    """
    split_question_instructions = system_prompt or SPLIT_QUESTION_INSTRUCTIONS
    split_question_examples = user_prompt or SPLIT_QUESTION_EXAMPLES

    all_metrics = await _get_all_metrics_info(
        milvus_client=milvus_client,
        query_mode=query_mode,
    )
    logger.debug(f"{MSG_HEADER} Found {len(all_metrics)} available metrics")

    if not all_metrics:
        logger.warning(f"{MSG_HEADER} No metrics found in database!")
        return []

    all_dimensions = await _get_all_dimensions_info(
        milvus_client=milvus_client,
        query_mode=query_mode,
    )

    matched_dimensions = await _get_matching_dimension_info(
        question=query,
        all_dimensions=all_dimensions,
        milvus_client=milvus_client,
        query_mode=query_mode,
    )
    logger.debug(f"{MSG_HEADER} Found {len(matched_dimensions)} matched dimensions")

    dimension_context = _build_dimension_context(matched_dimensions)
    dimension_instruction = _build_dimension_instruction(matched_dimensions)

    identified_metrics = await _identify_metrics_with_llm(
        question=query,
        dimension_context=dimension_context,
        all_metrics=all_metrics,
        llm=llm,
        usage_callback=usage_callback,
    )

    metric_codes = identified_metrics.get("metrics", [])
    logger.debug(f"{MSG_HEADER} Identified metrics: {metric_codes}")

    if not metric_codes:
        return []

    sub_questions: list[dict[str, Any]] = []
    valid_metric_infos = []

    for metric_code in metric_codes:
        metric_info = next(
            (m for m in all_metrics if m["metric_code"] == metric_code), None
        )
        if not metric_info:
            logger.warning(f"{MSG_HEADER} Could not find metric info for {metric_code}")
            continue
        dimension_list = []
        for dimension_code in metric_info["dimension_codes"]:
            if dimension_code in all_dimensions:
                dimension = all_dimensions[dimension_code]
                dimension_list.append({
                    "dimension_name": dimension["dimension_name"],
                    "dimension_values": dimension["dimension_values"]
                })
        metric_info["dimension_list"] = dimension_list
        valid_metric_infos.append(metric_info)

    logger.info(
        f"Generating sub-questions for {len(valid_metric_infos)} metrics in parallel"
    )

    tasks = [
        _generate_sub_question_list(
            question=query,
            metric_info=metric_info,
            dimension_context=dimension_context,
            dimension_instruction=dimension_instruction,
            llm=llm,
            split_question_instructions=split_question_instructions,
            split_question_examples=split_question_examples,
            usage_callback=usage_callback,
        )
        for metric_info in valid_metric_infos
    ]

    start_time = time.perf_counter()
    generated_sub_questions_list = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - start_time
    logger.info(
        f"Generated {len(generated_sub_questions_list)} sub-questions in {elapsed:.2f}s"
    )

    for metric_info, sub_question_list in zip(valid_metric_infos, generated_sub_questions_list):
        if isinstance(sub_question_list, BaseException):
            logger.error(
                f"{MSG_HEADER} Error generating sub-question for {metric_info['metric_code']}: {sub_question_list}"
            )
            continue

        sub_questions.append(
            {
                "metric_code": metric_info["metric_code"],
                "metric_name": metric_info["metric_name"],
                "sub_questions": sub_question_list,
                "original_question": query,
            }
        )

    return sub_questions
