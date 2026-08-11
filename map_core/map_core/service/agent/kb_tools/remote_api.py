"""
知识库远程 API 调用方法
支持异步调用多知识库搜索、删除、更新和查询 chunk
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Literal, Optional, Sequence, overload

import httpx
from loguru import logger
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field

# ==================== Request Models ====================

class HybridWeightsSchema(BaseModel):
    """混合检索权重"""
    text_emb: float = Field(default=0, description="稠密向量权重", serialization_alias="textEmb")
    sparse_emb: float = Field(default=0, description="稀疏向量权重", serialization_alias="sparseEmb")


class DateRangeSchema(BaseModel):
    """日期范围"""
    start_date: date = Field(
        ...,
        description="开始日期，yyyy-mm-dd 格式",
        validation_alias=AliasChoices("start_date", "start_datetime"),
        serialization_alias="startDatetime",
    )
    end_date: date = Field(
        ...,
        description="结束日期，yyyy-mm-dd 格式",
        validation_alias=AliasChoices("end_date", "end_datetime"),
        serialization_alias="endDatetime",
    )


class DatetimeFilterSchema(BaseModel):
    """时间过滤"""
    date_range: DateRangeSchema = Field(
        ...,
        description="时间范围",
        validation_alias=AliasChoices("date_range", "datetime_range"),
        serialization_alias="datetimeRange",
    )
    nullable: bool = Field(default=True, description="是否允许无时间戳的实体")


class RerankParamSchema(BaseModel):
    """重排序参数"""
    rerank_score_threshold: float = Field(default=0.5, description="重排序分数阈值", serialization_alias="rerankScoreThreshold")
    rerank_model: str = Field(description="重排序模型", serialization_alias="rerankModel")
    rerank_model_url: str = Field( description="重排序模型服务url", serialization_alias="rerankModelUrl")
    rerank_auth_token: str = Field(description="重排序模型服务authtoken", serialization_alias="rerankAuthToken")

class SearchStrategySchema(BaseModel):
    """检索策略"""
    hybrid_weights: HybridWeightsSchema | None = Field(default=None, description="混合检索权重", serialization_alias="hybridWeights")
    datetime_filter: DatetimeFilterSchema | None = Field(default=None, description="时间过滤", serialization_alias="datetimeFilter")
    rerank_param: RerankParamSchema | None = Field(default=None, description="重排序参数", serialization_alias="rerankParam")


class EmbSchema(BaseModel):
    """Embedding 模型配置"""
    model_name: str = Field(..., description="embedding 模型名称", serialization_alias="modelName")
    model_url: str = Field(..., description="embedding 模型链接", serialization_alias="modelUrl")
    auth_token: str = Field(..., description="authToken", serialization_alias="authToken")

class KbConfigSchema(BaseModel):
    """知识库配置"""
    kb_code: str = Field(..., description="kbCode", serialization_alias="kbCode")
    emb_model_name: str = Field(..., description="embedding 模型名称", serialization_alias="embModelName")
    filter_by_file_ids: list[str] | None = Field(default=None, description="按文件 id 过滤", serialization_alias="filterByFileIds")

class SearchRequest(BaseModel):
    """语义检索请求"""
    # request_id: str = Field(..., description="request id", serialization_alias="requestId")
    query: str = Field(..., description="检索查询文本")
    search_kbs: list[KbConfigSchema] = Field(..., description="知识库配置列表", serialization_alias="searchKbs")
    emb_configs: list[EmbSchema] = Field(..., description="embedding 模型配置列表", serialization_alias="embConfigs")
    limit: int = Field(..., description="返回结果数量限制")
    search_strategy: SearchStrategySchema | None = Field(default=None, description="检索策略", serialization_alias="searchStrategy")

class QueryFilterItem(BaseModel):
    """查询条件项"""
    kb_file_id: str = Field(..., description="知识库文件 id", serialization_alias="kbFileId")
    # embed_id: str = Field(..., description="embedding 模型 id", serialization_alias="embedId")
    embed_name: str = Field(..., description="embedding 模型名称", serialization_alias="embedName")
    embed_url: str = Field(..., description="embedding 链接", serialization_alias="embedUrl")


class QueryChunkRequest(BaseModel):
    """查询 chunk 请求"""
    query_filter_list: list[QueryFilterItem] = Field(..., description="查询条件列表", serialization_alias="queryFilterList")


# ==================== Response Models ====================
class ResultItem(BaseModel):
    """单条结果"""
    # model_config = ConfigDict(extra="allow")
    id: int = Field(..., description="主键 ID")
    chunk_index: int = Field(..., description="chunk index", validation_alias=AliasChoices('chunk_id', 'chunkId'))
    header1: str | None = Field(default=None, description="一级标题")
    header2: str | None = Field(default=None, description="二级标题")
    header3: str | None = Field(default=None, description="三级标题")
    chunk_content: str = Field(default="", description="分块内容", validation_alias=AliasChoices('chunk_content', 'chunkContent'))
    knowledge_code: str = Field(default="", description="知识库编码", validation_alias=AliasChoices('knowledge_code', 'knowledgeCode'))
    kb_file_id: str | None = Field(default=None, description="知识库文件 ID", validation_alias=AliasChoices('kb_file_id', 'kbFileId'))
    title: str | None = Field(default=None, description="标题")
    chunk_tag_list: list[str] = Field(default_factory=list, description="分块标签列表", validation_alias=AliasChoices('chunk_tag_list', 'chunkTagList'))
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    file_timestamp: int | None = Field(default=None, description="文件时间戳", validation_alias=AliasChoices('file_timestamp', 'fileTimestamp'))
    backend_id: str | None = Field(default=None, description="baid", validation_alias=AliasChoices('backend_id', 'backendId', 'backEndId'))
    ba_id:str | None = Field(default=None, description="baid", validation_alias=AliasChoices('ba_id', 'baId'))

class SearchResultItem(ResultItem):
    """单条搜索结果"""
    reranker_score: float | None = Field(default=None, description="重排序分数")
    distance: float = Field(..., description="距离分数")


class SearchResponse(BaseModel):
    """语义检索响应"""
    model_config = ConfigDict(extra="allow")

    success: bool = Field(default=True, description="success")
    data: Optional[list[SearchResultItem]] = Field(default_factory=list, description="返回结果列表")
    message: str | None = Field(default=None, description="异常信息")

class QueryChunkResponse(BaseModel):
    code: int = Field(..., description="响应代码")
    data: list[QueryChunkData] = Field(..., description="返回结果列表")
    message: str | None = Field(default=None, description="异常信息")


class QueryChunkData(BaseModel):
    kb_file_id: str = Field(..., description="kb_file_id", serialization_alias='kbFileId', validation_alias='kbFileId')
    kb_file_chunks: list[ResultItem] = Field(..., description="kb_file_chunks", serialization_alias='kbFileChunks', validation_alias='kbFileChunks')

class BaseAPIResponse(BaseModel):
    """基础 API 响应"""
    model_config = ConfigDict(extra="allow")

    code: str = Field(..., description="响应代码")
    data: str | None = Field(default=None, description="响应数据")
    message: str | None = Field(default=None, description="响应消息")


# ==================== API Client ====================

class KnowledgeBaseAPI:
    """知识库远程 API 客户端"""

    def __init__(self, base_url: str, timeout: float = 30.0):
        """
        初始化 API 客户端

        Args:
            base_url: API 基础 URL (如: https://api.example.com)
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # def _get_headers(self, request_id: str | None = None) -> dict[str, str]:
    #     """获取请求头"""
    #     headers = {
    #         "Content-Type": "application/json",
    #         "X-Request-Id": uuid.uuid4().hex,
    #     }
    #     if isinstance(request_id, str) and request_id.strip():
    #         headers["X-Task-Id"] = request_id.strip()
    #     return headers

    async def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        extra_headers: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法 (GET, POST, etc.)
            path: API 路径
            data: 请求体数据

        Returns:
            响应 JSON 数据

        Raises:
            httpx.HTTPStatusError: HTTP 状态错误
            httpx.RequestError: 请求错误
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(**extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=data,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                body = exc.response.text if exc.response else ""
                logger.exception(
                    f"KnowledgeBaseAPI call failed with HTTP status {status}. Body: {body}"
                )
                raise
            except httpx.RequestError:
                logger.exception(f"KnowledgeBaseAPI call failed (request error): {url}")
                raise
        res_json = response.json()

        # print(json.dumps(res_json, ensure_ascii=False))
        return res_json

    async def multi_knowledge_search(
        self,
        query: str,
        kb_configs: Sequence[dict[str, Any] | KbConfigSchema],
        emb_configs: Sequence[dict[str, Any] | EmbSchema],
        limit: int,
        request_id: Optional[str],
        task_id: Optional[str],
        search_strategy: dict[str, Any] | None = None,

    ) -> SearchResponse:
        """
        多知识库搜索

        Args:
            request_id: 请求 ID
            query: 检索查询文本
            search_kbs: 知识库配置列表
                [{"kb_code": "kb_1", "emb_model_name": "bce", "filter_by_file_ids": ["file_id_1"]}]
            emb_configs: embedding 模型配置列表
                [{"model_name": "bce-embedding-base_v1", "model_url": "http://xxx"}]
            limit: 返回结果数量限制
            search_strategy: 检索策略（可选）

        Returns:
            SearchResponse: 搜索响应对象

        Example:
            >>> api = KnowledgeBaseAPI("https://api.example.com")
            >>> result = await api.multi_knowledge_search(
            ...     request_id="req_123",
            ...     query="如何使用 Python?",
            ...     search_kbs=[{
            ...         "kb_code": "kb_1",
            ...         "emb_model_name": "bce",
            ...         "filter_by_file_ids": ["file_id_1"]
            ...     }],
            ...     emb_configs=[{
            ...         "model_name": "bce-embedding-base_v1",
            ...         "model_url": "http://xxx"
            ...     }],
            ...     limit=10
            ... )
        """
        search_kb_schemas = [kb if isinstance(kb, KbConfigSchema) else KbConfigSchema(**kb) for kb in kb_configs]
        emb_schemas = [emb if isinstance(emb, EmbSchema) else EmbSchema(**emb) for emb in emb_configs]

        request_data = SearchRequest(
            # request_id=request_id,
            query=query,
            search_kbs=search_kb_schemas,
            emb_configs=emb_schemas,
            limit=limit,
            search_strategy=SearchStrategySchema(**search_strategy) if search_strategy else None,
        )
        extra_headers = {
            'X-Request-ID': request_id or uuid.uuid4().hex,
            'X-Task-ID': task_id
        }
        response_data = await self._request(
            method="POST",
            path="/multiKnowledgeSearch",
            data=request_data.model_dump(mode="json", exclude_none=True, by_alias=True),
            extra_headers=extra_headers,
        )
        try:
            search_response = SearchResponse(**response_data)
        except Exception as exc:
            logger.exception(
                "Failed to parse kb search response, "
                f"response_data={response_data}, error={exc}"
            )
            search_response = SearchResponse(
                success=False,
                data=[],
                message=f"calling kb fails: {exc}",
            )
        if not search_response.success:
            logger.warning(
                "search_response unsuccessful, "
                f"message={search_response.message}, response_data={response_data}"
            )
        return search_response


    async def query_chunk(
        self,
        query_filter_list: list[dict[str, Any]],
    ) -> QueryChunkResponse:
        """
        查询 chunk

        Args:
            query_filter_list: 查询条件列表
                [{
                    "kb_file_id": "file_id_1",
                    "embed_name": "bce",
                    "embed_url": "http://xxx"
                }]

        Returns:
            BaseAPIResponse: API 响应对象

        Example:
            >>> api = KnowledgeBaseAPI("https://api.example.com")
            >>> result = await api.query_chunk([
            ...     {
            ...         "kb_file_id": "file_id_1",
            ...         "embed_name": "bce",
            ...         "embed_url": "http://xxx"
            ...     }
            ... ])
        """
        request_data = QueryChunkRequest(
            query_filter_list=[QueryFilterItem(**item) for item in query_filter_list]
        )

        response_data = await self._request(
            method="POST",
            path="/queryChunk",
            data=request_data.model_dump(mode="json", exclude_none=True, by_alias=True),
        )

        return QueryChunkResponse(**response_data)


# ==================== Convenience Functions ====================

# @overload
# async def search_knowledge(
#     base_url: str,
#     query: str,
#     kb_configs: list[KbConfigSchema],
#     emb_configs: list[EmbSchema],
#     limit: int = 10,
#     request_id: str | None = None,
#     search_strategy: dict[str, Any] | None = None,
# ) -> list[SearchResultItem]: ...

# @overload
# async def search_knowledge(
#     base_url: str,
#     query: str,
#     kb_configs: list[dict[str, Any]],
#     emb_configs: list[dict[str, Any]],
#     limit: int = 10,
#     request_id: str | None = None,
#     search_strategy: dict[str, Any] | None = None,
# ) -> list[SearchResultItem]: ...

async def search_knowledge(
    base_url: str,
    query: str,
    kb_configs: Sequence[dict[str, Any] | KbConfigSchema],
    emb_configs: Sequence[dict[str, Any] | EmbSchema],
    limit: int = 10,
    request_id: str | None = None, # search_knowledge request id. 若外层不提供则内部api会自动生成
    task_id: str | None = None,  # upstream request id
    search_strategy: dict[str, Any] | None = None,
) -> SearchResponse:
    """
    便捷函数：搜索知识库

    Args:
        base_url: API 基础 URL
        query: 检索查询文本
        kb_configs: 知识库配置列表
        emb_configs: embedding 模型配置列表
        limit: 返回结果数量限制
        request_id: 请求 ID（可选，自动生成）
        search_strategy: 检索策略（可选）

    Returns:
        SearchResponse: 搜索响应对象
    """
    import uuid

    api = KnowledgeBaseAPI(base_url)
    res = await api.multi_knowledge_search(
        query=query,
        kb_configs=kb_configs,
        emb_configs=emb_configs,
        limit=limit,
        request_id=request_id,
        task_id=task_id,
        search_strategy=search_strategy,
    )
    # res = res.data
    return res


async def query_chunks_by_file(
    base_url: str,
    kb_file_id: str,
    # embed_id: str,
    embed_name: str,
    embed_url: str,
) -> list[ResultItem]:
    """
    便捷函数：根据知识库文件 ID 查询 chunk

    Args:
        base_url: API 基础 URL
        kb_file_ids: 知识库文件 ID 列表
        embed_id: embedding 模型 ID
        embed_name: embedding 模型名称
        embed_url: embedding 模型链接

    Returns:
        BaseAPIResponse: API 响应对象

    Example:
        >>> result = await query_chunks_by_file(
        ...     base_url="https://api.example.com",
        ...     kb_file_ids=["file_id_1", "file_id_2"],
        ...     embed_id="embed_id",
        ...     embed_name="bce",
        ...     embed_url="http://xxx"
        ... )
    """
    api = KnowledgeBaseAPI(base_url)
    query_filter_list = [
        {
            "kb_file_id": kb_file_id,
            # "embed_id": embed_id,
            "embed_name": embed_name,
            "embed_url": embed_url,
        }
    ]
    res = await api.query_chunk(query_filter_list=query_filter_list)
    res  = res.data[0].kb_file_chunks
    # res.sort(key=lambda x: x.chunk_index)
    assert isinstance(res, list)
    return res


if __name__ == '__main__':
    # http://10.48.2.29:1103
    import asyncio
    def _get_chunks():
        url = 'http://10.48.2.29:1103'
        asyncio.run(
            query_chunks_by_file(
                base_url=url,
                kb_file_id='5844701610000400',
                # embed_id ='bge',
                embed_name = 'bge',
                embed_url = 'http://10.50.56.243/v1/embeddings',
            )
        )

    # _get_chunks()

    def _search_chunks():
        url = 'http://10.48.2.29:1103'
        # url = 'http://localhost:1104'

        asyncio.run(
            search_knowledge(
                base_url=url,
                query='阻隔防爆技术',
                kb_configs=[
                    KbConfigSchema(
                        kb_code = 'knowledgeBase1758769747452',
                        emb_model_name = 'bge',
                        filter_by_file_ids = ['5844701610000400']
                    )
                ],
                emb_configs = [
                    EmbSchema(
                        model_name = 'bge',
                        model_url = 'http://10.50.56.243/v1/embeddings',
                        auth_token = "gpustack_67740332be54f86f_6711f81dbbcecdf9f85be842418e44d9"
                    )
                ],
                limit = 10,
                request_id = str(uuid.uuid4()),
                search_strategy = {
                    "rerank_param": {
                        "rerank_model": "jina-reranker-v2-base-multilingual",
                        "rerank_model_url": "http://10.50.56.243/v1/rerank",
                        "rerank_auth_token": "gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306",
                        "rerank_score_threshold": 0.3
                    }
                }
            )

        )
    _search_chunks()
