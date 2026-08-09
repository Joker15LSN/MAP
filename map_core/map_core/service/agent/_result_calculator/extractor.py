import json
import re
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field

from map_core.utils.llm_engine import LLMEngine

CalculationType = Literal[
    "sum",
    "avg",
    "yoy",
    "yoy_monthly",
    "yoy_quarterly",
    "yoy_daily",
    "mom",
    "qoq",
    "dod",
    "percentage",
    "unknown",
]

class LLMIntentExtraction(BaseModel):
    metric: str = Field(description="需要计算的指标名称，例如：'合同额'")
    base_calculation: Literal[
        "sum",
        "avg",
        "yoy",
        "mom",
        "percentage",
        "unknown",
    ] = Field(
        description="""基础计算大类：
- 求和 -> sum
- 平均值 -> avg
- 同比 -> yoy (无论是年同比、月同比、季同比还是日同比，只要是同比，基础大类都是 yoy)
- 环比 -> mom (无论是月环比、季环比还是日环比，只要是环比，基础大类都是 mom)
- 占比 -> percentage
"""
    )
    time_granularity: Literal["year", "quarter", "month", "day", "none"] = Field(
        description="""问题所针对的时间粒度：
- 如果问题中明确包含“日”或具体几号（如“15日”），为 day
- 如果问题中明确包含“季”（如“第一季度”、“二季度”），为 quarter
- 如果问题中明确包含“月”（如“1月”、“1月至3月”等，注意多个连续月份仍然是 month 粒度），为 month
- 如果问题中只包含“年”且无更细粒度（如“2026年”），为 year
- 如果没有明确时间，为 none
"""
    )
    has_multiple_types: bool = Field(
        description="如果用户在一个问题中请求了多种计算类型（例如既求同环比，又求占比），则设为 True。"
    )

class CalculationIntent(BaseModel):

    metric: str = Field(description="需要计算的指标名称")

    calculation_type: CalculationType = Field(description="最终的确切计算类型")

    has_multiple_types: bool = Field(description="是否包含多种计算类型")


def _parse_code_block(content: str) -> str:
    # 提取多行 markdown json block 中的内容
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


async def extract_intent(question: str, llm: LLMEngine) -> CalculationIntent:
    """
    根据问题提取计算意图
    """
    prompt = f"""--- Task ---
请分析以下问题，提取出需要计算的指标名称、基础计算大类(base_calculation)以及时间粒度(time_granularity)。

--- 问题 ---
{question}

Instructions：
请注意：如果问题中包含多种计算请求，请将 has_multiple_types 设为 true。

非常重要，关于 base_calculation（基础计算大类）的提取规则：
- 问题中包含“同比” -> 提取为 yoy
- 问题中包含“环比” -> 提取为 mom
- 问题中包含“求和”、“总和” -> 提取为 sum
- 问题中包含“平均” -> 提取为 avg
- 问题中包含“占比” -> 提取为 percentage

非常重要，关于 time_granularity（时间粒度）的提取规则：
- 问题中只包含“年”且无更细粒度（如“2026年”） -> 提取为 year
- 问题中明确包含“月”（如“1月”、“1月至3月”，“1月到3月”等） -> 提取为 month
- 问题中明确包含“日”或具体几号（如“15日”） -> 提取为 day
- 问题中明确包含“季”（如“第一季度
”） -> 提取为 quarter
- 如果没有明确时间 -> 提取为 none

NOTE: 只有问题中明确提到季度，比如说：第一季度、Q1这样的字眼，才判断时间粒度为 quarter

--- 问题 ---
{question}

请务必以 JSON 格式输出结果，严格遵循以下结构：
{{
    "metric": "需要计算的指标名称，例如：'合同额'",
    "base_calculation": "基础计算大类，可选值：sum, avg, yoy, mom, unknown",
    "time_granularity": "时间粒度，可选值：year, quarter, month, day, none",
    "has_multiple_types": false
}}
"""
    try:
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        content = response.content
        parsed_content = _parse_code_block(content)
        data = json.loads(parsed_content)
        ext = LLMIntentExtraction.model_validate(data)
    except Exception as e:
        logger.error(f"Failed to parse LLM response for intent extraction: {e}")
        return CalculationIntent(
            metric="",
            calculation_type="unknown",
            has_multiple_types=False
        )

    calc_type = ext.base_calculation


    # 根据基础计算类型和时间粒度推导最终类型
    if calc_type == "yoy":
        if ext.time_granularity == "month":
            calc_type = "yoy_monthly"
        elif ext.time_granularity == "quarter":
            calc_type = "yoy_quarterly"
        elif ext.time_granularity == "day":
            calc_type = "yoy_daily"
    elif calc_type == "mom":
        if ext.time_granularity == "quarter":
            calc_type = "qoq"
        elif ext.time_granularity == "day":
            calc_type = "dod"

    return CalculationIntent(
        metric=ext.metric,
        calculation_type=calc_type,
        has_multiple_types=ext.has_multiple_types
    )
