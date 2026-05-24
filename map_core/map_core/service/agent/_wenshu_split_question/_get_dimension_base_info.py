"""获取维度基本信息"""

from typing import Literal

from map_core.database.milvus import MilvusClient

from ._schema import (
    DIMENSION_INFO_DRAFT_COLLECTION,
    DIMENSION_INFO_PUBLISHED_COLLECTION,
)


async def get_dimension_base_info(
    milvus_client: MilvusClient,
    query_mode: Literal["publish", "edit"],
):
    """获取维度基本信息"""
    dimension_collection_name = (
        DIMENSION_INFO_PUBLISHED_COLLECTION
        if query_mode == "publish"
        else DIMENSION_INFO_DRAFT_COLLECTION
    )

    _client = milvus_client._client
    if not _client:
        return None

    results = await _client.query(
        collection_name=dimension_collection_name,
        filter="",
        output_fields=["dimension_code", "dimension_name", "dimension_type"],
        limit=16384,
    )
    return {
        result["dimension_code"]: {
            "dimension_name": result["dimension_name"],
            "dimension_type": result["dimension_type"],
        } for result in results
    }
