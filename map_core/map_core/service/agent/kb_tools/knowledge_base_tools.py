from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from loguru import logger
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ...agent.base import AgentRequest, ToolResult
from ..tool_call_agent import Tool
from .base import (
    KB_API_BASE_URL,
    RerankModelConfig,
    SearchKbChunkOutput,
    fetch_tool_self_dict,
    to_tool_result,
)
from .remote_api import (
    EmbSchema,
    KbConfigSchema,
    search_knowledge,
)

TOOL_NAME_SEARCH_KB = "search_mount_kbs"


class KbConfig(BaseModel):
    embed_name: str = Field(..., description="embedding 模型名称")
    embed_url: str = Field(..., description="embedding 模型服务链接")
    embed_auth_token: str = Field(..., description="embedding 模型服务authtoken")

    kb_code: str = Field(..., description="知识库编号")
    kb_name: Optional[str] = Field(default=None, description="知识库名称")


class MountedKnowledgebasesConfig(BaseModel):
    """知识库配置"""

    # model_config = ConfigDict(extra="forbid")

    rerank_model_config: RerankModelConfig
    kb_configs: list[KbConfig]

    env: Optional[Any] = Field(default=None, description="环境配置， 保留字段吧")

    def to_handler(self) -> KBFilesConfigHandler:
        return KBFilesConfigHandler(related_kbs_config=self)


class KBFilesConfigHandler:
    def __init__(self, related_kbs_config: MountedKnowledgebasesConfig) -> None:
        self._rerank_model_config: RerankModelConfig = (
            related_kbs_config.rerank_model_config
        )
        self._kb_configs: list[KbConfig] = related_kbs_config.kb_configs

    def _get_rerank_model_config(self) -> RerankModelConfig:
        return self._rerank_model_config

    def _get_search_kbs_and_embs(
        self, kb_codes: Optional[list[str]]
    ) -> tuple[list[KbConfigSchema], list[EmbSchema]]:

        def _validate_kb_code(a_code: str):
            if kb_codes is None:
                return True
            return a_code in kb_codes

        # embed_confs: list[EmbSchema] = []
        kb_code_2_fil_ids_mapping = {}
        emb_name_2_emb_conf_mapping = {}

        for kb_conf in self._kb_configs:
            kb_code = kb_conf.kb_code
            if _validate_kb_code(kb_code) and kb_code not in kb_code_2_fil_ids_mapping:
                kb_code_2_fil_ids_mapping[kb_code] = KbConfigSchema(
                    kb_code=kb_code,
                    emb_model_name=kb_conf.embed_name,
                    filter_by_file_ids=None,
                )
                emb_name_2_emb_conf_mapping[kb_conf.embed_name] = EmbSchema(
                    model_name=kb_conf.embed_name,
                    model_url=kb_conf.embed_url,
                    auth_token=kb_conf.embed_auth_token,
                )
                # embed_confs.append(
                #     EmbSchema(
                #         model_name=kb_conf.embed_name,
                #         model_url=kb_conf.embed_url,
                #         auth_token=kb_conf.embed_auth_token,
                #     )
                # )
            else:
                logger.warning(f"kb code: {kb_code} deplicated!")
        return list(kb_code_2_fil_ids_mapping.values()), list(emb_name_2_emb_conf_mapping.values())


def _fetch_conf_from_request(agent_request: AgentRequest, tool_name: str) -> dict:
    self_extra_dict = fetch_tool_self_dict(
        agent_request=agent_request, self_tool_name=tool_name
    )
    rerank_config = self_extra_dict.get("rerank_model_config")
    if not rerank_config:
        # 若无配置则使用通用rerank
        rerank_config = agent_request.extra.get("rerank_model_config")
    kb_configs = self_extra_dict.get("kb_configs")

    kb_tools_config = {"kb_configs": kb_configs, "rerank_model_config": rerank_config}
    return kb_tools_config


class SearchTimeScopeInput(BaseModel):
    start_date: date = Field(
        ...,
        description="开始日期，必须是 %Y-%m-%d 格式",
        validation_alias=AliasChoices("start_date", "start_datetime"),
        serialization_alias="startDatetime",
    )
    end_date: date = Field(
        ...,
        description="结束日期，必须是 %Y-%m-%d 格式",
        validation_alias=AliasChoices("end_date", "end_datetime"),
        serialization_alias="endDatetime",
    )

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_to_date(cls, v: str) -> date:
        if isinstance(v, str):
            try:
                return date.fromisoformat(v)
            except ValueError:
                return datetime.fromisoformat(v).date()
        if isinstance(v, date):
            return v
        raise ValueError(f"无法解析日期: {v}")


class SearchKBChunkInput(BaseModel):
    """语义搜索知识库 chunk 输入参数"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="搜索查询文本，用于语义检索知识库内容")
    limit: int = Field(default=10, description="返回结果数量限制，默认为10")
    kb_codes: list[str] | None = Field(
        default=None, description="限制检索的kbcode列表。若不提供，则不做此限制。"
    )
    time_scope: SearchTimeScopeInput | None = Field(
        default=None, description="限制检索的时间跨度。若不提供，则不做此限制。"
    )

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


def _resolve_request_id(request: AgentRequest) -> str | None:
    request_id = getattr(request, "request_id", "")
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()

    extra_request_id = (request.extra or {}).get("request_id")
    if isinstance(extra_request_id, str) and extra_request_id.strip():
        return extra_request_id.strip()
    return None


async def search_kb_core(
    args: dict,
    kb_file_config_handler: KBFilesConfigHandler,
    parid: str,
    request_id: str | None = None,
) -> SearchKbChunkOutput:
    try:
        payload = SearchKBChunkInput.model_validate(args)
    except ValidationError as exc:
        return SearchKbChunkOutput(
            success=False, error=f"Invalid input parameters: {exc.errors()}"
        )

    try:
        logger.info(
            f"[{TOOL_NAME_SEARCH_KB}] Searching chunks with query={payload.query}, "
            f"limit={payload.limit}, parid={parid}"
        )

        # 构建知识库配置列表
        kb_configs, emb_configs = kb_file_config_handler._get_search_kbs_and_embs(
            kb_codes=payload.kb_codes
        )
        rerank_model_config = kb_file_config_handler._get_rerank_model_config()

        search_strategy: dict[str, Any] = {
            "rerank_param": {
                "rerank_model": rerank_model_config.rerank_model_name,
                "rerank_model_url": rerank_model_config.rerank_model_url,
                "rerank_auth_token": rerank_model_config.rerank_auth_token,
                "rerank_score_threshold": 0.1,
            },
        }
        if payload.time_scope:
            search_strategy["datetime_filter"] = {
                "datetime_range": payload.time_scope.model_dump(mode="json"),
                "nullable": True,
            }
        # 调用远程 API 进行语义搜索
        result = await search_knowledge(
            base_url=KB_API_BASE_URL,
            query=payload.query,
            kb_configs=kb_configs,
            emb_configs=emb_configs,
            limit=payload.limit,
            task_id=request_id, # 这里使用上游 request_id，在 API 层映射为 task_id
            search_strategy=search_strategy,
        )

        if not result.success or result.data is None:
            error_detail = result.message or 'search kb chunk fails.'
            return SearchKbChunkOutput(
                success=False,
                results=[],
                total_count=0,
                error=error_detail,
            )

        # 转换为字典列表
        # result_dicts = [build_item_as_dict(item=chunk) for chunk in results]
        results = result.data
        logger.info(
            f"[{TOOL_NAME_SEARCH_KB}] Successfully retrieved {len(results)} results "
            f"for query={payload.query}"
        )

        return SearchKbChunkOutput(
            success=True,
            results=results,
            total_count=len(results),
        )

    except Exception as exc:
        error_msg = f"Failed to search chunks with query={payload.query}: {exc}"
        logger.exception(f"[{TOOL_NAME_SEARCH_KB}] {error_msg}")
        return SearchKbChunkOutput(
            success=False,
            error=error_msg,
        )


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
    kb_file_config_dict = _fetch_conf_from_request(
        agent_request=_request, tool_name=TOOL_NAME_SEARCH_KB
    )
    kb_file_config_handler = MountedKnowledgebasesConfig(
        **kb_file_config_dict
    ).to_handler()

    res = await search_kb_core(
        args=args,
        kb_file_config_handler=kb_file_config_handler,
        parid=parid,
        request_id=_resolve_request_id(_request),
    )
    return to_tool_result(
        tool_name=TOOL_NAME_SEARCH_KB,
        search_chunk_output=res)


# ==================== Tool Factory ====================
def create_search_kb_chunk_tool() -> Tool:
    """
    创建语义搜索知识库 chunk 工具

    该工具用于基于查询文本进行语义检索，返回最相关的知识库 chunk。

    Returns:
        Tool: 语义搜索知识库 chunk 工具实例

    Raises:
        RuntimeError: 如果必要的环境变量未配置
    """

    return Tool(
        name=TOOL_NAME_SEARCH_KB,
        description=(
            "Search across configured knowledge bases using semantic search. "
            "This tool performs semantic retrieval on one or more pre-configured knowledge bases, "
            "returning the most relevant chunks ranked by similarity. "
            "Use this when you need to find information from organizational knowledge bases "
            "or indexed document collections that are available to the agent."
        ),
        parameters=SearchKBChunkInput.model_json_schema(),
        handler=_handle_search_chunks,
    )
