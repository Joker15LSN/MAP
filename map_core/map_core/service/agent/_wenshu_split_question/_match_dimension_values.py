from typing import Literal

from ._get_dimension_base_info import get_dimension_base_info

try:
    from rapidfuzz import fuzz  # type: ignore
except ImportError:
    from difflib import SequenceMatcher
    def fuzz_partial_ratio(s1: str, s2: str) -> float:
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio() * 100
    fuzz = type("fuzz", (object,), {"partial_ratio": fuzz_partial_ratio})

from map_core.database.milvus import MilvusClient
from map_core.utils.model_factory import aembed_text

from ._schema import DIMENSION_DETAIL_COLLECTION_PREFIX

MATCHED_DIMENSION_MINIMAL_SCORE = 0.6

async def find_matched_dimension_values(
    question: str,
    dimension_code: str,
    dimension_name: str,
    milvus_client: MilvusClient,
    query_mode: Literal["publish", "edit"],
) -> dict | None:
    """找到和问题匹配的维度值。"""

    question_embedding = await aembed_text(question)
    if not question_embedding:
        return None

    dimension_collection_name = f"{DIMENSION_DETAIL_COLLECTION_PREFIX}{dimension_code}"

    _aclient = milvus_client._client
    if not _aclient:
        return None

    has_col = await _aclient.has_collection(dimension_collection_name)
    if not has_col:
        return None

    dimension_base_info = await get_dimension_base_info(
        milvus_client=milvus_client,
        query_mode=query_mode,
    )

    if dimension_code not in dimension_base_info:
        warning_msg = f"维度代码 {dimension_code} 不存在于维度基本信息中"
        print(warning_msg)
        return None
    dimension_name = dimension_base_info[dimension_code]["dimension_name"]
    dimension_type = dimension_base_info[dimension_code]["dimension_type"]

    if dimension_type == "type/CreationTime":
        #TODO:
        return

    _dimension_value_list = await _aclient.search(
        collection_name=dimension_collection_name,
        data=[question_embedding],
        output_fields=["dimension_value"],
        search_params={"metric_type": "COSINE", "nprobe": 128},
        anns_field="dimension_value_embedding",
        limit=50,
    )

    if _dimension_value_list:
        dimension_value_list = _dimension_value_list[0]

    rated_dimension_value_list = []
    visited_dimension_values = set()

    for _dimension_value in dimension_value_list:
        similarity = _dimension_value["distance"]
        dimension_value: str = _dimension_value["entity"]["dimension_value"]
        if dimension_value in visited_dimension_values:
            continue
        visited_dimension_values.add(dimension_value)
        fuzz_score = fuzz.partial_ratio(question, dimension_value)
        weighted_score = 0.2 * fuzz_score / 100 + 0.8 * similarity
        rated_dimension_value_list.append((dimension_value, weighted_score, similarity))

    rated_dimension_value_list.sort(key=lambda x: (x[1], x[2]), reverse=True)

    # lgr.debug(f"Rated dimension value list: {rated_dimension_value_list}")

    filterd_dimension_value_list = []
    for dimension_value, score, similarity in rated_dimension_value_list:
        if score < MATCHED_DIMENSION_MINIMAL_SCORE:
            break
        if dimension_value.lower() not in question.lower():
            continue
        filterd_dimension_value_list.append(dimension_value)

    return {
        "dimension_code": dimension_code,
        "dimension_name": dimension_name,
        "dimension_values": filterd_dimension_value_list,
    } if filterd_dimension_value_list else None
