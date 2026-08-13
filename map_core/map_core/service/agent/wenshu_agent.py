from __future__ import annotations

"""WenshuAgent.

tool_context 契约说明：

- 推荐放置路径：
  `request.extra.tool_context.<caller_agent_name>.wenshu_agent`
- 当前实现也兼容：
  `request.extra.tool_context.wenshu_agent`
- 合并优先级（低 -> 高）：
  1. `tool_context.wenshu_agent`
  2. `tool_context.<caller_agent_name>.wenshu_agent`

当前代码事实：

- `run()` 主执行路径会读取业务侧 `tool_context` 字段，用于解析问数查询所需鉴权/
  环境信息，以及可选的子问题拆解 prompt。
- 下方 `WenshuToolContext` 是当前主执行路径实际使用的上下文字段定义。

_parse_tool_context() 分支字段定义：

- `user_id` (`int`, 必填)：用户 ID。
- `agent_id` (`int`, 必填)：智能体 ID。
- `backend_env` (`str`, 必填)：查询模式（`RELEASE_STATE` 或 `EDITORIAL_STATE`），
  会自动映射为 `publish` 或 `edit`。
- `selected_data_model_ids` (`list[int]`, 必填)：选中的数据模型 ID 列表。
- `request_token` (`str`, 必填)：认证 token（映射为 `authentication_token`）。
- `disassembly_system_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 system prompt，会在末尾自动追加当前日期。
- `disassembly_user_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 user prompt，支持 `{query}` 占位符。
- `request_id` (`str | None`, 可选)：请求链路 ID；为空时回退到 request 对象上的
  `request_id`。
- `session_id` (`str | None`, 可选)：会话 ID。

约束：

- 必填字段（user_id, agent_id, backend_env, selected_data_model_ids, request_token）
  缺失或格式错误时会抛出 ValueError。
- backend_env 必须是 `RELEASE_STATE` 或 `EDITORIAL_STATE`，其他值会回退到 `publish`。
"""

import asyncio
import json
import os
import time
from typing import Any, Literal, cast

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...config import METRIC_MILVUS_URI, TEXT_TO_METRICS_API
from ...utils.global_context import agent_log_context
from ._wenshu_split_question import MILVUS_DB_NAME_PREFIX, split_question
from .base import AgentRequest, AgentResult
from .traceable_agent import TraceableAgent

MSG_HEADER = "[WenshuAgent]"

class WenshuQueryParams(BaseModel):
    query: str = Field(..., description="查询问题")


class WenshuToolContext(BaseModel):
    """Documented tool_context contract for the legacy payload branch."""

    session_id: str | None = Field(
        default=None,
        description="会话 ID",
    )

    request_id: str | None = Field(
        default=None,
        description="请求链路 ID；为空时回退到 request 对象上的 request_id。",
    )

    user_id: int = Field(description="用户 ID")

    user_name: str = Field(description="用户名")

    agent_id: int = Field(description="智能体 ID")

    query_mode: str = Field(
        alias="backend_env", description="查询模式（发布态、编辑态）"
    )

    staff_code: str = Field(description="用户工号")

    selected_data_model_ids: list[int] = Field(description="选中的数据模型 ID 列表")

    authentication_token: str = Field(
        alias="request_token",
        description="认证 token",
    )
    disassembly_system_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 system prompt，会在末尾自动追加当前日期。",
    )
    disassembly_user_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 user prompt，支持 {query} 占位符。",
    )

    model_config = ConfigDict(
        extra="allow",
        validate_by_name=True,
        validate_by_alias=True,
    )

    @model_validator(mode="after")
    def _update_query_mode(self) -> "WenshuToolContext":
        if self.query_mode in ("RELEASE_STATE", "publish"):
            self.query_mode = "publish"
        elif self.query_mode in ("EDITORIAL_STATE", "edit"):
            self.query_mode = "edit"
        else:
            warning_msg = f"未识别的查询模式：{self.query_mode}，默认使用发布态"
            logger.warning(f"{MSG_HEADER} {warning_msg}")
            self.query_mode = "publish"
        return self


# TODO: 换成 text-to-metrics 接口
# TEXT_TO_METRICS_API = "http://10.54.56.113:10006/text-to-metrics/query"
"""
method post
request_body:
{
  "query": "2026-02 supOS的合同额是多少",
  "user_id": 1,
  "agent_id": 12,
  "query_mode": "publish",
  "selected_data_model_ids": [
    1,
    2,
    3
  ]
}
response:
{
  "code": 200,
  "message": "成功",
  "results": [
    "data_model_description": "",
    "question": "",
    "metric_ql": "",
    "data": [],
    "error": null
  ]
}
"""


class WenshuAgent(TraceableAgent):
    name = "wenshu_agent"
    description = "查询公司经营相关指标数据"
    CONNECTOR_QUERY_PATH = "/msService/map-data-connector/algorithm/query"
    _api_url = "http://10.50.56.47:15173/metric-payload"
    timeout = 60.0

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": WenshuQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.name = "wenshu_agent"
        self.description = "查询公司经营相关指标数据"

    def _format_exception_detail(self, exc: Exception) -> str:
        detail = str(exc).strip()
        if not detail:
            detail = repr(exc)

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

    @staticmethod
    def _read_prompt(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else None
        return None

    @staticmethod
    def _append_current_date_hint(prompt: str | None) -> str | None:
        if not prompt:
            return prompt
        return f"{prompt}\n\n现在日期是 {time.strftime('%Y-%m-%d')}。"

    @classmethod
    def _build_environment_url(cls, endpoint: str) -> str:
        normalized_endpoint = endpoint.strip()
        if not normalized_endpoint:
            return "missing"

        url = httpx.URL(normalized_endpoint)
        normalized_path = url.path.rstrip("/")
        final_path = (
            normalized_path
            if normalized_path.endswith(cls.CONNECTOR_QUERY_PATH)
            else normalized_path + cls.CONNECTOR_QUERY_PATH
        )
        return str(url.copy_with(path=final_path))

    # def _merge_extra(self, request: AgentRequest) -> dict[str, Any]:
    #     extra = dict(request.extra or {})
    #     tool_context = extra.get("tool_context", {})
    #     caller_agent_name = extra.get("caller_agent_name")

    #     tool_name = self.name

    #     _context = tool_context.get(caller_agent_name, {}).get(tool_name, {})

    #     print(f"[WenshuAgent] tool_context: {tool_context}")
    #     print(f"[WenshuAgent] caller_agent_name: {caller_agent_name}")

    #     if isinstance(tool_context, dict):
    #         if isinstance(caller_agent_name, str) and caller_agent_name.strip():
    #             caller_context = tool_context.get(caller_agent_name)
    #             if isinstance(caller_context, dict):
    #                 nested_agent_context = caller_context.get(self.name)
    #                 if isinstance(nested_agent_context, dict):
    #                     for key, value in nested_agent_context.items():
    #                         extra.setdefault(key, value)

    #         agent_context = tool_context.get(self.name)
    #         if isinstance(agent_context, dict):
    #             for key, value in agent_context.items():
    #                 extra.setdefault(key, value)
    #     print(f"[WenshuAgent] extra: {extra}")
    #     return extra

    def _parse_tool_context(self, request: AgentRequest) -> WenshuToolContext:
        extra = dict(request.extra or {})
        request_id = extra.get("request_id", "UNKNOWN_REQUEST")
        session_id = extra.get("session_id", "UNKNOWN_SESSION")
        tool_context = extra.get("tool_context", {})
        caller_agent_name = extra.get("caller_agent_name")
        x_username = extra.get("x_username", "UNKNOWN_USER")
        tool_name = self.name

        _context: dict[str, Any] = {}
        if isinstance(tool_context, dict):
            top_level_context = tool_context.get(tool_name)
            if isinstance(top_level_context, dict):
                _context.update(top_level_context)

            if isinstance(caller_agent_name, str) and caller_agent_name.strip():
                caller_context = tool_context.get(caller_agent_name)
                if isinstance(caller_context, dict):
                    nested_tool_context = caller_context.get(tool_name)
                    if isinstance(nested_tool_context, dict):
                        _context.update(nested_tool_context)

        user_id = _context.get("user_id")
        agent_id = _context.get("agent_id")
        query_mode = (
            _context.get("query_mode")
            or _context.get("backend_env")
            or extra.get("query_mode")
            or extra.get("backend_env")
        )
        staff_code = request.staff_code  #! 直接从 request 里获得 staff_code
        selected_data_model_ids = _context.get("selected_data_model_ids")
        authentication_token = (
            _context.get("authentication_token")
            or _context.get("request_token")
            or extra.get("authentication_token")
            or extra.get("request_token")
        )

        error_msg = None
        if user_id is None:
            pass
        elif query_mode is None:
            error_msg = "缺少 query_mode"
        elif query_mode not in ["RELEASE_STATE", "EDITORIAL_STATE", "publish", "edit"]:
            error_msg = "query_mode 格式错误"
        elif selected_data_model_ids is None:
            error_msg = "缺少 selected_data_model_ids"
        elif not isinstance(selected_data_model_ids, list) or any(
            not isinstance(item, int) or item <= 0 for item in selected_data_model_ids
        ):
            error_msg = "selected_data_model_ids 格式错误"

        logger.debug(f"{MSG_HEADER} user_id: {user_id}")

        if error_msg:
            raise ValueError(error_msg)

        validated_user_id = cast(int, user_id)
        validated_agent_id = cast(int, agent_id)
        validated_query_mode = cast(str, query_mode)
        validated_selected_data_model_ids = cast(list[int], selected_data_model_ids)
        validated_authentication_token = cast(str, authentication_token)

        return WenshuToolContext(
            request_id=request_id,
            session_id=session_id,
            user_id=validated_user_id,
            user_name=x_username,
            agent_id=validated_agent_id,
            staff_code=staff_code,
            backend_env=validated_query_mode or "RELEASE_STATE",
            selected_data_model_ids=validated_selected_data_model_ids,
            request_token=validated_authentication_token or "MISSING_TOKEN",
            disassembly_system_prompt=_context.get("disassembly_system_prompt"),
            disassembly_user_prompt=_context.get("disassembly_user_prompt"),
        )

    @staticmethod
    def _extract_results_from_response(
        response_json: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(response_json, dict):
            return {
                "results": [],
                "error": "text-to-metrics 响应格式错误：非 JSON 对象",
            }

        if "code" in response_json:
            status_code = response_json.get("code")
            if status_code != 200:
                error = response_json.get("message")
                return {
                    "results": [],
                    "error": str(error or f"text-to-metrics code={status_code}"),
                }

        raw_results: list[dict] = response_json.get("results", [])
        # contains:
        #  - data_model_description
        #  - question
        #  - metric_ql
        #  - data
        #  - error
        if not isinstance(raw_results, list):
            return {
                "results": [],
                "error": "text-to-metrics 响应格式错误：results 不是 list",
            }

        normalized_results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue

            item_error = item.get("error")
            if item_error:
                warning_msg = f"text-to-metrics 子问题错误：{item_error}"
                logger.warning(f"{MSG_HEADER} {warning_msg}")
                normalized_results.append({"error": item_error})
                continue

            normalized_item = dict(item)
            normalized_results.append(normalized_item)

        return {
            "results": normalized_results,
        }

    async def _summarize_result(
        self,
        question: str,
        result: dict[str, list[dict[str, Any]]],
    ) -> str:
        # result_text = result.strip()
        # if not result_text:
        #     result_text = json.dumps(data, ensure_ascii=False)
        # if not result_text.strip():
        #     return ""
        prompt = """
            你是问数指标助手。请基于查询结果生成简明摘要，突出关键数据、时间范围与结论，并与用户问题相关。
            输出要求：
            1. 用简洁、专业的语言总结核心发现
            2. 突出关键数据、时间范围与结论
            3. 与用户问题相关，避免冗余
            4. 如果结果为空或无意义，明确说明
            5. 不要计算、推导或添加额外信息
            """
        user_content = (
            f"用户问题：{question}\n\n查询结果：{result}"
            "\n\n请用将数据整理为可读性强的摘要。请务必保持数据的真实全面。"
        )
        response = await self.llm.ainvoke(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ]
        )
        self._accumulate_usage(response.usage)
        return response.content.strip()

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"{MSG_HEADER} WenshuAgent run started, query={request.query}")

            try:
                _context: WenshuToolContext = self._parse_tool_context(request)
            except ValueError as e:
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    error=str(e),
                )
            _authentication_token = _context.authentication_token
            _environment_url = str(request.extra.get("backend_env_base_url", "missing"))
            _environment_url = _environment_url.strip().rstrip("/")

            # logger.debug(f"{MSG_HEADER} _authentication_token: {_authentication_token}")
            try:
                disassembly_system_prompt = self._append_current_date_hint(
                    self._read_prompt(_context.disassembly_system_prompt)
                )
                disassembly_user_prompt = self._read_prompt(
                    _context.disassembly_user_prompt
                )
                sub_questions = await self.disassemble_queries(
                    query=request.query,
                    query_mode=_context.query_mode,  # type: ignore
                    agent_id=str(_context.agent_id),
                    system_prompt=disassembly_system_prompt,
                    user_prompt=disassembly_user_prompt,
                )

                progress = {
                    "queried": 0,
                    "summarized": 0,
                    "total": len(sub_questions),
                }

                tasks = [
                    self._run_sub_question(
                        question=sub_question,
                        user_id=_context.user_id,
                        user_name=_context.user_name,
                        agent_id=_context.agent_id,
                        query_mode=_context.query_mode,
                        staff_code=_context.staff_code,
                        selected_data_model_ids=_context.selected_data_model_ids,
                        authentication_token=_authentication_token,
                        request_id=_context.request_id,
                        system_prompt="",
                        user_prompt="",
                        progress=progress,
                        environment_url=_environment_url,
                    )
                    for sub_question in sub_questions
                ]

                sub_question_result = await asyncio.gather(
                    *tasks, return_exceptions=True
                )

                error_list = []
                result_list = []
                data_sources = []
                for sub_question, item in zip(sub_questions, sub_question_result):
                    if isinstance(item, BaseException):
                        error_list.append(str(item))
                    else:
                        _error = item.get("error")
                        if _error:
                            error_list.append(_error)
                            continue
                        _summary = item.get("summary")
                        _data = item.get("data")
                        if _summary:
                            result_list.append(f"### {sub_question}\n{_summary}")
                        if _data:
                            data_sources.append(_data)

                if error_list and not result_list and not data_sources:
                    error_message = "\n".join(error_list)
                    logger.error(
                        f"{MSG_HEADER} WenshuAgent run failed, all sub questions failed, error={error_message}"
                    )
                    return AgentResult(
                        success=False,
                        name=self.name,
                        content="",
                        data_source={},
                        error=error_message,
                    )
                logger.info(f"{MSG_HEADER} WenshuAgent run completed successfully")
                return AgentResult(
                    name=self.name,
                    content="\n".join(result_list),
                    data_source={
                        "source": "wenshu_metrics",
                        "data": data_sources,
                    },
                    # extra_result={"payload_response": payload_response},
                )
            except Exception as exc:
                request_id = getattr(request, "request_id", "")
                logger.error(
                    f"{MSG_HEADER} WenshuAgent run failed, request_id={request_id}, error={exc}"
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )

    async def _run_sub_question(
        self,
        question: str,
        user_id: int,
        user_name: str,
        agent_id: int,
        query_mode: str,
        staff_code: str,
        selected_data_model_ids: list[int],
        authentication_token: str | None,  # deprecated
        request_id: str | None,
        system_prompt: str | None,
        user_prompt: str | None,
        progress: dict[str, int],
        *,
        environment_url: str = "missing",
        max_items: int = 6,
    ):
        """子问题查询指标接口并总结。"""
        headers = {
            "content-type": "application/json",
            "authentication": authentication_token or "DEPRECATED",
            "environment-url": environment_url,
        }
        if request_id:
            # headers["request-id"] = request_id
            # Change request-id to task-id
            headers["task-id"] = request_id
        try:
            # First stage is querying
            _query_start = time.perf_counter()
            async with httpx.AsyncClient(
                timeout=(self.timeout or 60) * 0.8,
                trust_env=False,
            ) as client:
                try:
                    text_to_metrics_response = await client.post(
                        url=TEXT_TO_METRICS_API,
                        json={
                            "query": question,
                            "user_id": user_id,
                            "user_name": user_name,
                            "agent_id": agent_id,
                            "query_mode": query_mode,
                            "staff_code": staff_code,
                            "selected_data_model_ids": selected_data_model_ids,
                        },
                        headers=headers,
                        timeout=30.0,
                    )
                    text_to_metrics_response.raise_for_status()
                except Exception as e:
                    logger.exception(
                        f"{MSG_HEADER} WenshuAgent _run_sub_question failed, error={self._format_exception_detail(e)}"
                    )
                    return {"error": self._format_exception_detail(e)}
            response_json: dict[str, Any] = text_to_metrics_response.json()
            _processed_result = self._extract_results_from_response(response_json)
            if _processed_result.get("error"):
                logger.error(
                    f"{MSG_HEADER} WenshuAgent _run_sub_question failed, error={_processed_result.get('error') or '<empty>'}"
                )
                return {"error": _processed_result.get("error") or "<empty>"}

            _results: list[dict[str, Any]] = _processed_result.get("results", [])
            data_to_summarize: dict[str, list[dict[str, Any]]] = {}
            for _result in _results:
                _question = _result.get("question", "")
                _data: list[dict[str, Any]] = _result.get("data", [])
                _error = _result.get("error")
                if _error:
                    data_to_summarize[_question] = [{"error": _error}]
                else:
                    data_to_summarize[_question] = _data

            _query_end = time.perf_counter()
            progress["queried"] += 1
            logger.debug(
                f"{MSG_HEADER} Query Progress: {progress['queried']}/{progress['total']}. "
                f"Elapsed: {_query_end - _query_start:.2f}s. "
                f"Sub-question: {question}"
            )
            # Second stage is summarizing
            _summarize_start = time.perf_counter()
            summary = await self._summarize_result(
                question=question,
                result=data_to_summarize,
            )
            _summarize_end = time.perf_counter()
            progress["summarized"] += 1
            logger.debug(
                f"{MSG_HEADER} Summarize Progress: {progress['summarized']}/{progress['total']}. "
                f"Elapsed: {_summarize_end - _summarize_start:.2f}s. "
                f"Sub-question: {question}"
            )
            return {
                "summary": summary,
                "data": _results,
            }
        except Exception as e:
            logger.exception(
                f"{MSG_HEADER} WenshuAgent _run_sub_question failed, error={self._format_exception_detail(e)}"
            )
            return {"error": self._format_exception_detail(e)}

    async def disassemble_queries(
        self,
        query: str,
        query_mode: Literal["publish", "edit"],
        agent_id: str,
        system_prompt: str | None,
        user_prompt: str | None,
        *,
        max_items: int = 6,
    ) -> list[str]:
        """问数问题拆解服务

        Args:
            query: 原始问题
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            max_items: 最大拆解数量

        Returns:
            拆解后的问题列表
        """
        from map_core.database.milvus import MilvusClient

        #! legacy business_domain will become agent_id

        # P0-SEC-01: no hardcoded credentials; unset password fails closed.
        milvus_client = MilvusClient(
            uri=METRIC_MILVUS_URI,
            user=os.getenv("MAP_MILVUS_USER", "root"),
            password=os.getenv("MAP_MILVUS_PASSWORD", ""),
            db_name=f"{MILVUS_DB_NAME_PREFIX}{agent_id}",
        )
        await milvus_client.connect()

        all_sub_questions = []
        try:
            splitted_questions = await split_question(
                query=query,
                query_mode=query_mode,
                # business_domain_id=agent_id,
                milvus_client=milvus_client,
                llm=self.llm,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                usage_callback=self._accumulate_usage,
            )
            for item in splitted_questions:
                all_sub_questions.extend(item["sub_questions"])
            return all_sub_questions
        except Exception as e:
            logger.exception(
                f"{MSG_HEADER} WenshuAgent disassemble_queries failed, error={self._format_exception_detail(e)}"
            )
            return [query]


if __name__ == "__main__":
    from ...config.common import DEEPSEEKV3_LOCAL_CONFIG
    from ...utils.llm_engine import LLMEngine

    test_questions = [
        "2025年MAP（Multi Agent Path）的销售收入?",
        "2025年MAP（Multi Agent Path）怎么样?",
    ]

    async def _demo():
        agent = WenshuAgent(llm=LLMEngine(DEEPSEEKV3_LOCAL_CONFIG))

        request = AgentRequest(
            query=test_questions[-1],
            staff_code="0120250028",
            summarize=True,
            extra={
                "userName": "liusongnan",
                "request_id": "6062589570674192",
                "data_origin": "supcon_metrics",
                "model_list": "",
                "permission_model_list": "",
                "business_domain": "6102261701897472",
                "ask_back_payload": False,
                "algorithmSpecMap": {
                    "queryResult": None,
                    "payload_list": None,
                    "query_parsing": None,
                    "modelInfos": None,
                },
            },
        )

        result = await agent.run(request)

        print("AgentResult:")
        print(f"  name: {result.name}")
        print(f"  success: {result.success}")
        print(f"  content: {result.content}")
        print(f"  error: {result.error}")
        print(f"  data_source: {result.data_source}")

    asyncio.run(_demo())
