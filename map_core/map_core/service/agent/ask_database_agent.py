from __future__ import annotations

"""AskDatabaseAgent.

tool_context 契约说明：

- 支持路径：
  `request.extra.tool_context.ask_database_agent`
  `request.extra.tool_context.<caller_agent_name>.ask_database_agent`
- 合并优先级（低 -> 高）：
  1. `request.extra.tool_context.ask_database_agent`
  2. `request.extra.tool_context.<caller_agent_name>.ask_database_agent`
- 本 agent 不从 `request.extra` 顶层读取业务参数，避免 tool args 与运行时上下文混用。

字段定义：

- `api_url` (`str | None`, 可选)：text-to-sql 接口地址；为空时回退默认地址。
- `business_domain` (`int | str | None`, 必填)：业务域 ID。
- `userName` (`str | None`, 必填)：下游接口用户名。
"""

import asyncio
import json
import time
from typing import Any, TypedDict

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...config import TEXT_TO_SQL_API
from ...utils.global_context import agent_log_context
from ...utils.model_invocation import ModelInvocationRequest
from ._ask_database_atomize_request import (
    AtomizeContext,
    DecomposedTask,
    PrunedSchema,
    process_atomize_pipeline,
)
from .base import AgentRequest, AgentResult
from .traceable_agent import TraceableAgent


class AskDatabaseQueryParams(BaseModel):
    query: str = Field(..., description="数据库查询问题")

    sql: str | None = Field(
        default=None,
        description="如果用户意图为查看上一轮 sql 的后若干行，则需要传入上一次返回的 sql，否则不传",
    )

    data_model_id: str | None = Field(
        default=None,
        description="如果用户意图为查看上一轮 sql 的后若干行，则需要传入上一次 sql 对应的 data_model_id，否则不传",
    )


# TEXT_TO_SQL_BASE_URL_MAP = {
#     "dev": TEXT_TO_SQL_API,
# }
# TEXT_TO_SQL_QUERY_URL_PATH = "/text-to-sql/query"

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
"""


class AskDatabaseToolContext(BaseModel):
    session_id: str | None = Field(
        default=None,
        description="会话 ID",
    )

    request_id: str | None = Field(
        default=None,
        description="请求链路 ID；为空时回退到 request 对象上的 request_id。",
    )

    user_id: int = Field(description="用户 ID")

    agent_id: int = Field(description="智能体 ID")

    query_mode: str = Field(
        alias="backend_env", description="查询模式（发布态、编辑态）"
    )

    selected_data_model_ids: list[int] = Field(description="选中的数据模型 ID 列表")

    authentication_token: str = Field(
        alias="request_token",
        description="认证 token",
    )
    # 上一轮 sql
    sql: str | None = Field(
        default=None,
        description="上一轮 sql",
    )

    data_model_id: str | None = Field(
        default=None,
        description="上一次 sql 对应的 data_model_id",
    )

    # 配置系统提示词和用户提示词
    disassembly_system_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 system prompt",
    )
    disassembly_user_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 user prompt",
    )

    model_config = ConfigDict(
        extra="allow",
        validate_by_name=True,
        validate_by_alias=True,
    )

    @model_validator(mode="after")
    def _update_query_mode(self) -> "AskDatabaseToolContext":
        if self.query_mode == "RELEASE_STATE":
            self.query_mode = "publish"
        elif self.query_mode == "EDITORIAL_STATE":
            self.query_mode = "edit"
        else:
            warning_msg = f"未识别的查询模式：{self.query_mode}，默认使用发布态"
            logger.warning(warning_msg)
            self.query_mode = "publish"
        return self


class ExecSqlAndSummarizeSuccess(TypedDict):
    summary: str
    data: list[dict[str, Any]]


class ExecSqlAndSummarizeError(TypedDict):
    error: str


ExecSqlAndSummarizeResult = ExecSqlAndSummarizeSuccess | ExecSqlAndSummarizeError


class AskDatabaseAgent(TraceableAgent):
    name = "ask_database_agent"
    description = "调用 text-to-sql 数据库查询接口，并基于结果生成摘要"
    _api_url = TEXT_TO_SQL_API
    timeout = 60.0

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": AskDatabaseQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, name=self.tool_name, **kwargs)

    def _format_exception_detail(self, exc: Exception) -> str:
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

    @classmethod
    def _resolve_api_url(cls, value: Any) -> str:
        if isinstance(value, str):
            api_url = value.strip().rstrip("/")
            if api_url:
                return api_url
        return cls._api_url

    def _resolve_nested_tool_context(self, request: AgentRequest) -> dict[str, Any]:
        extra = dict(request.extra or {})
        tool_context = extra.get("tool_context")
        caller_agent_name = extra.get("caller_agent_name")
        merged: dict[str, Any] = {}

        if not isinstance(tool_context, dict):
            return merged

        top_level_context = tool_context.get(self.name)
        if isinstance(top_level_context, dict):
            merged.update(top_level_context)

        if isinstance(caller_agent_name, str) and caller_agent_name.strip():
            caller_context = tool_context.get(caller_agent_name)
            if isinstance(caller_context, dict):
                nested_agent_context = caller_context.get(self.name)
                if isinstance(nested_agent_context, dict):
                    merged.update(nested_agent_context)

        return merged

    def _parse_tool_context(self, request: AgentRequest) -> AskDatabaseToolContext:
        extra = dict(request.extra or {})
        request_id = extra.get("request_id", "UNKNOWN_REQUEST")
        session_id = extra.get("session_id", "UNKNOWN_SESSION")
        resolved_context = self._resolve_nested_tool_context(request)

        sql = extra.get("sql")
        data_model_id = extra.get("data_model_id")
        if sql and data_model_id:
            debug_msg = "当前问表意图是查看上一轮 sql 结果的后若干行"
            logger.debug(debug_msg)
        elif sql and not data_model_id:
            warning_msg = "上一次查询有sql参数缺失"
            logger.warning(warning_msg)
        elif data_model_id and not sql:
            warning_msg = "上一次查询有data_model_id参数缺失"
            logger.warning(warning_msg)

        user_id = resolved_context.get("user_id")
        agent_id = resolved_context.get("agent_id")
        query_mode = resolved_context.get("backend_env") or extra.get("backend_env")
        selected_data_model_ids = resolved_context.get("selected_data_model_ids")
        authentication_token = resolved_context.get("request_token") or extra.get(
            "request_token"
        )
        disassembly_system_prompt = resolved_context.get("disassembly_system_prompt")
        disassembly_user_prompt = resolved_context.get("disassembly_user_prompt")

        error_msg = None
        if user_id is None:
            error_msg = "缺少 user_id"
        elif not isinstance(user_id, int) or user_id <= 0:
            error_msg = "user_id 格式错误"
        elif agent_id is None:
            error_msg = "缺少 agent_id"
        elif not isinstance(agent_id, int) or agent_id <= 0:
            error_msg = "agent_id 格式错误"
        elif query_mode is None:
            error_msg = "缺少 query_mode"
        elif query_mode not in ["RELEASE_STATE", "EDITORIAL_STATE"]:
            error_msg = "query_mode 格式错误"
        elif selected_data_model_ids is None:
            error_msg = "缺少 selected_data_model_ids"
        elif not isinstance(selected_data_model_ids, list) or any(
            not isinstance(item, int) or item <= 0 for item in selected_data_model_ids
        ):
            error_msg = "selected_data_model_ids 格式错误"
        elif authentication_token is None:
            authentication_token = "MISSING"

        logger.debug(f"[AskDatabaseAgent] user_id: {user_id}")

        if error_msg:
            raise ValueError(error_msg)

        return AskDatabaseToolContext(
            session_id=session_id,
            request_id=request_id,
            user_id=user_id or -1,
            agent_id=agent_id or -1,
            backend_env=query_mode or "RELEASE_STATE",
            selected_data_model_ids=selected_data_model_ids or [],
            request_token=authentication_token or "MISSING_TOKEN",
            sql=sql,
            data_model_id=data_model_id,
            disassembly_system_prompt=disassembly_system_prompt,
            disassembly_user_prompt=disassembly_user_prompt,
        )

    async def _fetch_query_result(
        self,
        *,
        api_url: str,
        headers: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=(self.timeout or 60) * 0.8,
            trust_env=False,
        ) as client:
            try:
                response = await client.post(
                    api_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                body = exc.response.text if exc.response else ""
                logger.exception(
                    "ask_database_agent API call failed with HTTP status "
                    f"{status}. Body: {body}"
                )
                raise
            except httpx.RequestError:
                logger.exception("ask_database_agent API call failed (request error)")
                raise

        return response.json()

    async def _summarize_result(
        self,
        question: str,
        result_list: list[dict[str, Any]],
    ) -> str:
        text_to_sql_details = []
        truncated = False
        for i, result in enumerate(result_list, start=1):
            sub_question = result.get("question")
            table_schema = result.get("table_schema")
            #! if not generate_sql or len(generate_sql) > 100: ignore sql
            generated_sql = result.get("sql")
            if generated_sql and len(generated_sql) > 100:
                generated_sql = None
            executed_result = result.get("data")
            truncated = truncated or result.get("truncated")
            error = result.get("error")
            detail = f"子问题: {sub_question}\n"
            if generated_sql:
                detail += f"生成的SQL: {generated_sql}\n"
            if table_schema:
                detail += f"表结构: {table_schema}\n"
            if executed_result:
                detail += f"执行结果: {json.dumps(executed_result)}\n"
            if error:
                detail += f"错误信息: {error}\n"
            text_to_sql_details.append(detail)
        text_to_sql_details_str = "\n".join(text_to_sql_details)

        # TODO: 注意 truncated 字段，表示因为数据量过大而做了截断，可以在返回的 summary 最后拼接“因返回数据量过大，超过模型上下文处理能力，部分数据已做截断，必须说明。”
        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是数据库查询助手。请基于查询返回结果生成简洁、准确的中文总结，"
                            "明确回答用户问题，并保留关键字段、统计值和限制条件。"
                            "不要只复述接口成功状态，优先解读实际查询结果。"
                            "严格禁止编造信息或数据。如果查询结果为空，则明确说明未查询到相关数据！"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"用户问题：{question}\n\n"
                            f"{text_to_sql_details_str}\n\n"
                            "请输出可直接给业务方查看的总结，避免臆测；若结果不足以回答，明确说明。"
                        ),
                    },
                ]
            )
        )
        outcome.raise_for_status()
        self._accumulate_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        summary = outcome.content.strip()
        if truncated:
            summary += "\n\nWARNING: **因返回数据量过大，超过模型上下文处理能力，部分数据已做截断，必须说明。**"
        return summary

    def _extract_result_list(
        self,
        text_to_sql_response: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_results = text_to_sql_response.get("results", [])
        if not isinstance(raw_results, list):
            return []

        normalized_results: list[dict[str, Any]] = []
        for item in raw_results:
            if isinstance(item, dict):
                normalized_results.append(
                    json.loads(json.dumps(item, ensure_ascii=False))
                )
            else:
                normalized_results.append({"value": item})
        return normalized_results

    def _build_original_query_fallback_tasks(
        self,
        context: AtomizeContext,
    ) -> list[DecomposedTask]:
        question = context.question.strip()
        if not question:
            return []

        tasks: list[DecomposedTask] = []
        for model_id in context.selected_data_model_ids:
            tasks.append(
                DecomposedTask(
                    sub_question=question,
                    pruned_schema=PrunedSchema(
                        model_id=str(model_id),
                        data_source_id="",
                        data_model_name="",
                        data_model_description="",
                        wide_table_sql="",
                        relevant_columns=[],
                    ),
                )
            )
        return tasks

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"[TOOL] AskDatabaseAgent run started, query={request.query}")
            try:
                _context: AskDatabaseToolContext = self._parse_tool_context(request)
            except ValueError as e:
                logger.error(f"[TOOL] AskDatabaseAgent parse tool context failed: {e}")
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    error=str(e),
                )

            text_to_sql_url = TEXT_TO_SQL_API

            _authentication_token = _context.authentication_token
            _environment_url = self._resolve_api_url(
                str(request.extra.get("backend_env_base_url", "missing"))
            )
            _task_id = _context.request_id

            headers = {
                "content-type": "application/json",
                "authentication": _authentication_token,
                "environment-url": _environment_url,
                "task-id": _task_id,
            }

            previous_sql = _context.sql
            previous_data_model_id = _context.data_model_id

            if previous_sql and previous_data_model_id:
                try:
                    _exec_sql_and_summarize_result = await self._exec_sql_and_summarize(
                        question=request.query,
                        sql=previous_sql,
                        context=_context,
                        environment_url=_environment_url,
                    )
                    if "error" in _exec_sql_and_summarize_result:
                        return AgentResult(
                            success=False,
                            name=self.name,
                            content="",
                            error=_exec_sql_and_summarize_result["error"],  # type: ignore
                        )

                    summary = _exec_sql_and_summarize_result["summary"]
                    result_list = _exec_sql_and_summarize_result["data"]

                    return AgentResult(
                        name=self.name,
                        content=summary,
                        data_source={
                            "source": "ask_database_api",
                            "api_url": text_to_sql_url,
                            "result_count": 1,
                            "data": result_list,  # list of one dict
                        },
                    )

                except Exception as e:
                    error_detail = self._format_exception_detail(e)
                    return AgentResult(
                        success=False,
                        name=self.name,
                        content="",
                        data_source={},
                        error=error_detail,
                    )

            # logger.debug(f"_authentication_token: {_authentication_token}")

            # debug_msg = f"问表用户提示词: {_context.disassembly_user_prompt}"
            # logger.debug(debug_msg)

            atomize_context = AtomizeContext(
                request_id=_context.request_id,
                user_id=_context.user_id,
                agent_id=_context.agent_id,
                query_mode=_context.query_mode,
                environment_url=_environment_url,
                authorization_token=_authentication_token,
                system_prompt=_context.disassembly_system_prompt,
                user_prompt=_context.disassembly_user_prompt,
                question=request.query,
                selected_data_model_ids=_context.selected_data_model_ids,
            )

            try:
                decomposed_tasks = await process_atomize_pipeline(
                    context=atomize_context,
                    llm=self.llm,
                    usage_callback=self._accumulate_usage,
                )

                if not decomposed_tasks:
                    logger.warning(
                        "[TOOL] AskDatabaseAgent found no sub-questions from atomize pipeline"
                    )
                    decomposed_tasks = self._build_original_query_fallback_tasks(
                        atomize_context
                    )
                    if not decomposed_tasks:
                        return AgentResult(
                            name=self.name,
                            content=f"未能分解出有效的查询子问题，原问题：{request.query}",
                            data_source={},
                        )

                progress = {
                    "queried": 0,
                    "summarized": 0,
                    "total": len(decomposed_tasks),
                }

                async def _ask_text_to_sql(task: DecomposedTask) -> dict[str, Any]:
                    try:
                        data_model_id = int(task.pruned_schema.model_id)
                    except (ValueError, TypeError):
                        logger.error(
                            f"Failed to cast model_id '{task.pruned_schema.model_id}' to int"
                        )
                        data_model_id = -1

                    request_body = {
                        "query": task.sub_question,
                        "user_id": _context.user_id,
                        "agent_id": _context.agent_id,
                        "data_model_id": data_model_id,
                        "query_mode": _context.query_mode,
                        "evidence": _context.disassembly_user_prompt,
                    }
                    _query_start = time.perf_counter()
                    try:
                        response = await self._fetch_query_result(
                            api_url=text_to_sql_url,
                            payload=request_body,
                            headers=headers,
                        )
                        result_list = self._extract_result_list(response)
                    except Exception as e:
                        logger.exception(
                            f"[TOOL] AskDatabaseAgent _ask_text_to_sql query failed, error={self._format_exception_detail(e)}"
                        )
                        return {"error": self._format_exception_detail(e)}

                    _query_end = time.perf_counter()
                    progress["queried"] += 1
                    logger.debug(
                        f"[AskDatabase] Query Progress: {progress['queried']}/{progress['total']}. "
                        f"Elapsed: {_query_end - _query_start:.2f}s. "
                        f"Sub-question: {task.sub_question}"
                    )

                    _summarize_start = time.perf_counter()
                    try:
                        summary = await self._summarize_result(
                            question=task.sub_question,
                            result_list=result_list,
                        )
                    except Exception as e:
                        logger.exception(
                            f"[TOOL] AskDatabaseAgent _ask_text_to_sql summarize failed, error={self._format_exception_detail(e)}"
                        )
                        return {"error": self._format_exception_detail(e)}

                    _summarize_end = time.perf_counter()
                    progress["summarized"] += 1
                    logger.debug(
                        f"[AskDatabase] Summarize Progress: {progress['summarized']}/{progress['total']}. "
                        f"Elapsed: {_summarize_end - _summarize_start:.2f}s. "
                        f"Sub-question: {task.sub_question}"
                    )

                    return {
                        "summary": summary,
                        "data": result_list,
                    }

                fetch_tasks = [_ask_text_to_sql(task) for task in decomposed_tasks]
                results_nested = await asyncio.gather(
                    *fetch_tasks, return_exceptions=True
                )

                error_list = []
                result_list = []
                data_sources = []

                for task, item in zip(decomposed_tasks, results_nested):
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
                            result_list.append(f"### {task.sub_question}\n{_summary}")
                        if _data:
                            data_sources.extend(_data)

                if error_list and not result_list and not data_sources:
                    error_message = "\n".join(error_list)
                    logger.error(
                        f"[TOOL] AskDatabaseAgent run failed, all sub questions failed, error={error_message}"
                    )
                    return AgentResult(
                        success=False,
                        name=self.name,
                        content="",
                        data_source={},
                        error=error_message,
                    )

                logger.info("[TOOL] AskDatabaseAgent run completed successfully")
                return AgentResult(
                    name=self.name,
                    content="\n".join(result_list),
                    data_source={
                        "source": "ask_database_api",
                        "api_url": text_to_sql_url,
                        "result_count": len(data_sources),
                        "data": data_sources,
                    },
                )
            except Exception as exc:
                error_detail = self._format_exception_detail(exc)
                logger.error(
                    "[TOOL] AskDatabaseAgent run failed, "
                    f"query={request.query}, api_url={text_to_sql_url or '<unresolved>'}, "
                    f"error={error_detail}"
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=error_detail,
                )

    async def _exec_sql_and_summarize(
        self,
        question: str,
        sql: str,
        context: AskDatabaseToolContext,
        environment_url: str,
    ) -> ExecSqlAndSummarizeResult:
        headers = {
            "content-type": "application/json",
            "authentication": context.authentication_token,
            "environment-url": environment_url,
            "task-id": context.request_id or "",
        }
        request_body = {
            "user_id": context.user_id,
            "data_model_id": context.data_model_id,
            "sql": sql,
            "query_mode": context.query_mode,
        }
        # TODO: query 后缀要改成 execute-sql
        execute_sql_api_url = TEXT_TO_SQL_API.replace("/query", "/execute-sql")

        try:
            async with httpx.AsyncClient(
                timeout=(self.timeout or 60) * 0.8,
                trust_env=False,
            ) as client:
                try:
                    response = await client.post(
                        execute_sql_api_url,
                        json=request_body,
                        headers=headers,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response else "unknown"
                    body = exc.response.text if exc.response else ""
                    logger.exception(
                        "ask_database_agent API call failed with HTTP status "
                        f"{status}. Body: {body}"
                    )
                    raise
                except httpx.RequestError:
                    logger.exception(
                        "ask_database_agent API call failed (request error)"
                    )
                    raise

            execute_sql_response: dict = response.json()
        except Exception as e:
            logger.exception(
                f"[TOOL] AskDatabaseAgent _exec_limit_sql failed, error={self._format_exception_detail(e)}"
            )
            return {"error": self._format_exception_detail(e)}

        result_list = self._extract_result_list(execute_sql_response)
        _summarize_start = time.perf_counter()

        try:
            summary = await self._summarize_result(
                question=question,
                result_list=result_list,
            )
        except Exception as e:
            logger.exception(
                f"[TOOL] AskDatabaseAgent _ask_text_to_sql summarize failed, error={self._format_exception_detail(e)}"
            )
            return {"error": self._format_exception_detail(e)}

        _summarize_end = time.perf_counter()

        return {
            "summary": summary,
            "data": result_list,
        }
