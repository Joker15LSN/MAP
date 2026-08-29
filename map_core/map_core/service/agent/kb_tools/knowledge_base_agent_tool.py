from __future__ import annotations

"""MountedKBSearchAgent (search_mounted_kb_agent).

tool_context 契约说明：

- 推荐放置路径：
  `request.extra.tool_context.<caller_agent_name>.search_mounted_kb_agent`
- 当前实现也兼容：
  `request.extra.tool_context.search_mounted_kb_agent`

合并逻辑（由 `_fetch_agent_conf_from_request` 处理）：
  1. 优先从 `tool_context.<caller_agent_name>.<tool_name>` 获取配置
  2. 回退到 `tool_context.<tool_name>` 获取配置

字段定义：

- `kb_configs` (`list[dict]`, 必填)：
  知识库配置列表，每个元素包含：
  - `kb_code` (`str`): 知识库唯一标识
  - `kb_name` (`str`, 可选): 知识库显示名称
  - `embed_name` (`str`): 嵌入模型名称（如 "bge"）
  - `embed_url` (`str`): 嵌入服务接口地址
  - `embed_auth_token` (`str`): 嵌入服务认证token
- `rerank_model_config` (`dict | None`, 可选)：
  重排序模型配置，包含：
  - `rerank_model_url` (`str`): 重排序服务地址
  - `rerank_model_name` (`str`): 重排序模型名称（如 "jina-reranker-v2-base-multilingual"）
  - `rerank_auth_token` (`str`): 重排序服务认证token
- `disassembly_system_prompt` (`str | None`, 可选)：
  查询拆解阶段的 system prompt，支持 `{current_time}` 占位符。
- `disassembly_user_prompt` (`str | None`, 可选)：
  查询拆解阶段的 user prompt，支持 `{query}` 占位符。
- `summarize_prompt` (`str | None`, 可选)：
  多条检索结果汇总时的总结提示词，默认为 "请按结论、关键数据、补充说明三部分总结。"

约束：

- `kb_configs` 为必填字段，至少需要配置一个知识库。
- 其他字段缺失时使用内置默认值或从系统配置读取。
"""

import asyncio
import json
from datetime import datetime
from typing import Any, Optional, override

import httpx
from loguru import logger
from pydantic import BaseModel, Field, TypeAdapter

from ....utils.global_context import agent_log_context
from ....utils.model_invocation import ModelInvocationRequest
from ..base import AgentRequest, AgentResult
from ..traceable_agent import TraceableAgent
from .base import SearchKbChunkOutput, build_item_as_dict, fetch_tool_self_dict
from .knowledge_base_tools import (
    KbConfig,
    KBFilesConfigHandler,
    MountedKnowledgebasesConfig,
    SearchKBChunkInput,
    _fetch_conf_from_request,
    create_search_kb_chunk_tool,
    search_kb_core,
)

TOOL_NAME_SEARCH_MOUNTED_KB_AGENT = "search_mounted_kb_agent"


class MountedKBAgentParams(BaseModel):
    query: str = Field(..., description="检索问题")


class KBFilesAgentConfigHandler(KBFilesConfigHandler):
    """
    对于search agent有一些prompt的额外属性，所以继承以后额外配置方法
    """

    def __init__(self, related_kbs_config: MountedKnowledgebasesAgentConfig) -> None:
        super().__init__(related_kbs_config)
        self._disassembly_system_prompt = related_kbs_config.disassembly_system_prompt
        self._disassembly_user_prompt = related_kbs_config.disassembly_user_prompt
        self._summarize_prompt = (
            related_kbs_config.summarize_prompt
            if related_kbs_config.summarize_prompt
            else "请按结论、关键数据、补充说明三部分总结。"
        )

    def _get_disassembly_system_prompt(self) -> Optional[str]:
        return self._disassembly_system_prompt

    def _get_disassembly_user_prompt(self) -> Optional[str]:
        return self._disassembly_user_prompt

    def _get_summarize_prompt(self) -> str:
        return self._summarize_prompt


class MountedKnowledgebasesAgentConfig(MountedKnowledgebasesConfig):
    """
    对于search agent有一些prompt的额外属性，所以继承以后额外添加字段
    """

    disassembly_system_prompt: Optional[str] = None
    disassembly_user_prompt: Optional[str] = None
    summarize_prompt: Optional[str] = None

    @override
    def to_handler(self) -> KBFilesAgentConfigHandler:
        return KBFilesAgentConfigHandler(related_kbs_config=self)


def _fetch_agent_conf_from_request(agent_request: AgentRequest, tool_name: str) -> dict:
    # 获取self dict
    self_extra_dict = fetch_tool_self_dict(
        agent_request=agent_request, self_tool_name=tool_name
    )
    # 复用方法获取kb原生属性
    kb_tools_config = _fetch_conf_from_request(
        agent_request=agent_request,
        tool_name=tool_name,
    )
    kb_tools_config = {
        **kb_tools_config,  # 复用方法获取kb原生属性
        "disassembly_system_prompt": self_extra_dict.get(
            "disassembly_system_prompt", None
        ),
        "disassembly_user_prompt": self_extra_dict.get("disassembly_user_prompt", None),
        "summarize_prompt": self_extra_dict.get("summarize_prompt", None),
    }
    return kb_tools_config


class MountedKBSearchAgent(TraceableAgent):
    """
    agent as tool

    """

    name = TOOL_NAME_SEARCH_MOUNTED_KB_AGENT
    description = "聚合检索多个挂载的知识库"
    timeout = 60.0

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": MountedKBAgentParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.description = "聚合检索多个知识库"
        self._kb_search_tool = create_search_kb_chunk_tool()

    @staticmethod
    def _read_prompt(value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned
        return str(value)

    @staticmethod
    def _inject_current_time(prompt: str | None) -> str | None:
        if not prompt:
            return prompt
        return prompt.replace("{current_time}", datetime.now().strftime("%Y-%m-%d"))

    @staticmethod
    def _append_current_date_hint(prompt: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return f"{prompt}\n\n现在日期是 {today}。"

    @staticmethod
    def _apply_query_template(prompt: str, query: str) -> str:
        if "{query}" in prompt:
            return prompt.format(query=query)
        if prompt:
            return f"{prompt}\n\n用户问题：{query}"
        return f"用户问题：{query}"

    def _build_disassembly_prompts(
        self,
        main_query: str,
        kb_configs: list[KbConfig],
        config_handler: KBFilesAgentConfigHandler,
        max_sub_query: int,
    ) -> tuple[str, str]:
        kb_descriptions = []
        for idx, kb_config in enumerate(kb_configs):
            kb_name = kb_config.kb_name or kb_config.kb_code
            kb_descriptions.append(
                f"{idx + 1}. {kb_name} (ID: {kb_config.kb_code})"
            )
        kb_list_str = "\n".join(kb_descriptions)

        configured_system_prompt = self._inject_current_time(
            self._read_prompt(config_handler._get_disassembly_system_prompt())
        )
        configured_user_prompt = self._inject_current_time(
            self._read_prompt(config_handler._get_disassembly_user_prompt())
        )

        if configured_system_prompt:
            system_prompt = f"{configured_system_prompt}\n\n可用知识库列表：\n{kb_list_str}"
        else:
            system_prompt = "你是一个专业的查询分析助手，擅长查询拆解和知识库匹配。"

        if configured_user_prompt:
            user_prompt = self._apply_query_template(configured_user_prompt, main_query)
        else:
            user_prompt = f"""你是一个智能查询分析助手。你的任务是将用户的查询拆分为多个子查询，并为每个子查询选择合适的知识库(列表)进行检索。

可用知识库列表：
{kb_list_str}

请分析用户查询，并将其拆分为最多{max_sub_query}个子查询。对于每个子查询，指定相关的知识库ID（列表）。若不指定知识库ID则会在所有已配置知识库中检索。


注意：
1. 如果查询很简单，可以只返回1个子查询
2. 查询时可以指定子查询的返回检索结果条数限制，若不提供则使用默认数值。
3. 查询时可以根据需要指定知识库ID，识库ID必须从上面列表中选择。
4. 查询时可以根据需要指定时间范围参数。时间参数请注意时间格式。若指定了时间范围参数，则不要在query中体现。

用户查询：{main_query}
"""

        return system_prompt, user_prompt

    @staticmethod
    def _build_result_item(
        query: str,
        *,
        success: bool,
        result: str,
        payload: dict[str, Any],
        data: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "success": success,
            "result": result,
            "payload": payload,
            "data": data,
            "error": error,
        }

    @staticmethod
    def _resolve_request_id(request: AgentRequest) -> str:
        request_id = getattr(request, "request_id", "")
        if isinstance(request_id, str) and request_id.strip():
            return request_id

        extra = request.extra if isinstance(request.extra, dict) else {}
        extra_request_id = extra.get("request_id", "")
        return extra_request_id if isinstance(extra_request_id, str) else ""

    @staticmethod
    def _format_exception_detail(exc: Exception) -> str:
        detail = str(exc).strip() or repr(exc)

        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            status = response.status_code if response is not None else "unknown"
            body = response.text if response is not None else ""
            return f"{type(exc).__name__}: {detail}; status={status}; body={body}"

        if isinstance(exc, httpx.RequestError):
            request = exc.request
            method = request.method if request is not None else "unknown"
            url = str(request.url) if request is not None else "unknown"
            return f"{type(exc).__name__}: {detail}; request={method} {url}"

        return f"{type(exc).__name__}: {detail}"

    def _build_disassembly_json_schema(self, max_sub_query: int):
        adapter = TypeAdapter(list[SearchKBChunkInput])
        schema = adapter.json_schema()
        schema['minItems'] = 1
        schema['maxItems'] = max_sub_query
        return schema

    async def _handle_search_chunks(
        self,
        args: dict[str, Any],
        _request: AgentRequest,
        parid: str,
        config_handler: KBFilesAgentConfigHandler,
    ) -> SearchKbChunkOutput:
        """
        处理语义搜索知识库 chunk 请求

        Args:
            args: 包含 query 和可选的 limit、search_strategy 的参数字典
            _request: Agent 请求对象
            parid: 父进程 ID（用于日志）

        Returns:
            包含搜索结果的字典
        """
        res = await search_kb_core(
            args=args,
            kb_file_config_handler=config_handler,
            parid=parid,
            request_id=self._resolve_request_id(_request),
        )
        return res

    async def _summarize_multi_result(
        self,
        main_query: str,
        input_and_result_list: list[tuple[SearchKBChunkInput, SearchKbChunkOutput]],
        summarize_prompt: str,
    ) -> str:
        """
        对所有子查询的检索结果进行一次性总结

        Args:
            main_query: 用户的主查询问题
            input_and_result_list: (子查询输入, 检索结果) 的元组列表
            prompt: 可选的自定义总结提示词

        Returns:
            总结后的文本内容
        """
        # 收集所有检索结果
        all_results: list[str] = []

        for sub_query_input, result in input_and_result_list:
            if not result.success or not result.results:
                continue

            # 为每个子查询构建结果文本
            sub_query = sub_query_input.query
            result_items: list[str] = []

            for search_item in result.results:  # 限制每个子查询最多5条结果
                item = build_item_as_dict(item=search_item)
                chunk_content = item.get("chunk_content", "")
                file_name = item.get("file_name", "")
                sub_headers = item.get("sub_headers", "")

                item_text = f"文件：{file_name}"
                if sub_headers:
                    item_text += f" ({sub_headers})"
                item_text += f"\n内容：{chunk_content}"
                result_items.append(item_text)

            if result_items:
                all_results.append(
                    f"子问题：{sub_query}\n检索结果：\n" + "\n\n".join(result_items)
                )

        # 如果没有有效结果，返回空字符串
        if not all_results:
            return "未检索到任何相关内容。"

        # 构建总结提示
        summary_timeout = (self.timeout or 60) * 0.8
        summary_prompt = self._read_prompt(summarize_prompt)
        summary_prompt = self._append_current_date_hint(summary_prompt)

        # 构建用户输入内容
        user_content = (
            f"用户问题：{main_query}\n\n"
            f"检索结果：\n" + "\n\n".join(all_results) + "\n\n"
            "检索结果可能与问题相关/不相关，你需要自行判断其相关性。"
            "请基于以上信息，总结和用户问题相关的所有关键信息，包括但不限于关键发现/事实/定义/时间/数据等。"
        )

        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {"role": "system", "content": summary_prompt},
                    {"role": "user", "content": user_content},
                ],
                timeout=summary_timeout,
            )
        )
        outcome.raise_for_status()
        self._accumulate_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        return outcome.content.strip()

    async def split_queries_with_schemas(
        self,
        main_query: str,
        request: AgentRequest,
        kb_configs: list[KbConfig],
        config_handler: KBFilesAgentConfigHandler,
    ) -> list[SearchKBChunkInput]:
        max_sub_query = 5
        system_prompt, split_prompt = self._build_disassembly_prompts(
            main_query=main_query,
            kb_configs=kb_configs,
            config_handler=config_handler,
            max_sub_query=max_sub_query,
        )
        output_schema = self._build_disassembly_json_schema(max_sub_query)
        response = await self.llm.asimple_chat(
            prompt=split_prompt,
            system_prompt=system_prompt or "",
            json_schema=output_schema,
            schema_name='response_schema',
            schema_strict=True,
        )
        self._accumulate_usage(response.usage)
        content = response.content
        parts = json.loads(content)
        assert isinstance(parts, list)
        inputs = []
        for part in parts:
            try:
                _input = SearchKBChunkInput(**part)
                inputs.append(_input)
            except Exception:
                logger.warning(f'parsing args: {str(part)} fails')
                continue
        return inputs

    async def split_queries(
        self, main_query: str, request: AgentRequest, kb_configs: list[KbConfig]
    ) -> list[SearchKBChunkInput]:
        """
        使用LLM对主查询进行拆分，并为不同的知识库分配合适的查询

        该方法通过LLM分析用户问题，将其拆分为多个子问题，
        并为每个子问题指定合适的知识库进行检索。

        Args:
            main_query: 用户的原始查询问题
            request: Agent请求对象，包含知识库配置等信息

        Returns:
            SearchKBChunkInput列表，每个元素包含子查询和对应的知识库信息
        """
        try:
            # 多个知识库时，使用LLM进行查询拆分
            kb_descriptions = []
            for idx, kb_config in enumerate(kb_configs):
                kb_name = kb_config.kb_name or kb_config.kb_code
                kb_descriptions.append(
                    f"{idx + 1}. {kb_name} (ID: {kb_config.kb_code})"
                )

            kb_list_str = "\n".join(kb_descriptions)

            split_prompt = f"""你是一个智能查询分析助手。你的任务是将用户的查询拆分为多个子查询，并为每个子查询选择合适的知识库(列表)进行检索。

可用知识库列表：
{kb_list_str}

请分析用户查询，并将其拆分为最多5个子查询。对于每个子查询，指定相关的知识库ID（列表）。若不指定知识库ID则会在所有已配置知识库中检索。
limit 表示针对子查询等返回检索结果条数限制，若不提供则使用默认数值。

用户查询：{main_query}

请按照以下JSON格式返回结果：
{{
    "sub_queries": [
        {{
            "query": "子查询1",
            "kb_codes": ["知识库ID1"],
            "limit": 10
        }},
        {{
            "query": "子查询2",
            "kb_codes": ["知识库ID2", "知识库ID3"]
        }},
        {{
            "query": "子查询3",
            "kb_codes": [],
            "limit": 15
        }}
    ]
}}

注意：
1. 如果查询很简单，可以只返回1个子查询
2. 知识库ID必须从上面列表中选择
3. 返回纯JSON格式，不要包含其他说明文字
"""

            # print(split_prompt)
            outcome = await self.llm.invoke(
                ModelInvocationRequest(
                    messages=[
                        {
                            "role": "system",
                            "content": "你是一个专业的查询分析助手，擅长查询拆解和知识库匹配。",
                        },
                        {"role": "user", "content": split_prompt},
                    ],
                    timeout=30.0,
                )
            )
            outcome.raise_for_status()
            self._accumulate_usage(
                outcome.usage.to_dict() if outcome.usage else None
            )

            # 解析LLM返回的JSON结果
            result_text = outcome.content.strip()
            # 提取JSON部分（去除可能的markdown标记）
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            result = json.loads(result_text)
            sub_queries_data = result.get("sub_queries", [])

            if not sub_queries_data:
                # 如果拆分失败，使用原查询并搜索所有知识库
                all_kb_codes = [kb.kb_code for kb in kb_configs]
                return [
                    SearchKBChunkInput(
                        query=main_query, limit=10, kb_codes=all_kb_codes
                    )
                ]

            # 构建SearchKBChunkInput列表
            tool_inputs = []
            for sq in sub_queries_data:
                query_text = sq.get("query", main_query)
                kb_codes = sq.get("kb_codes", [])

                # 验证kb_codes是否有效
                valid_kb_codes = [
                    kb_code
                    for kb_code in kb_codes
                    if any(kb.kb_code == kb_code for kb in kb_configs)
                ]

                if not valid_kb_codes:
                    # 如果没有有效的kb_code，使用所有知识库
                    valid_kb_codes = [kb.kb_code for kb in kb_configs]

                tool_inputs.append(
                    SearchKBChunkInput(
                        query=query_text, limit=10, kb_codes=valid_kb_codes
                    )
                )

            logger.info(
                f"[TOOL] {self.name} split query '{main_query}' into "
                f"{len(tool_inputs)} sub-queries"
            )

            return tool_inputs

        except Exception as exc:
            logger.error(f"[TOOL] {self.name} query splitting failed: {exc}")
            # 出错时使用原查询搜索所有知识库
            all_kb_codes = [kb.kb_code for kb in kb_configs]
            return [
                SearchKBChunkInput(query=main_query, limit=10, kb_codes=all_kb_codes)
            ]

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"[TOOL] {self.name} run started, query={request.query}")
            try:
                kb_file_config = _fetch_agent_conf_from_request(
                    agent_request=request, tool_name=self.tool_name
                )
                kb_file_config_handler = MountedKnowledgebasesAgentConfig(
                    **kb_file_config
                ).to_handler()

                tool_inputs: list[SearchKBChunkInput] = await self.split_queries_with_schemas(
                    main_query=request.query,
                    request=request,
                    kb_configs=kb_file_config_handler._kb_configs,
                    config_handler=kb_file_config_handler,
                )

                # 并发调用kb搜索工具进行处理
                tasks = [
                    self._handle_search_chunks(
                        args=tool_input.model_dump(),
                        _request=request,
                        config_handler=kb_file_config_handler,
                        parid=self.agent_id,
                    )
                    for tool_input in tool_inputs
                ]

                # 并发执行，return_exceptions=True 保证单个失败不影响其他
                # gather 保证返回结果顺序与 tasks 列表顺序一致
                raw_results = await asyncio.gather(*tasks, return_exceptions=True)

                # 配对原始输入和结果，处理异常
                input_and_result_list: list[
                    tuple[SearchKBChunkInput, SearchKbChunkOutput]
                ] = []
                for tool_input, result in zip(tool_inputs, raw_results):
                    if isinstance(result, Exception):
                        # _handle_search_chunks底层实现应该已经把异常全部抓掉了，实际上不会走到这
                        logger.error(
                            f"[TOOL] search failed for query '{tool_input.query}': {result}"
                        )
                        input_and_result_list.append(
                            (
                                tool_input,
                                SearchKbChunkOutput(
                                    success=False,
                                    error=str(result),
                                    results=[],
                                    total_count=0,
                                ),
                            )
                        )
                    else:
                        assert isinstance(result, SearchKbChunkOutput)
                        input_and_result_list.append((tool_input, result))

                summarize_prompt = kb_file_config_handler._get_summarize_prompt()

                logger.debug(f"[TOOL] {self.name} summarizing result")
                content = await self._summarize_multi_result(
                    main_query=request.query,
                    input_and_result_list=input_and_result_list,
                    summarize_prompt=summarize_prompt,
                )

                error_messages = [
                    result.error.strip()
                    for _, result in input_and_result_list
                    if not result.success and result.error and result.error.strip()
                ]
                has_successful_result = any(
                    result.success for _, result in input_and_result_list
                )
                if not has_successful_result and error_messages:
                    error_detail = "\n".join(error_messages)
                    logger.warning(
                        f"[TOOL] {self.name} run completed with no successful kb result, "
                        f"errors={error_detail}"
                    )
                    return AgentResult(
                        success=False,
                        name=self.name,
                        content="",
                        data_source={},
                        meta_data={
                            "decomposed_queries": [i.query for i in tool_inputs],
                        },
                        error=error_detail,
                    )

                # 收集所有结果用于 data_source
                all_results = []
                for _, result in input_and_result_list:
                    if result.success:
                        all_results.extend(result.results)

                logger.info(f"[TOOL] {self.name} run completed successfully")
                return AgentResult(
                    success=True,
                    name=self.name,
                    content=content,
                    data_source={
                        "source": f"{self.tool_name}_source",
                        "data": all_results,
                    },
                    meta_data={
                        "decomposed_queries": [i.query for i in tool_inputs],
                        # "all_queries": all_queries,
                    },
                    error="",
                )
            except Exception as exc:
                request_id = self._resolve_request_id(request)
                error_detail = self._format_exception_detail(exc)
                logger.exception(
                    f"[TOOL] {self.name} run failed, "
                    f"request_id={request_id}, error={error_detail}"
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=error_detail,
                )
