"""
知识库查询工具模块
提供 query_kb_chunk 工具，用于根据 file_id/file_name 查询知识库 chunk
提供 search_kb_chunk 工具，用于基于语义搜索检索知识库 chunk
"""

from __future__ import annotations

from functools import partial
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ...agent.base import AgentRequest, ToolResult
from ..tool_call_agent import RuntimeSchemaTool, Tool
from .base import (
    KB_API_BASE_URL,
    RerankModelConfig,
    SearchKbChunkOutput,
    build_item_as_dict,
    to_tool_result,
)
from .remote_api import (
    EmbSchema,
    KbConfigSchema,
    ResultItem,
    query_chunks_by_file,
    search_knowledge,
)

TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK = "read_uploaded_file_chunk"
TOOL_NAME_SEARCH_UPLOADED_FILES = "search_uploaded_file"


MAX_CHUNK_NUM_OF_QUERY_CHUNK = 20
# ==================== 配置 ====================

# ==================== Input/Output Schema ====================


class QueryKbChunkInput(BaseModel):
    """查询知识库 chunk 输入参数"""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(..., description="知识库文件ID")
    file_name: str | None = Field(
        default=None, description="文件名称（可选，用于日志）"
    )
    start_chunk_index: int | None = Field(
        default=None,
        description="用于指定开始chunk的index，若不提供则默认返回前20个chunk。",
    )
    end_chunk_index: int | None = Field(
        default=None,
        description="用于指定结束chunk的index，若不提供则默认返回前20个chunk。",
    )

    @field_validator("file_id")
    @classmethod
    def _validate_file_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("`file_id` must be a non-empty string.")
        return v.strip()


class QueryKbChunkOutput(BaseModel):
    """查询知识库 chunk 输出结果"""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="查询是否成功")
    chunks: list[dict[str, Any]] = Field(
        default_factory=list, description="查询到的 chunk 列表"
    )
    total_count: int = Field(default=0, description="chunk 总数")
    error: str | None = Field(default=None, description="错误信息")


class WithEmbedConifg(BaseModel):
    # embed_id: str = Field(..., description="embedding 模型 ID")
    embed_name: str = Field(..., description="embedding 模型名称")
    embed_url: str = Field(..., description="embedding 模型服务链接")
    embed_auth_token: str = Field(..., description="embedding 模型服务authtoken")


class SpecifiedFile(WithEmbedConifg):
    file_id: str = Field(..., description="文件ID")
    file_name: str = Field(..., description="文件名称")
    kb_code: str = Field(..., description="知识库编号")


class UploadedFilesConfig(BaseModel):
    """知识库配置"""

    # model_config = ConfigDict(extra="forbid")

    specified_files: list[SpecifiedFile] | None = Field(
        default=None, description="指定的文件列表"
    )
    rerank_model_config: RerankModelConfig

    env: Optional[Any] = Field(default=None, description="环境配置， 保留字段吧")

    def to_handler(self) -> KBFilesConfigHandler:
        return KBFilesConfigHandler(upload_files_config=self)


class KBFilesConfigHandler:
    def __init__(self, upload_files_config: UploadedFilesConfig) -> None:
        self._specified_file_confs = (
            upload_files_config.specified_files
            if upload_files_config.specified_files
            else []
        )
        self._file_id_to_conf_cache = {
            file_conf.file_id: file_conf for file_conf in self._specified_file_confs
        }
        self._rerank_model_config: RerankModelConfig = (
            upload_files_config.rerank_model_config
        )

    def _get_file_conf_by_id(
        self, file_id: str, file_name: Optional[str]
    ) -> Optional[SpecifiedFile]:
        res = None
        for conf in self._specified_file_confs:
            if conf.file_id == file_id:
                res = conf
                break
            if file_name and conf.file_name == file_name:
                res = conf
                break
        return res

    def _get_search_kbs_and_emb_by_file_ids(
        self, file_ids: Optional[list[str]]
    ) -> tuple[list[KbConfigSchema], list[EmbSchema]]:
        if not file_ids:
            saerch_file_confs = list(self._file_id_to_conf_cache.values())
        else:
            saerch_file_confs = []
            for file_id in file_ids:
                if file_id in self._file_id_to_conf_cache:
                    saerch_file_confs.append(self._file_id_to_conf_cache[file_id])
        kb_code_2_fil_ids_mapping = {}
        embed_confs = []
        for saerch_file_conf in saerch_file_confs:
            kb_code = saerch_file_conf.kb_code
            if kb_code in kb_code_2_fil_ids_mapping:
                kb_code_2_fil_ids_mapping[kb_code].filter_by_file_ids.append(
                    saerch_file_conf.file_id
                )
            else:
                kb_code_2_fil_ids_mapping[kb_code] = KbConfigSchema(
                    kb_code=kb_code,
                    emb_model_name=saerch_file_conf.embed_name,
                    filter_by_file_ids=[saerch_file_conf.file_id],
                )
                embed_confs.append(
                    EmbSchema(
                        model_name=saerch_file_conf.embed_name,
                        model_url=saerch_file_conf.embed_url,
                        auth_token=saerch_file_conf.embed_auth_token,
                    )
                )
        return list(kb_code_2_fil_ids_mapping.values()), embed_confs

    def _get_rerank_model_config(self) -> RerankModelConfig:
        return self._rerank_model_config


async def query_chunks_by_file_with_index(
    base_url: str,
    kb_file_id: str,
    # embed_id: str,
    embed_name: str,
    embed_url: str,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
):
    chunks = await query_chunks_by_file(
        base_url=base_url,
        kb_file_id=kb_file_id,
        # embed_id=embed_id,
        embed_name=embed_name,
        embed_url=embed_url,
    )
    if start_index is not None and end_index is not None:
        res: list[ResultItem] = []
        for chunk in chunks:
            if (
                chunk.chunk_index is not None
                and end_index >= chunk.chunk_index >= start_index
            ):
                res.append(chunk)
    else:
        res: list[ResultItem] = list(chunks)
    res.sort(key=lambda x: x.chunk_index)
    res = res[:MAX_CHUNK_NUM_OF_QUERY_CHUNK]
    return res


def _get_uploade_files_config(_dict: dict) -> UploadedFilesConfig:
    """
    从字典中解析知识库配置

    Args:
        _dict: 配置字典，格式为：
        {
            "specified_files": [
                {
                    "file_id": "",
                    "file_name": "",
                    "kb_code": "",
                    "embed_name": "",
                    "embed_url": "",
                }
            ],
            "rerank_config": {

            }
        }

    Returns:
        KBFilesConfig: 知识库配置对象
    """
    return UploadedFilesConfig(**_dict)


def _fetch_conf_from_request(agent_request: AgentRequest) -> UploadedFilesConfig:
    if not agent_request.extra:
        raise ValueError("cannot fetch extra dict for tool.")
    # caller_agent_name: str = str(agent_request.extra.get('caller_agent_name'))
    # self_extra_dict = agent_request.extra.get(caller_agent_name)
    # assert isinstance(self_extra_dict, dict)
    # kb_tools_config = self_extra_dict.get('kb_tools')
    # assert isinstance(kb_tools_config, dict)
    # return _get_uploade_files_config(kb_tools_config)
    uploaded_kb_files = agent_request.extra.get("uploaded_kb_files")
    rerank_config = agent_request.extra.get("rerank_model_config")
    kb_tools_config = {
        "specified_files": uploaded_kb_files,
        "rerank_model_config": rerank_config,
    }
    return _get_uploade_files_config(kb_tools_config)


async def _handle_query_chunks(
    args: dict[str, Any], _request: AgentRequest, parid: str
) -> dict[str, Any]:
    """
    处理查询知识库 chunk 请求

    Args:
        args: 包含 file_id 和可选的 file_name 的参数字典
        _request: Agent 请求对象（未使用）
        parid: 父进程 ID（用于日志）

    Returns:
        包含查询结果的字典
    """
    kb_file_config = _fetch_conf_from_request(agent_request=_request)
    kb_file_config_handler = kb_file_config.to_handler()
    try:
        payload = QueryKbChunkInput.model_validate(args)
    except ValidationError as exc:
        return QueryKbChunkOutput(
            success=False, error=f"Invalid input parameters: {exc.errors()}"
        ).model_dump()

    target_file_id = payload.file_id
    target_file_name = payload.file_name
    target_config = kb_file_config_handler._get_file_conf_by_id(
        file_id=target_file_id, file_name=target_file_name
    )
    if not target_config:
        msg = f"cannot fetch [{TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK}] upload file config for file_id={payload.file_id}, file_name={payload.file_name}"

        logger.info(f"{msg}, parid={parid}")
        return QueryKbChunkOutput(
            success=False,
            error=msg,
        ).model_dump()
    try:
        logger.info(
            f"[{TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK}] Querying chunks for file_id={payload.file_id}, "
            f"file_name={payload.file_name}, parid={parid}"
        )

        # 调用远程 API 查询 chunks
        chunks = await query_chunks_by_file_with_index(
            base_url=KB_API_BASE_URL,
            kb_file_id=payload.file_id,
            # embed_id=target_config.embed_id,
            embed_name=target_config.embed_name,
            embed_url=target_config.embed_url,
            start_index=payload.start_chunk_index,
            end_index=payload.end_chunk_index,
        )

        # 转换为字典列表
        chunk_dicts = [build_item_as_dict(item=chunk) for chunk in chunks]

        logger.info(
            f"[{TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK}] Successfully retrieved {len(chunk_dicts)} chunks "
            f"for file_id={payload.file_id}"
        )

        return QueryKbChunkOutput(
            success=True,
            chunks=chunk_dicts,
            total_count=len(chunk_dicts),
        ).model_dump()

    except Exception as exc:
        error_msg = f"Failed to query chunks for file_id={payload.file_id}: {exc}"
        logger.exception(f"[{TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK}] {error_msg}")
        return QueryKbChunkOutput(
            success=False,
            error=error_msg,
        ).model_dump()


# ==================== Search KB Chunk Tool ====================


class SearchUploadedFileChunkInput(BaseModel):
    """语义搜索知识库 chunk 输入参数"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="搜索查询文本，用于语义检索知识库内容")
    limit: int = Field(default=10, description="返回结果数量限制，默认为10")
    file_ids: Optional[list[str]] = Field(
        ..., description="限制检索的文件id范围，若不指定，则检索当前会话已上传文件。"
    )
    # search_strategy: dict[str, Any] | None = Field(
    #     default=None,
    #     description=(
    #         "检索策略配置，可选参数。包含 hybrid_weights（混合检索权重）、"
    #         "datetime_filter（时间过滤）、rerank_param（重排序参数）等配置"
    #     )
    # )

    @field_validator("query")
    @classmethod
    def _validate_query(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("`query` must be a non-empty string.")
        return v.strip()

    @field_validator("limit")
    @classmethod
    def _validate_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("`limit` must be a positive integer.")
        if v > 100:
            raise ValueError("`limit` must not exceed 100.")
        return v


async def _handle_search_chunks(
    args: dict[str, Any], _request: AgentRequest, parid: str
) -> ToolResult:
    """
    处理语义搜索知识库 chunk 请求

    Args:
        args: 包含 query 和可选的 limit、search_strategy 的参数字典
        _request: Agent 请求对象
        parid: 父进程 ID（用于日志）

    Returns:
        包含搜索结果的字典
    """
    kb_file_config = _fetch_conf_from_request(agent_request=_request)
    kb_file_config_handler = kb_file_config.to_handler()
    to_tool_result_wrapper = partial(to_tool_result, tool_name=TOOL_NAME_SEARCH_UPLOADED_FILES)
    try:
        payload = SearchUploadedFileChunkInput.model_validate(args)
    except ValidationError as exc:
        return to_tool_result_wrapper(
                    search_chunk_output = SearchKbChunkOutput(
                        success=False,
                        error=f"Invalid input parameters: {exc.errors()}"
                    )
        )

    try:
        logger.info(
            f"[{TOOL_NAME_SEARCH_UPLOADED_FILES}] Searching chunks with query={payload.query}, "
            f"limit={payload.limit}, parid={parid}"
        )

        # 构建知识库配置列表
        kb_configs, emb_configs = (
            kb_file_config_handler._get_search_kbs_and_emb_by_file_ids(
                file_ids=payload.file_ids
            )
        )
        rerank_model_config = kb_file_config_handler._get_rerank_model_config()
        # 调用远程 API 进行语义搜索
        request_id = (_request.extra or {}).get("request_id")
        assert request_id
        result = await search_knowledge(
            base_url=KB_API_BASE_URL,
            query=payload.query,
            kb_configs=kb_configs,
            emb_configs=emb_configs,
            limit=payload.limit,
            task_id=request_id, # 这里使用上游 request_id，在 API 层映射为 task_id
            search_strategy={
                "rerank_param": {
                    "rerank_model": rerank_model_config.rerank_model_name,
                    "rerank_model_url": rerank_model_config.rerank_model_url,
                    "rerank_auth_token": rerank_model_config.rerank_auth_token,
                    "rerank_score_threshold": 0.1,
                }
            },
        )

        if not result.success or result.data is None:
            return to_tool_result(
                tool_name=TOOL_NAME_SEARCH_UPLOADED_FILES,
                search_chunk_output = SearchKbChunkOutput(
                success=False,
                results=[],
                total_count=0,
                error='search kb chunk fails.'
            ))

        # 转换为字典列表
        # result_dicts = [build_item_as_dict(item=chunk) for chunk in results]
        results = result.data
        logger.info(
            f"[{TOOL_NAME_SEARCH_UPLOADED_FILES}] Successfully retrieved {len(results)} results "
            f"for query={payload.query}"
        )

        return to_tool_result(
            tool_name=TOOL_NAME_SEARCH_UPLOADED_FILES,
            search_chunk_output = SearchKbChunkOutput(
            success=True,
            results=results,
            total_count=len(results),
        ))

    except Exception as exc:
        error_msg = f"Failed to search chunks with query={payload.query}: {exc}"
        logger.exception(f"[{TOOL_NAME_SEARCH_UPLOADED_FILES}] {error_msg}")
        return to_tool_result(
            tool_name=TOOL_NAME_SEARCH_UPLOADED_FILES,
            search_chunk_output = SearchKbChunkOutput(
                success=False,
                error=error_msg,
        ))


# ==================== Tool Factory ====================


class _GetUploadedChunkTool(RuntimeSchemaTool):
    def build_function_name(self, agent_request: None | AgentRequest) -> str:
        return self.name

    def build_function_description(self, agent_request: None | AgentRequest) -> str:
        if not agent_request:
            raise ValueError("agent request not provided!")
        kb_file_config = _fetch_conf_from_request(agent_request=agent_request)
        simple_tempalte = "file_id: {file_id}, file_name: {file_name}"
        if kb_file_config.specified_files:
            return (
                self.description
                + ' Available files:" \n'
                + "\n".join(
                    [
                        simple_tempalte.format(
                            **{
                                "file_id": file_conf.file_id,
                                "file_name": file_conf.file_name,
                            }
                        )
                        for file_conf in kb_file_config.specified_files
                    ]
                )
            )
        else:
            return self.description

    def build_function_parameters(
        self, agent_request: None | AgentRequest
    ) -> dict[str, Any]:
        return self.parameters


def create_query_kb_chunk_tool() -> Tool:
    """
    创建查询知识库 chunk 工具

    该工具用于根据给定的 file_id 查询知识库中的 chunk 内容。

    Returns:
        Tool: 查询知识库 chunk 工具实例

    Raises:
        RuntimeError: 如果必要的环境变量未配置
    """

    return _GetUploadedChunkTool(
        name=TOOL_NAME_QUERY_UPLOADED_FILE_CHUNK,
        description=(
            "Query knowledge base chunks by file ID (and start index and end index if provided)"
            f"This tool retrieves max {MAX_CHUNK_NUM_OF_QUERY_CHUNK} chunks ssociated with a specific knowledge base file. "
            "Useful for getting detailed content from a file that has been indexed in the knowledge base."
        ),
        parameters=QueryKbChunkInput.model_json_schema(),
        handler=_handle_query_chunks,
    )


def create_search_uploaded_file_chunk_tool() -> Tool:
    """
    创建语义搜索知识库 chunk 工具

    该工具用于基于查询文本进行语义检索，返回最相关的知识库 chunk。

    Returns:
        Tool: 语义搜索知识库 chunk 工具实例

    Raises:
        RuntimeError: 如果必要的环境变量未配置
    """

    return Tool(
        name=TOOL_NAME_SEARCH_UPLOADED_FILES,
        description=(
            "Search within user-uploaded files in the current conversation using semantic search. "
            "This tool performs semantic retrieval specifically on files uploaded by the user during the conversation, "
            "returning the most relevant chunks ranked by similarity. "
            "You can optionally specify specific file IDs to narrow the search scope. "
            "Use this when you need to find information from documents the user has provided in this conversation."
        ),
        parameters=SearchUploadedFileChunkInput.model_json_schema(),
        handler=_handle_search_chunks,
    )
