"""
将 final_results parse 成可以调用计算工具的格式

final_results: list[dict]

- question:
- metric_ql: dict, metric-ql format (拿到 metric_code, 再通过 milvus 获得 metric_name)
- data: list[dict], records='oriented', key 为 metric_name or dimension_name
- error: optional


从 milvus：
- 获得所有 dimension_type=="type/CreationTime" 的 dimension_code 和 dimension_name，得到 set of dimension_name。
  找到 data 中的 key which in this set，这个 key 作为 time（fallback：没有找到，则 fallback 使用 llm 根据问题和当前时间判断时间。）
- 获得所有 metric_code 和 metric_name，得到 set of metric_name。
  找到 data 中的 key which in this set，这个 key 作为 value

如果返回 data 里没有时间维度，则 fallback 使用 llm 根据问题和当前时间判断时间。

"""

from typing import Literal

from loguru import logger

from map_core.config import load_actual_config

load_actual_config()

from map_core.database.milvus import MilvusClient

from .._wenshu_split_question._schema import (
    DIMENSION_INFO_DRAFT_COLLECTION,
    DIMENSION_INFO_PUBLISHED_COLLECTION,
    METRIC_INFO_DRAFT_COLLECTION,
    METRIC_INFO_PUBLISHED_COLLECTION,
)


async def get_time_dimension_names(
    milvus_client: MilvusClient,
    query_mode: Literal["draft", "published"],
):
    collection_name = (
        DIMENSION_INFO_PUBLISHED_COLLECTION
        if query_mode == "published"
        else DIMENSION_INFO_DRAFT_COLLECTION
    )
    if not milvus_client._client:
        return set()
    results = await milvus_client._client.query(
        collection_name=collection_name,
        filter="dimension_type == 'type/CreationTime'",
        output_fields=["dimension_name", "dimension_code"],
    )
    dimension_names = set()
    for result in results:
        dimension_names.add(result["dimension_name"])
    return dimension_names


async def get_all_metric_info(
    milvus_client: MilvusClient,
    query_mode: Literal["draft", "published"],
):
    collection_name = (
        METRIC_INFO_PUBLISHED_COLLECTION
        if query_mode == "published"
        else METRIC_INFO_DRAFT_COLLECTION
    )
    if not milvus_client._client:
        return {}
    results = await milvus_client._client.query(
        collection_name=collection_name,
        filter="",
        output_fields=["metric_code", "metric_name_default"],
        limit=16384,
    )
    metric_info = {}
    for result in results:
        metric_info[result["metric_code"]] = result["metric_name_default"]
    return metric_info


async def parse_final_results(
    milvus_client: MilvusClient,
    final_results: list[dict],
    query_mode: Literal["draft", "published"],
):
    time_dimension_names = await get_time_dimension_names(milvus_client, query_mode)
    all_metric_info = await get_all_metric_info(milvus_client, query_mode)

    parsed_results = []
    for result in final_results:
        _results = []

        metric_ql = result.get("metric_ql", {})
        metric_codes = metric_ql.get("metrics", [])

        if not metric_codes:
            logger.warning(f"[ParseFinalResults] | {metric_ql}")
            continue

        metric_name = all_metric_info[metric_codes[0]]

        error = result.get("error")

        if error:
            logger.warning(f"[ParseFinalResults] | {error}")
            continue

        data = result.get("data", [])
        result_type = result.get("type")
        time = None
        value = None
        for _record in data:
            extra = {}
            for k, v in _record.items():
                if k in time_dimension_names:
                    time = v
                elif k == metric_name:
                    value = v
                else:
                    extra[k] = v

            _results.append(
                {
                    "time": time,
                    "value": value,
                    "extra": extra,
                    "type": result_type,
                }
            )
        parsed_results.extend(_results)

    return parsed_results
