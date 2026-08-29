from __future__ import annotations

from .enterprise_kb_api import fetch_aggr_retrieve_as_dict
from .formatter import format_retrieve_response
from .prompts import (
    default_disassemble_queries_user_prompt_template,
    default_summary_sys_prompt,
    default_summary_user_prompt_template,
)

"""ZhiwenAgent.

tool_context 契约说明：

- 推荐放置路径：
  `request.extra.tool_context.zhiwen_agent`
  `request.extra.tool_context.<caller_agent_name>.zhiwen_agent`
- 合并优先级（低 -> 高）：
  1. `request.extra`
  2. `tool_context.zhiwen_agent`
  3. `tool_context.<caller_agent_name>.zhiwen_agent`

字段定义：

- `user_name` (`str`, 必填)：调用用户名称。
- `tenant_id` (`str`, 必填)：租户 ID。
- `user_id` (`str`, 必填)：用户 ID。
- `sources` (`list[str] | None`, 可选)：
  检索源列表；缺失时回退到 `["REPORT_MARKET", "KMS", "OA", "SEP"]`。
- `source_config` (`dict[str, Any] | None`, 条件必填)：
  各检索源配置，直接透传到新检索接口。
- `disassembly_system_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 system prompt，支持 `{current_time}` 占位符。
- `disassembly_user_prompt` (`str | None`, 可选)：
  子问题拆解阶段的 user prompt，支持 `{query}` 占位符。
- `summarize_prompt` (`str | None`, 可选)：
  检索结果汇总时使用的总结提示词。
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, cast

from loguru import logger
from pydantic import (
    BaseModel,
    Field,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from ....config import ZHIWEN_API_URL
from ....utils.global_context import agent_log_context
from ....utils.model_invocation import (
    ModelInvocationRequest,
    StructuredOutput,
)
from ..base import AgentRequest, AgentResult
from ..traceable_agent import TraceableAgent


class ZhiwenDatetimeRange(BaseModel):
    start_datetime: str = Field(..., description="开始日期，例如 2024-01-01")
    end_datetime: str = Field(..., description="结束日期，例如 2025-12-31")


class ZhiwenDatetimeFilter(BaseModel):
    datetime_range: ZhiwenDatetimeRange = Field(
        ...,
        description="时间范围，必须完整提供开始和结束日期。",
    )
    nullable: bool = Field(
        default=True,
        description="固定为 true；当整个 datetime_filter 为 null 时表示不做时间过滤。",
    )


class ZhiwenQueryParams(BaseModel):
    query: str = Field(..., description="检索问题")
    sources: list[str] = Field(
        default_factory=lambda: ["REPORT_MARKET", "KMS", "OA", "SEP"],
        description=(
            "检索源列表，可多选。"
            "REPORT_MARKET=研报市场；KMS=公司知识库,包含公司所有发文（高优先级）；"
            "OA=公司流程，表单；SEP=公司产品说明书/产品介绍"
        ),
    )
    datetime_filter: ZhiwenDatetimeFilter | None = Field(
        default=None,
        description="时间过滤配置。用户提问没有明显提及时间范围时传None；用户显示提及时间则需要填写传递 datetime_range 对象。",
    )


class ZhiwenToolContext(BaseModel):
    """Validated tool_context contract for ZhiwenAgent."""

    user_name: str | None = Field(default=None, description="调用用户名称。")
    tenant_id: str | None = Field(default=None, description="租户 ID。")
    user_id: str | None = Field(default=None, description="用户 ID。")
    sources: list[str] | None = Field(
        default=None,
        description="检索源列表；缺失时使用默认值。",
    )
    source_config: dict[str, Any] | None = Field(
        default=None,
        description="各检索源配置。",
    )
    rerank_model_config: dict[str, Any] | None = Field(
        default=None,
        description="重排序模型配置，将在请求外部 API 时转换为 rerank_param。",
    )
    datetime_filter: ZhiwenDatetimeFilter | None = Field(
        default=None,
        description="时间过滤配置。要么为 null，要么带完整 datetime_range。",
    )
    disassembly_system_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 system prompt，支持 {current_time} 占位符。",
    )
    disassembly_user_prompt: str | None = Field(
        default=None,
        description="子问题拆解阶段的 user prompt，支持 {query} 占位符。",
    )
    summarize_prompt: str | None = Field(
        default=None,
        description="检索结果汇总时使用的总结提示词。",
    )

    @field_validator("user_id", mode="before")
    @classmethod
    def normalize_user_id(cls, value: Any) -> Any:
        if isinstance(value, int):
            return str(value)
        return value

    @field_validator("tenant_id", mode="before")
    @classmethod
    def normalize_tenant_id(cls, value: Any) -> Any:
        if value is None:
            return "dt"
        return value

    @model_validator(mode="after")
    def validate_required_fields(self) -> "ZhiwenToolContext":
        for field_name in ("user_name", "tenant_id", "user_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
        if not isinstance(self.source_config, dict) or not self.source_config:
            raise ValueError("source_config is required")
        return self


NonEmptyDisassemblyQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]


class ZhiwenDisassemblyQueries(RootModel[list[NonEmptyDisassemblyQuery]]):
    pass


class ZhiwenAgent(TraceableAgent):
    name = "zhiwen_agent"
    description = (
        "面向公司员工的检索工具，可以聚合检索多个公司内部文件/知识，包括但不限于公司发文、规章制度、员工福利等，支持通过sources指定检索范围"
    )
    _api_url = ZHIWEN_API_URL
    # _api_url = "http://10.40.0.77:7884/enterprise_kb/source_retrieve"
    timeout = 60.0

    tool_name = name
    tool_description = description
    _disassembly_schema_name = "zhiwen_disassembly_queries"

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": ZhiwenQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.name = "zhiwen_agent"
        self.description = "面向公司员工的检索工具，可以聚合检索多个公司内部文件/知识，包括但不限于公司发文、规章制度、员工福利等，支持通过sources指定检索范围"

    def _merge_extra(self, request: AgentRequest) -> dict[str, Any]:
        extra = dict(request.extra or {})
        tool_context = extra.get("tool_context")
        caller_agent_name = extra.get("caller_agent_name")

        if isinstance(tool_context, dict):
            agent_context = tool_context.get(self.name)
            if isinstance(agent_context, dict):
                extra.update(agent_context)

            if isinstance(caller_agent_name, str) and caller_agent_name.strip():
                caller_context = tool_context.get(caller_agent_name)
                if isinstance(caller_context, dict):
                    nested_agent_context = caller_context.get(self.name)
                    if isinstance(nested_agent_context, dict):
                        extra.update(nested_agent_context)

        return extra

    def _resolve_tool_context(self, request: AgentRequest) -> ZhiwenToolContext:
        return ZhiwenToolContext.model_validate(self._merge_extra(request))

    @staticmethod
    def _normalize_sources(raw_sources: Any) -> list[str]:
        if not isinstance(raw_sources, list):
            return ["REPORT_MARKET", "KMS", "OA", "SEP"]
        normalized: list[str] = []
        for item in raw_sources:
            if isinstance(item, str):
                value = item.strip().upper()
                if value:
                    normalized.append(value)
        if not normalized:
            return ["REPORT_MARKET", "KMS", "OA", "SEP"]
        return normalized

    @staticmethod
    def _read_runtime_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or cleaned in {"missing", "unknown", "-", "None"}:
            return None
        return cleaned

    def _resolve_request_id(self, request: AgentRequest) -> str:
        request_id = self._read_runtime_string((request.extra or {}).get("request_id"))
        if request_id:
            return request_id
        raise RuntimeError("zhiwen_agent requires request_id in request.extra")

    def _resolve_request_token(self, request: AgentRequest) -> str:
        token = self._read_runtime_string((request.extra or {}).get("request_token"))
        if token:
            return token
        else:
            return ""

    def _build_headers(
        self, request: AgentRequest, tool_context: ZhiwenToolContext
    ) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Task-Id": self._resolve_request_id(request),
            "X-Request-Id": uuid.uuid4().hex,
            "X-User-Name": tool_context.user_name or "",
            "X-User-Id": tool_context.user_id or "",
            "X-Staff-Code": request.staff_code,
            "X-Sup-Token": self._resolve_request_token(request),
            "X-Tenant-Id": tool_context.tenant_id or "",
        }

    @staticmethod
    def _convert_rerank_model_config(
        rerank_model_config: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(rerank_model_config, dict) or not rerank_model_config:
            return None

        raw_rerank_model = rerank_model_config.get("rerank_model_name")
        raw_rerank_model_url = rerank_model_config.get("rerank_model_url")
        raw_rerank_auth_token = rerank_model_config.get("rerank_auth_token")
        if not isinstance(raw_rerank_model, str) or not raw_rerank_model.strip():
            return None
        if (
            not isinstance(raw_rerank_model_url, str)
            or not raw_rerank_model_url.strip()
        ):
            return None
        if (
            not isinstance(raw_rerank_auth_token, str)
            or not raw_rerank_auth_token.strip()
        ):
            return None

        rerank_model = raw_rerank_model.strip()
        rerank_model_url = raw_rerank_model_url.strip()
        rerank_auth_token = raw_rerank_auth_token.strip()
        return {
            "rerank_model": rerank_model,
            "rerank_model_url": rerank_model_url,
            "rerank_auth_token": rerank_auth_token,
            # "rerank_score_threshold": 0.05, # 低阈值保召回; 不设置使用服务端默认参数值
        }

    def _build_payload(
        self, request: AgentRequest, *, query: str | None = None
    ) -> dict[str, Any]:
        tool_context = self._resolve_tool_context(request)
        tenant_id = tool_context.tenant_id or ""
        sources = self._normalize_sources(tool_context.sources)
        datetime_filter = (
            tool_context.datetime_filter.model_dump()
            if tool_context.datetime_filter is not None
            else None
        )
        rerank_param = self._convert_rerank_model_config(
            tool_context.rerank_model_config
        )
        if not isinstance(rerank_param, dict) or not rerank_param:
            logger.error(
                "zhiwen_agent requires rerank_model_config in merged extra/tool_context"
            )
            raise RuntimeError(
                "zhiwen_agent requires rerank_model_config in merged extra/tool_context"
            )

        return {
            "search_query": query or request.query,
            "tenant_id": tenant_id,
            "sources": sources,
            "overall_size": 5,
            "score_threshold": 0.3,
            "enable_rerank": True,
            "datetime_filter": datetime_filter,
            "rerank_param": rerank_param,
            "source_config": tool_context.source_config or {},
        }

    def _extract_result(self, data: dict[str, Any]) -> str:
        if not isinstance(data, dict):
            return ""

        for key in ("result", "content", "answer", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        for key in ("result", "content", "answer", "text"):
            value = nested.get(key) if isinstance(nested, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""

    @classmethod
    def _read_prompt(cls, value: Any) -> str | None:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else None
        return None

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

    @staticmethod
    def _build_disassembly_json_schema(max_items: int) -> dict[str, Any]:
        schema = ZhiwenDisassemblyQueries.model_json_schema()
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
            queries = ZhiwenDisassemblyQueries.model_validate(payload)
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
        system_prompt = self._read_prompt(system_prompt)
        user_prompt = self._read_prompt(user_prompt)
        if not system_prompt and not user_prompt:
            return []

        user_content = self._apply_query_template(user_prompt or "", query)
        user_content = default_disassemble_queries_user_prompt_template.format(
            user_content = user_content
        )
        logger.info((f"[TOOL] ZhiwenAgent prompt len:"
                     f"system: {len(system_prompt or '')}"
                     f"user: {len(user_content or '')}"))
        # logger.info(f'user_c: \n{user_content}')
        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": user_content},
        ]
        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=messages,
                structured=StructuredOutput(
                    schema=self._build_disassembly_json_schema(max_items),
                    name=self._disassembly_schema_name,
                    strict=True,
                    parse=False,
                ),
            )
        )
        outcome.raise_for_status()
        self._accumulate_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        raw_items = self._parse_disassembly_array(outcome.content)
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

    async def _run_queries(
        self, request: AgentRequest, queries: list[str]
    ) -> list[dict[str, Any]]:
        tool_context = self._resolve_tool_context(request)
        headers = self._build_headers(request, tool_context)
        payloads = [self._build_payload(request, query=query) for query in queries]
        tasks = [
            fetch_aggr_retrieve_as_dict(
                req_id = headers['X-Request-Id'],
                api_url = self._api_url,
                payload=payload,
                headers=headers,
                timeout=self.timeout) for payload in payloads
        ]
        responses: list[dict[str, Any] | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        items: list[dict[str, Any]] = []
        for query, payload, response in zip(queries, payloads, responses):
            if isinstance(response, BaseException):
                items.append(
                    self._build_result_item(
                        query,
                        success=False,
                        result="",
                        payload=payload,
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
                        payload=payload,
                        data={},
                        error=f"unexpected response type: {type(response).__name__}",
                    )
                )
                continue
            result = self._extract_result(response)
            items.append(
                self._build_result_item(
                    query,
                    success=True,
                    result=result,
                    payload=payload,
                    data=response,
                    error=None,
                )
            )
        return items

    async def _summarize_single_result(
        self,
        *,
        sub_query: str,
        result: str,
        data: dict[str, Any],
        prompt: str | None,
    ) -> str:
        result_text = result.strip()
        if not result_text:
            result_text = json.dumps(data, ensure_ascii=False)
        if not result_text.strip():
            return ""

        summary_timeout = (self.timeout or 60) * 0.8
        summary_prompt = self._read_prompt(prompt)
        if not summary_prompt: # 外层不会透传（所以一定为None），
            summary_prompt = default_summary_sys_prompt
        summary_prompt = self._append_current_date_hint(summary_prompt)
        user_content = default_summary_user_prompt_template.format(
            sub_query=sub_query,
            result_text=result_text
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

    async def _summarize_multi_result(
        self, items: list[dict[str, Any]], prompt: str | None
    ) -> list[dict[str, str]]:
        if not items:
            return []

        tasks = []
        task_indexes: list[int] = []
        summaries: list[dict[str, str]] = []

        for index, item in enumerate(items):
            sub_query = str(item.get("query") or "").strip()
            if not sub_query:
                continue

            result = str(item.get("result") or "")
            error = str(item.get("error") or "").strip()
            if error:
                summary = f"查询失败：{error}"
                item["summary"] = summary
                summaries.append({"query": sub_query, "summary": summary})
                continue

            tasks.append(
                self._summarize_single_result(
                    sub_query=sub_query,
                    result=result,
                    data=(
                        cast(dict[str, Any], item.get("data"))
                        if isinstance(item.get("data"), dict)
                        else {}
                    ),
                    prompt=prompt,
                )
            )
            task_indexes.append(index)

        if tasks:
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for item_index, response in zip(task_indexes, responses):
                item = items[item_index]
                sub_query = str(item.get("query") or "").strip()
                if not sub_query:
                    continue
                if isinstance(response, BaseException):
                    summary = f"总结失败：{response}"
                else:
                    summary = str(response or "").strip() or "未生成总结。"
                item["summary"] = summary
                summaries.append({"query": sub_query, "summary": summary})

        summaries.sort(
            key=lambda x: next(
                (
                    i
                    for i, item in enumerate(items)
                    if str(item.get("query") or "").strip() == x["query"]
                ),
                len(items),
            )
        )
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


    @classmethod
    def _compose_content_without_summary(cls, query: str, items: list[dict[str, str]]) -> str:
        if not items:
            return ""

        sections: list[str] = [f"问题：{query}"]
        for index, item in enumerate(items, start=1):
            sub_query = item.get("query", "")
            if sub_query:
                response = item.get('data', {})
                if response and isinstance(response, dict):
                    res_data = response.get('data', {})
                    if res_data and isinstance(res_data, dict):
                        summary = format_retrieve_response(res_data=res_data)
                        # res_data = response.get('data', None)
                        # summary = json.dumps(res_data, ensure_ascii=False) if res_data else "无检索结果。"
                        sections.append(f"{index}. 子问题：{sub_query}\n检索结果：{summary}")
        return "\n\n".join(sections)

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"[TOOL] ZhiwenAgent run started, query={request.query}")
            try:
                tool_context = self._resolve_tool_context(request)
                disassembly_system_prompt = self._read_prompt(
                    tool_context.disassembly_system_prompt
                )
                disassembly_system_prompt = self._inject_current_time(
                    disassembly_system_prompt
                )
                disassembly_user_prompt = self._read_prompt(
                    tool_context.disassembly_user_prompt
                )
                summarize_prompt = self._read_prompt(tool_context.summarize_prompt)
                logger.info("[TOOL] ZhiwenAgent starts disassemble_queries")
                sub_queries = await self.disassemble_queries(
                    request.query,
                    system_prompt=disassembly_system_prompt,
                    user_prompt=disassembly_user_prompt,
                )
                all_queries = self._build_query_list(request.query, sub_queries)
                logger.info(f"[TOOL] ZhiwenAgent finishes disassemble_queries, queries: {all_queries}")

                items = await self._run_queries(request, all_queries)
                logger.info("[TOOL] ZhiwenAgent finishes fetch result by sub queries")


                do_mult_summary = False
                if do_mult_summary:
                    logger.debug("[TOOL] ZhiwenAgent summarizing result")
                    summaries = await self._summarize_multi_result(items, summarize_prompt)
                    content = self._compose_content(request.query, summaries)

                else:
                    content = self._compose_content_without_summary(
                        query=request.query,
                        items=items,
                    )
                success = any(item.get("success") for item in items)
                error = None
                if not success:
                    errors = [item.get("error") for item in items if item.get("error")]
                    error = errors[0] if errors else "all queries failed"

                logger.info(f"[TOOL] ZhiwenAgent run completed successfully, content len: {len(content)}")
                return AgentResult(
                    success=success,
                    name=self.name,
                    content=content,
                    data_source={
                        "source": "local_doc_qa_aggr_retrieve",
                        "data": items,
                    },
                    meta_data={
                        "decomposed_queries": sub_queries,
                        "all_queries": all_queries,
                    },
                    error=error,
                )
            except Exception as exc:
                request_id = (
                    self._read_runtime_string((request.extra or {}).get("request_id"))
                    or ""
                )
                logger.error(
                    "[TOOL] ZhiwenAgent run failed, "
                    f"request_id={request_id}, error={exc}"
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )
