from __future__ import annotations

"""EfficiencyPiAgent.

tool_context 契约说明：

- 推荐放置路径：
  `request.extra.tool_context.<caller_agent_name>.efficiency_pi_agent`
- 当前实现也兼容：
  `request.extra.tool_context.efficiency_pi_agent`
- 合并优先级（低 -> 高）：
    1. request.extra.tool_context.<self.name>
    2. request.extra.tool_context.<caller_agent_name>.<self.name>

字段定义：

- `enable_query_disassembly` (`bool`, 可选)：
  是否启用问题拆解流程，默认 `False`。
- `authentication` (`str`, 可选)：
  调用效率派图API所需的认证信息（token字符串），默认为 `"missing_token"`。
  实际运行时会按优先级从以下位置读取：tool_context 配置、request_id_context、extra。
- `ng_space` (`str`, 可选)：
  效率派图数据库空间名称，默认为 `AIM_GRAPH_SPACE` 配置值（如 `efficiency_graph_sbx`）。
- `user_id` (`int`, 必填)：
  下游 text-to-ngql 接口要求的用户 ID。
- `agent_id` (`int`, 必填)：
  下游 text-to-ngql 接口要求的智能体 ID。
- `query_mode` (`str`, 必填)：
  查询模式，仅支持 `publish`/`edit`；兼容输入 `RELEASE_STATE`/`EDITORIAL_STATE`。
- `environment_url` (`str`, 必填)：
  下游接口要求的 `environment-url` header；也兼容从 `backend_env_base_url` 读取。
- `disassembly_system_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 system prompt，会在末尾自动追加当前日期。
- `disassembly_user_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 user prompt，支持 `{query}` 占位符。
- `summarize_prompt` (`str | None`, 可选)：
  多条查询结果汇总时使用的总结提示词。

约束：

- 本 agent 不要求必填 tool_context；`enable_query_disassembly`、拆解/总结 prompt
  缺失时，退化为“不拆解”或使用内置总结逻辑。
- 运行态必须显式提供 `user_id`、`agent_id`、`query_mode`、`staff_code`；
  不再提供默认值回退。
- 只有上述字段被视为 EfficiencyPiAgent 自身的 tool_context 契约；其他额外字段
  即使出现在 `request.extra` 中，也不应被当作该工具的稳定输入约定。
"""

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field, RootModel, StringConstraints

from ...config import AIM_GRAPH_SPACE, EFFI_API
from ...utils.global_context import agent_log_context, request_id_ctx
from .base import AgentRequest, AgentResult
from .tool_context_utils import resolve_agent_tool_context_overlay
from .traceable_agent import TraceableAgent


class EfficiencyPiQueryParams(BaseModel):
    query: str = Field(..., description="查询问题")
    staff_code: str = Field(..., description="发起查询的员工的工号")
    summarize: bool = Field(default=True, description="是否让工具生成摘要")


class EfficiencyPiToolContext(BaseModel):
    """Validated tool_context contract for EfficiencyPiAgent."""

    enable_query_disassembly: bool = Field(
        default=True,
        description="是否启用问题拆解流程。",
    )
    disassembly_system_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 system prompt，会在末尾自动追加当前日期。",
    )
    disassembly_user_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 user prompt，支持 {query} 占位符。",
    )
    summarize_prompt: str | None = Field(
        default=None,
        description="多条查询结果汇总时的总结提示词。",
    )
    ng_space: str = Field(
        default=AIM_GRAPH_SPACE,
        description="效率派图数据库空间名称，默认为 efficiency_graph_sbx",
    )
    user_id: int | None = Field(
        default=None,
        description="下游 text-to-ngql 接口要求的用户 ID。",
    )
    agent_id: int | None = Field(
        default=None,
        description="下游 text-to-ngql 接口要求的智能体 ID。",
    )
    query_mode: str | None = Field(
        default=None,
        description="查询模式，支持 publish/edit，兼容 RELEASE_STATE/EDITORIAL_STATE。",
    )
    environment_url: str | None = Field(
        default=None,
        description="下游接口要求的 environment-url header。",
    )
    authentication: str = Field(
        default="missing_token",
        description="调用效率派图API所需的认证信息，通常是一个token字符串",
    )


NonEmptyDisassemblyQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]


class EfficiencyPiDisassemblyQueries(RootModel[list[NonEmptyDisassemblyQuery]]):
    pass


DEFAULT_DISASSEMBLY_SYSTEM_PROMPT = """你是问题拆解助手。请把用户问题拆成1-3个可独立查询的子问题。你应该把对于多个主体的复杂查询分解为对于单个主体的查询。对于某个主体多维度的查询拆解为有限个数个单维度的查询。拆解后的问题 MUST 拥有明确的主语。
作为参考，下游支持的查询范围为人力资源有关数据，主要包括但不限于：
- 人员日程
- 人员日报/周报
- 人员详情
- 会议
- 公司组织架构
- 人员考勤出勤
- 等
---
### 补充信息
- 如无额外信息，则问题所指的公司是MAP（Multi Agent Path）有限公司
- 拆分后的子问题不要包含原问题"""

DEFAULT_DISASSEMBLY_USER_PROMPT = "请拆解这个问题：{query}"


class EfficiencyPiAgent(TraceableAgent):
    name = "efficiency_pi_agent"
    description = "查询人力平台有关员工和组织架构的所有信息"
    _api_url = EFFI_API
    timeout = 120

    tool_name = name
    tool_description = description
    _disassembly_schema_name = "efficiency_pi_disassembly_queries"

    @staticmethod
    def _is_successful_response(data: dict[str, Any]) -> bool:
        """Support legacy and current API response contracts."""
        if data.get("code") is not None:
            return data.get("code") == 200
        if data.get("status") == "success":
            return True
        return bool(data.get("success") is True and not data.get("error"))

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": EfficiencyPiQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.name = "efficiency_pi_agent"
        self.description = "查询人力平台有关员工和组织架构的所有信息"

    @staticmethod
    def _mask_secret(value: str | None, *, prefix: int = 6, suffix: int = 4) -> str:
        if not value:
            return "missing"
        if len(value) <= prefix + suffix:
            return "*" * len(value)
        return f"{value[:prefix]}***{value[-suffix:]}"

    def _build_request_log_context(
        self,
        *,
        task_id: str,
        environment_url: str,
        payload: dict[str, Any],
        authentication: str,
    ) -> dict[str, Any]:
        return {
            "api_url": self._api_url,
            "task_id": task_id,
            "environment_url": environment_url,
            "payload": payload,
            "headers": {
                "content-type": "application/json",
                "task-id": task_id,
                "authentication": self._mask_secret(authentication),
                "environment-url": environment_url,
            },
        }

    async def _fetch_external_pi(
        self,
        authentication: str,
        task_id: str,
        environment_url: str,
        ng_space: str,
        query: str,
        user_id: int,
        agent_id: int,
        query_mode: str,
        staff_code: str | None = None,
    ) -> dict[str, Any]:
        """Fetch efficiency PI data."""
        payload = {
            "query": query,
            "user_id": user_id,
            "agent_id": agent_id,
            "query_mode": query_mode,
            "staff_code": staff_code,
            "ng_space_name": ng_space,
        }
        request_log_context = self._build_request_log_context(
            task_id=task_id,
            environment_url=environment_url,
            payload=payload,
            authentication=authentication,
        )
        async with httpx.AsyncClient(timeout=(self.timeout or 60) * 0.8) as client:
            try:
                response = await client.post(
                    self._api_url,
                    json=payload,
                    headers={
                        "content-type": "application/json",
                        "task-id": task_id,
                        "authentication": authentication,
                        "environment-url": environment_url,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                body = exc.response.text if exc.response else ""
                logger.exception(
                    "efficiency_pi_agent API call failed with HTTP status {}. "
                    "request_context={}, response_body={}",
                    status,
                    request_log_context,
                    body,
                )
                raise
            except httpx.RequestError as exc:
                request_url = str(exc.request.url) if exc.request else self._api_url
                logger.exception(
                    "efficiency_pi_agent API call failed (request error). "
                    "error_type={}, error={}, request_url={}, request_context={}, cause={!r}",
                    type(exc).__name__,
                    str(exc),
                    request_url,
                    request_log_context,
                    exc.__cause__,
                )
                raise

        data = response.json()
        if not self._is_successful_response(data):
            logger.error(f"efficiency_pi_agent API status not success: {data}")
        return data

    def _extract_result(self, data: dict[str, Any]) -> str:
        # Legacy contract: {"results": [{"result": "..."}]}
        results = data.get("results") or []
        if not results:
            # Current contract: {"tool_call_results": [{"data": ...}]}
            tool_call_results = data.get("tool_call_results") or []
            if not tool_call_results:
                return ""
            valid_results: list[dict[str, Any]] = []
            for item in tool_call_results:
                if not isinstance(item, dict):
                    continue
                if self._normalize_tool_error(item.get("error")):
                    continue
                tool_data = item.get("data")
                if tool_data is None:
                    continue
                valid_results.append(
                    {
                        "tool_name": item.get("tool_name"),
                        "arguments": item.get("arguments"),
                        "data": tool_data,
                    }
                )
            if not valid_results:
                return ""
            if len(valid_results) == 1:
                tool_data = valid_results[0]["data"]
                if isinstance(tool_data, (dict, list)):
                    return json.dumps(tool_data, ensure_ascii=False)
                return str(tool_data)
            return json.dumps(valid_results, ensure_ascii=False)
        return str(results[0].get("result") or "")

    def _merge_extra(self, request: AgentRequest) -> dict[str, Any]:
        """Merge EfficiencyPiAgent tool_context overlays only.

        Priority (low -> high):
        1. request.extra.tool_context.<self.name>
        2. request.extra.tool_context.<caller_agent_name>.<self.name>
        """
        return resolve_agent_tool_context_overlay(
            request,
            agent_name=self.name,
            include_top_level_agent_context=True,
            include_caller_nested_agent_context=True,
        )

    def _resolve_tool_context(self, request: AgentRequest) -> EfficiencyPiToolContext:
        return EfficiencyPiToolContext.model_validate(self._merge_extra(request))

    @staticmethod
    def _read_prompt(value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else None
        return None

    @staticmethod
    def _read_runtime_token(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or cleaned in {"missing", "missing_token"}:
            return None
        return cleaned

    def _resolve_authentication(
        self, request: AgentRequest, tool_context: EfficiencyPiToolContext
    ) -> str:
        extra = request.extra or {}
        request_id_context = extra.get("request_id")

        candidates: list[Any] = [tool_context.authentication]
        if isinstance(request_id_context, dict):
            candidates.extend(
                [
                    request_id_context.get("authentication"),
                    request_id_context.get("request_token"),
                ]
            )
        candidates.extend(
            [
                extra.get("authentication"),
                extra.get("request_token"),
            ]
        )

        for candidate in candidates:
            token = self._read_runtime_token(candidate)
            if token:
                return token
        return "missing_token"

    @staticmethod
    def _read_runtime_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or cleaned in {"missing", "unknown", "-", "None"}:
            return None
        return cleaned

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned.isdigit():
                parsed = int(cleaned)
                return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _normalize_query_mode(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        normalized = raw.lower()
        if normalized in {"publish", "edit"}:
            return normalized
        if raw == "RELEASE_STATE":
            return "publish"
        if raw == "EDITORIAL_STATE":
            return "edit"
        return None

    def _resolve_task_id(self, request: AgentRequest) -> str:
        extra = request.extra or {}
        candidates: list[Any] = [
            request_id_ctx.get("missing"),
            extra.get("request_id"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                nested_id = candidate.get("request_id")
                cleaned = self._read_runtime_string(nested_id)
                if cleaned:
                    return cleaned
                continue
            cleaned = self._read_runtime_string(candidate)
            if cleaned:
                return cleaned
        return "missing"

    def _resolve_request_runtime_context(
        self,
        request: AgentRequest,
        tool_context: EfficiencyPiToolContext,
    ) -> tuple[int, int, str, str]:
        overlay = self._merge_extra(request)
        extra = request.extra or {}

        user_id = self._coerce_positive_int(
            overlay.get("user_id")
            or tool_context.user_id
            or extra.get("user_id")
        )
        if user_id is None:
            raise ValueError(
                "efficiency_pi_agent 缺少 user_id（tool_context.user_id 或 request.extra.user_id）"
            )

        agent_id = self._coerce_positive_int(
            overlay.get("agent_id")
            or tool_context.agent_id
            or extra.get("agent_id")
        )
        if agent_id is None:
            raise ValueError(
                "efficiency_pi_agent 缺少 agent_id（tool_context.agent_id 或 request.extra.agent_id）"
            )

        query_mode = self._normalize_query_mode(
            overlay.get("query_mode")
            or overlay.get("backend_env")
            or tool_context.query_mode
            or extra.get("query_mode")
            or extra.get("backend_env")
        )
        if query_mode is None:
            raise ValueError(
                "efficiency_pi_agent 缺少 query_mode（tool_context.query_mode 或 request.extra.query_mode/backend_env）"
            )

        environment_url = self._read_runtime_string(
            extra.get("backend_env_base_url")
        )
        if environment_url is None:
            raise ValueError(
                "efficiency_pi_agent 缺少 environment_url（request.extra.backend_env_base_url）"
            )

        return user_id, agent_id, query_mode, environment_url

    def _resolve_staff_code(self, request: AgentRequest) -> str:
        extra = request.extra or {}
        candidates: list[Any] = [
            request.staff_code,
            extra.get("staff_code"),
        ]
        for candidate in candidates:
            cleaned = self._read_runtime_string(candidate)
            if cleaned:
                return cleaned
        raise ValueError(
            "efficiency_pi_agent 缺少 staff_code（request.staff_code 或 request.extra.staff_code）"
        )

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

    @staticmethod
    def _build_disassembly_json_schema(max_items: int) -> dict[str, Any]:
        schema = EfficiencyPiDisassemblyQueries.model_json_schema()
        schema["maxItems"] = max_items
        return schema

    @staticmethod
    def _parse_disassembly_array(text: str) -> list[str]:
        if not isinstance(text, str):
            return []
        stripped = text.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        try:
            queries = EfficiencyPiDisassemblyQueries.model_validate(payload)
        except Exception:
            return []
        return list(queries.root)

    async def disassemble_queries(
        self,
        query: str,
        system_prompt: str | None,
        user_prompt: str | None,
        *,
        max_items: int = 6,
    ) -> list[str]:
        system_prompt = self._append_current_date_hint(
            self._read_prompt(system_prompt) or DEFAULT_DISASSEMBLY_SYSTEM_PROMPT
        )
        user_prompt = self._read_prompt(user_prompt) or DEFAULT_DISASSEMBLY_USER_PROMPT

        user_content = self._apply_query_template(user_prompt or "", query)

        response = await self.llm.asimple_chat(
            prompt=user_content,
            system_prompt=system_prompt or "",
            json_schema=self._build_disassembly_json_schema(max_items),
            schema_name=self._disassembly_schema_name,
            schema_strict=True,
        )
        self._accumulate_usage(response.usage)
        raw_items = self._parse_disassembly_array(response.content)
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            value = item.strip()
            if not value or value in seen or value == query:
                continue
            seen.add(value)
            cleaned.append(value)
            if len(cleaned) >= max_items:
                break
        return cleaned

    @staticmethod
    def _build_query_list(original_query: str, sub_queries: list[str]) -> list[str]:
        deduped = [item for item in sub_queries if item != original_query]
        deduped.append(original_query)
        return deduped

    @staticmethod
    def _build_result_item(
        query: str,
        *,
        success: bool,
        result: str,
        data: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "success": success,
            "result": result,
            "data": data,
            "error": error,
        }

    @staticmethod
    def _normalize_tool_error(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or not text.replace("|", "").strip():
            return None
        return text

    @staticmethod
    def _extract_response_error(data: dict[str, Any]) -> str | None:
        if data.get("code") is not None and data.get("code") != 200:
            return str(
                data.get("message")
                or data.get("error")
                or f"text-to-ngql code={data.get('code')}"
            )
        if data.get("status") not in (None, "success"):
            return str(data.get("error") or data.get("message") or "status not success")
        if data.get("success") is False:
            return str(data.get("error") or data.get("message") or "success=false")

        tool_call_results = data.get("tool_call_results")
        if isinstance(tool_call_results, list):
            for item in tool_call_results:
                if not isinstance(item, dict):
                    continue
                tool_error = EfficiencyPiAgent._normalize_tool_error(
                    item.get("error")
                )
                if tool_error:
                    return tool_error
        return None

    async def _run_queries(
        self,
        ng_space: str,
        authentication: str,
        queries: list[str],
        staff_code: str | None,
        *,
        user_id: int,
        agent_id: int,
        query_mode: str,
        environment_url: str,
        task_id: str,
    ) -> list[dict[str, Any]]:
        tasks = [
            self._fetch_external_pi(
                authentication=authentication,
                task_id=task_id,
                environment_url=environment_url,
                ng_space=ng_space,
                query=query,
                user_id=user_id,
                agent_id=agent_id,
                query_mode=query_mode,
                staff_code=staff_code,
            )
            for query in queries
        ]
        responses: list[dict[str, Any] | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        items: list[dict[str, Any]] = []
        for query, response in zip(queries, responses):
            if isinstance(response, BaseException):
                items.append(
                    self._build_result_item(
                        query,
                        success=False,
                        result="",
                        data={},
                        error=str(response),
                    )
                )
                continue
            if not isinstance(response, dict):
                items.append(
                    self._build_result_item(
                        query,
                        success=False,
                        result="",
                        data={},
                        error=f"unexpected response type: {type(response).__name__}",
                    )
                )
                continue
            response_error = self._extract_response_error(response)
            result = self._extract_result(response)
            items.append(
                self._build_result_item(
                    query,
                    success=response_error is None,
                    result=result,
                    data=response,
                    error=response_error,
                )
            )
        return items

    async def _summarize_single_result(
        self,
        *,
        sub_query: str,
        result: str,
        prompt: str | None,
    ) -> str:
        if not str(result).strip():
            return ""
        summary_timeout = (self.timeout or 60) * 0.8
        summary_prompt = self._read_prompt(prompt)
        if not summary_prompt:
            summary_prompt = (
                "你是文本总结提炼智能体。请基于查询结果生成总结。"
                "突出关键数据、时间范围与结论，并与提炼所有与用户问题相关的数据资料。"
                "注意工具入参staff_code字段值为发起查询的员工的工号，和被查询者的工号可能不同。"
            )
        summary_prompt = self._append_current_date_hint(summary_prompt)
        user_content = (
            f"子问题：{sub_query}\n"
            f"查询结果：{result}\n\n"
            "请开始总结摘要"
            # "请用1-3条要点输出，聚焦关键结论。"
        )
        response = await self.llm.ainvoke(
            [
                {"role": "system", "content": summary_prompt},
                {"role": "user", "content": user_content},
            ],
            timeout=summary_timeout,
        )
        self._accumulate_usage(response.usage)

        return response.content.strip()

    async def _summarize_multi_result(
        self, items: list[dict[str, Any]], query: str, prompt: str | None
    ) -> list[dict[str, str]]:
        if not items:
            return []

        summaries: list[dict[str, str]] = []
        for item in items:
            sub_query = str(item.get("query") or "").strip()
            if not sub_query:
                continue
            result = str(item.get("result") or "")
            error = str(item.get("error") or "").strip()

            summary = ""
            if result.strip():
                summary = await self._summarize_single_result(
                    sub_query=sub_query,
                    result=result,
                    prompt=prompt,
                )
            elif error:
                summary = f"查询失败：{error}"
            else:
                summary = "未获取到有效结果。"

            item["summary"] = summary
            summaries.append({"query": sub_query, "summary": summary})

        return summaries

    @staticmethod
    def _compose_content(query: str, summaries: list[dict[str, str]]) -> str:
        if not summaries:
            return ""

        sections: list[str] = [f"问题：{query}"]
        for index, item in enumerate(summaries, start=1):
            sub_query = item.get("query", "")
            summary = item.get("summary", "") or "未生成总结。"
            sections.append(f"{index}. 子问题：{sub_query}\n总结：{summary}")
        return "\n\n".join(sections)

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"[TOOL] EfficiencyPiAgent run started, query={request.query}")
            try:
                staff_code = self._resolve_staff_code(request)
                tool_context = self._resolve_tool_context(request)
                enable_query_disassembly = tool_context.enable_query_disassembly
                disassembly_system_prompt = tool_context.disassembly_system_prompt
                disassembly_user_prompt = tool_context.disassembly_user_prompt
                summarize_prompt = self._read_prompt(tool_context.summarize_prompt)

                sub_queries: list[str] = []
                if enable_query_disassembly:
                    sub_queries = await self.disassemble_queries(
                        request.query,
                        system_prompt=disassembly_system_prompt,
                        user_prompt=disassembly_user_prompt,
                    )
                all_queries = self._build_query_list(request.query, sub_queries)
                authentication = self._resolve_authentication(request, tool_context)
                user_id, agent_id, query_mode, environment_url = (
                    self._resolve_request_runtime_context(request, tool_context)
                )
                task_id = self._resolve_task_id(request)
                items = await self._run_queries(
                    tool_context.ng_space,
                    authentication,
                    all_queries,
                    staff_code,
                    user_id=user_id,
                    agent_id=agent_id,
                    query_mode=query_mode,
                    environment_url=environment_url,
                    task_id=task_id,
                )

                logger.debug("[TOOL] EfficiencyPiAgent summarizing result")
                summaries = await self._summarize_multi_result(
                    items, request.query, summarize_prompt
                )
                content = self._compose_content(request.query, summaries)

                success = any(item.get("success") for item in items)
                error = None
                if not success:
                    errors = [item.get("error") for item in items if item.get("error")]
                    error = errors[0] if errors else "all queries failed"

                logger.info("[TOOL] EfficiencyPiAgent run completed successfully")
                return AgentResult(
                    success=success,
                    name=self.name,
                    content=content,
                    data_source={"source": "efficiency_api", "data": items},
                    meta_data={
                        "decomposed_queries": sub_queries,
                        "all_queries": all_queries,
                    },
                    error=error,
                )
            except Exception as exc:
                logger.exception(
                    "[TOOL] EfficiencyPiAgent run failed, query={}, staff_code={}, error={}",
                    request.query,
                    request.staff_code,
                    str(exc),
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )
