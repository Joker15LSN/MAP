from __future__ import annotations

"""WebSearchAgent.

参数契约说明：

- `tool_context` 推荐放置路径：
  `request.extra.tool_context.<caller_agent_name>.web_search_agent`
- 当前实现会先读取 `request.extra`，再用
  `tool_context.<caller_agent_name>.web_search_agent` 中的同名字段补充默认值。
- 当前不会兼容 `tool_context.web_search_agent` 顶层路径。

字段定义：

- 来自 `request.extra`：
  - `min_score` (`float | int | str`, 可选，默认 `0.45`)：
    搜索接口打分阈值，写入请求 payload 的 `min_score`。

- 来自 `request.extra.tool_context.<caller_agent_name>.web_search_agent`：
  - `enable_query_disassembly` (`bool`, 可选，默认 `True`)：
    是否启用问题拆解流程。
  - `enable_disassembly_summary` (`bool`, 可选，默认 `False`)：
    是否对拆解检索结果执行多结果汇总。
  - `debug` (`bool`, 可选，默认 `False`)：
    为 `true` 时输出拆分后的问题与 `query_type`，以及每个子问题的检索原始结果。
  - `summarize_prompt` (`str | None`, 可选)：
    搜索结果总结阶段的 system prompt。
  - `disassembly_system_prompt` (`str | None`, 可选)：
    拆解阶段 system prompt。
  - `disassembly_user_prompt` (`str | None`, 可选)：
    拆解阶段 user prompt，支持 `{query}` 占位符。

- 当前实际发送给外部搜索接口的 payload 字段：
  `query`、`query_type`、`min_score`，以及有值时附带的
  `freshness`、`site`、`language`。
"""

import asyncio
import json
from datetime import date
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from ...config import WEB_SEARCH_API
from ...utils.global_context import agent_log_context
from ...utils.model_invocation import ModelInvocationRequest
from .base import AgentRequest, AgentResult
from .prompt.web_search_prompt import web_search_prompt
from .tool_context_utils import merge_extra_with_agent_tool_context_defaults
from .traceable_agent import TraceableAgent


class WebSearchQueryParams(BaseModel):
    query: str = Field(..., description="待联网搜索的问题")


class WebSearchToolContext(BaseModel):
    """Validated tool_context contract for WebSearchAgent."""

    enable_query_disassembly: bool = Field(
        default=True,
        description="是否启用问题拆解流程。",
    )
    enable_disassembly_summary: bool = Field(
        default=False,
        description="是否对拆解检索结果执行多结果汇总。",
    )
    debug: bool = Field(
        default=False,
        description="是否输出拆分问题与检索结果调试日志。",
    )
    summarize_prompt: str | None = Field(
        default=("你是联网搜索总结助手。请基于检索结果直接回答问题。"),
        description="搜索结果总结阶段 system prompt。",
    )
    disassembly_system_prompt: str | None = Field(
        default=web_search_prompt or (
            "你是问题拆解助手。请将用户问题拆成可独立联网检索的2到5个子问题，"
            "每个子问题只表达一个明确检索意图，并为其选择最合适的搜索源类型："
            "finance、general 或 academic。"
            "输出必须严格符合 JSON Schema，只返回 JSON 对象，不要输出任何额外解释。"
        ),
        description="拆解阶段 system prompt。",
    )
    disassembly_user_prompt: str | None = Field(
        default=(
            "请拆解这个问题：{query}\n\n"
            "要求：\n"
            "1. 只保留有助于回答原问题的子问题。\n"
            "2. 每个子问题都应能直接发起一次搜索。\n"
            "3. 为每个子问题标注 query_type，只能是 finance、general、academic 之一。\n"
            '4. 返回格式必须为 {"queries":[{"query":"问题","query_type":"general"}]}。'
        ),
        description="拆解阶段 user prompt。",
    )


class WebPageItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id_: str | None = Field(default=None, alias="id")
    name: str = ""
    url: str = ""
    summary: str = ""
    source_logo_url: str | None = None
    date_published: str | None = Field(default=None, alias="datePublished")
    site_name: str | None = Field(default=None, alias="siteName")


class RoutedSearchQuery(BaseModel):
    query: str = Field(..., min_length=1, description="可独立执行搜索的子问题")
    query_type: Literal["finance", "general", "academic"] = Field(
        ...,
        description="搜索源类型：finance/general/academic",
    )


class RoutedSearchPlan(BaseModel):
    queries: list[RoutedSearchQuery] = Field(
        default_factory=list,
        description="拆分后的子问题及其搜索源类型",
    )


class WebSearchAgent(TraceableAgent):
    name = "web_search_agent"
    description = (
        "按 finance/general/academic 三类搜索源联网检索公开信息，"
        "返回结构化搜索结果和摘要。"
    )
    default_api_url = WEB_SEARCH_API
    default_min_score = 0.45
    default_result_count = 5
    default_freshness = "noLimit"
    default_site: str | None = None
    default_language: str | None = None
    default_summarize = False
    timeout = 45.0

    tool_name = name
    tool_description = description
    _disassembly_schema_name = "web_search_disassembly_queries"

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": WebSearchQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.name = "web_search_agent"
        self.description = (
            "按 finance/general/academic 三类搜索源联网检索公开信息，"
            "返回结构化搜索结果和摘要。"
        )

    def _merge_extra(self, request: AgentRequest) -> dict[str, Any]:
        return merge_extra_with_agent_tool_context_defaults(
            request,
            agent_name=self.name,
            include_top_level_agent_context=False,
            include_caller_nested_agent_context=True,
        )

    def _resolve_tool_context(self, request: AgentRequest) -> WebSearchToolContext:
        return WebSearchToolContext.model_validate(self._merge_extra(request))

    @staticmethod
    def _current_date_text() -> str:
        return f"当前日期：{date.today().isoformat()}"

    @staticmethod
    def _normalize_freshness(value: str | None) -> str | None:
        if value is None:
            return None
        raw = value.strip()
        if not raw:
            return None

        if raw in {"oneWeek", "oneMonth", "oneYear", "noLimit"}:
            return raw

        return None

    @staticmethod
    def _normalize_site(value: str | None) -> str | None:
        if value is None:
            return None
        site = value.strip()
        if not site:
            return None
        site = site.removeprefix("https://").removeprefix("http://")
        return site.rstrip("/") or None

    @staticmethod
    def _normalize_language(value: str | None) -> str | None:
        if value is None:
            return None
        language = value.strip()
        return language or None

    @classmethod
    def _resolve_api_url(cls, value: Any) -> str:
        if isinstance(value, str):
            api_url = value.strip()
            if api_url:
                return api_url
        return cls.default_api_url

    @staticmethod
    def _assemble_payload(
        search_query: str,
        *,
        query_type: Literal["finance", "general", "academic"],
        min_score: float,
        freshness: str | None = None,
        site: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": search_query,
            "query_type": query_type,
            "min_score": min_score,
        }
        if freshness:
            payload["freshness"] = freshness
        if site:
            payload["site"] = site
        if language:
            payload["language"] = language
        return payload

    @classmethod
    def _resolve_min_score(cls, value: Any) -> float:
        if isinstance(value, bool):
            return cls.default_min_score
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    logger.warning(
                        "[TOOL] WebSearchAgent invalid min_score="
                        f"{value}, fallback to {cls.default_min_score}"
                    )
        return cls.default_min_score

    async def _fetch_web_search(
        self,
        query: str,
        *,
        query_type: Literal["finance", "general", "academic"] = "general",
        min_score: float,
        api_url: str | None = None,
        freshness: str | None = None,
        site: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        resolved_api_url = self._resolve_api_url(api_url)
        payload = self._assemble_payload(
            search_query=query,
            query_type=query_type,
            min_score=min_score,
            freshness=freshness,
            site=site,
            language=language,
        )

        async with httpx.AsyncClient(
            timeout=(self.timeout or 60) * 0.8,
            trust_env=False,
        ) as client:
            try:
                response = await client.post(
                    resolved_api_url,
                    json=payload,
                    headers={"content-type": "application/json"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = (
                    exc.response.status_code if exc.response is not None else "unknown"
                )
                body = exc.response.text if exc.response is not None else ""
                logger.error(
                    "web_search_agent API call failed with HTTP status "
                    f"{status}. Body: {body}"
                )
                raise RuntimeError(
                    f"search api returned HTTP {status}: {body or 'empty body'}"
                ) from exc
            except httpx.TimeoutException as exc:
                logger.exception("web_search_agent API call timeout")
                raise RuntimeError("search api timeout") from exc
            except httpx.RequestError as exc:
                method = exc.request.method if exc.request is not None else "unknown"
                url = (
                    str(exc.request.url)
                    if exc.request is not None
                    else resolved_api_url
                )
                logger.error(
                    "web_search_agent API call failed: "
                    f"{type(exc).__name__}: {exc}; request={method} {url}"
                )
                raise RuntimeError(
                    f"search api request failed: {type(exc).__name__}: {exc}"
                ) from exc

        response_text = response.text.strip()
        if not response_text:
            return {}

        try:
            payload_data = response.json()
        except json.JSONDecodeError as exc:
            logger.error(
                f"web_search_agent API returned invalid JSON: {response_text[:1000]}"
            )
            raise RuntimeError("search api returned invalid JSON") from exc

        if not isinstance(payload_data, dict):
            logger.error(
                "web_search_agent API returned unexpected response type: "
                f"{type(payload_data).__name__}"
            )
            raise RuntimeError(
                f"search api returned unexpected response type: {type(payload_data).__name__}"
            )

        return payload_data

    @staticmethod
    def _pick_result_candidates(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []

        candidates: list[Any] = []
        for key in ("results", "items", "data", "list", "value", "documents"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                for nested_key in ("results", "items", "list", "value", "documents"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, list):
                        candidates.extend(nested_value)

        return [item for item in candidates if isinstance(item, dict)]

    def _extract_result_items(
        self, data: dict[str, Any], *, count: int = 10
    ) -> list[WebPageItem]:
        items: list[WebPageItem] = []
        for raw_item in self._pick_result_candidates(data):
            title = self._read_first_str(
                raw_item,
                "title",
            )
            url = self._read_first_str(
                raw_item,
                "url",
            )
            summary = self._read_first_str(
                raw_item,
                "summary",
            )
            if not title and not summary and not url:
                continue

            items.append(
                WebPageItem.model_validate(
                    {
                        "name": title,
                        "url": url,
                        "summary": summary,
                        "source_logo_url": self._read_first_str(
                            raw_item, "source_logo_url", "sourceLogoUrl"
                        )
                        or None,
                        "siteName": self._read_first_str(
                            raw_item, "source", "site", "site_name", "siteName"
                        ),
                        "datePublished": self._read_first_str(
                            raw_item,
                            "published_date",
                            "publishedDate",
                            "date_published",
                            "datePublished",
                            "publication_date",
                            "publicationDate",
                        ),
                    }
                )
            )
            if len(items) >= count:
                break

        return items

    @staticmethod
    def _read_first_str(raw_item: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = raw_item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _build_result_text(items: list[WebPageItem]) -> str:
        if not items:
            return ""

        lines: list[str] = []
        for idx, item in enumerate(items[:10], start=1):
            title = item.name.strip() if item.name else ""
            source = item.site_name.strip() if item.site_name else ""
            summary = item.summary.strip() if item.summary else ""
            item_lines = [f"{idx}. 标题：{title}"]
            if source:
                item_lines.append(f"   来源：{source}")
            item_lines.append(f"   摘要：{summary}")
            lines.append("\n".join(item_lines))
        return "\n".join(lines)

    @staticmethod
    def _build_summary_result_text(items: list[WebPageItem]) -> str:
        if not items:
            return ""

        lines: list[str] = []
        for idx, item in enumerate(items[:10], start=1):
            title = item.name.strip() if item.name else ""
            summary = item.summary.strip() if item.summary else ""
            date_published = (
                item.date_published.strip() if item.date_published else ""
            )
            item_lines = [f"{idx}. 标题：{title}", f"   摘要：{summary}"]
            if date_published:
                item_lines.append(f"   发布日期：{date_published}")
            lines.append("\n".join(item_lines))
        return "\n".join(lines)

    @staticmethod
    def _strip_url_lines(text: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("链接：", "链接:", "url：", "url:", "URL：", "URL:")):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    @staticmethod
    def _build_disassembly_summary_content(summaries: list[dict[str, str]]) -> str:
        sections: list[str] = []
        for index, entry in enumerate(summaries, start=1):
            sub_query = entry.get("query", "").strip()
            query_type = entry.get("query_type", "").strip()
            summary = entry.get("summary", "").strip()
            lines = [f"{index}. sub_query：{sub_query}"]
            if query_type:
                lines.append(f"   query_type：{query_type}")
            lines.append(f"   llm_summary：{summary}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections).strip()

    @staticmethod
    def _build_disassembly_result_content(items: list[dict[str, Any]]) -> str:
        sections: list[str] = []
        for query_index, item in enumerate(items, start=1):
            sub_query = str(item.get("query") or "").strip()
            query_type = str(item.get("query_type") or "").strip()
            header = f"{query_index}. 子问题：{sub_query}"
            if query_type:
                header = f"{header}\n   query_type：{query_type}"

            lines = [header]
            error = str(item.get("error") or "").strip()
            if error:
                lines.append(f"   error：{error}")
                sections.append("\n".join(lines))
                continue

            raw_items = item.get("items")
            web_items = raw_items if isinstance(raw_items, list) else []
            if not web_items:
                lines.append("   未提取到有效检索结果。")
                sections.append("\n".join(lines))
                continue

            for result_index, raw_web_item in enumerate(web_items[:10], start=1):
                if isinstance(raw_web_item, WebPageItem):
                    name = raw_web_item.name.strip()
                    source = (
                        raw_web_item.site_name.strip()
                        if raw_web_item.site_name
                        else ""
                    )
                    summary = raw_web_item.summary.strip()
                    date_published = (
                        raw_web_item.date_published.strip()
                        if raw_web_item.date_published
                        else ""
                    )
                elif isinstance(raw_web_item, dict):
                    name = str(
                        raw_web_item.get("name") or raw_web_item.get("title") or ""
                    ).strip()
                    source = str(
                        raw_web_item.get("siteName")
                        or raw_web_item.get("site_name")
                        or raw_web_item.get("source")
                        or raw_web_item.get("site")
                        or ""
                    ).strip()
                    summary = str(raw_web_item.get("summary") or "").strip()
                    date_published = str(
                        raw_web_item.get("datePublished")
                        or raw_web_item.get("date_published")
                        or raw_web_item.get("published_date")
                        or ""
                    ).strip()
                else:
                    continue

                lines.append(f"   {result_index}. 标题：{name}")
                if source:
                    lines.append(f"      来源：{source}")
                if summary:
                    lines.append(f"      摘要：{summary}")
                if date_published:
                    lines.append(f"      datePublished：{date_published}")

            sections.append("\n".join(lines))

        return "\n\n".join(section for section in sections if section).strip()

    @staticmethod
    def _read_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return default

    @staticmethod
    def _read_prompt(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    @staticmethod
    def _build_disassembly_json_schema(max_items: int) -> dict[str, Any]:
        schema = RoutedSearchPlan.model_json_schema()
        queries_schema = schema.get("properties", {}).get("queries")
        if isinstance(queries_schema, dict):
            queries_schema["maxItems"] = max_items
        return schema

    @staticmethod
    def _parse_disassembly_plan(text: str) -> list[RoutedSearchQuery]:
        if not isinstance(text, str):
            return []
        stripped = text.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        try:
            plan = RoutedSearchPlan.model_validate(payload)
        except Exception:
            return []
        return plan.queries

    async def disassemble_queries(
        self,
        query: str,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        max_items: int = 5,
    ) -> list[RoutedSearchQuery]:
        prompt = (
            self._read_prompt(user_prompt)
            or WebSearchToolContext.model_fields["disassembly_user_prompt"].default
        )
        prompt = f"{self._current_date_text()}\n\n{prompt.replace('{query}', query)}"

        response = await self.llm.asimple_chat(
            prompt=prompt,
            system_prompt=(
                self._read_prompt(system_prompt)
                or WebSearchToolContext.model_fields[
                    "disassembly_system_prompt"
                ].default
            ),
            json_schema=self._build_disassembly_json_schema(max_items),
            schema_name=self._disassembly_schema_name,
            schema_strict=True,
        )
        self._accumulate_usage(response.usage)
        raw_items = self._parse_disassembly_plan(response.content)
        cleaned: list[RoutedSearchQuery] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_items:
            value = item.query.strip()
            query_type = item.query_type.strip()
            key = (value, query_type)
            if not value or key in seen:
                continue
            seen.add(key)
            cleaned.append(RoutedSearchQuery(query=value, query_type=item.query_type))
            if len(cleaned) >= max_items:
                break
        return cleaned

    @staticmethod
    def _build_query_list(
        original_query: str, sub_queries: list[RoutedSearchQuery]
    ) -> list[RoutedSearchQuery]:
        queries = [item for item in sub_queries if item.query != original_query]
        if queries:
            return queries
        return [RoutedSearchQuery(query=original_query, query_type="general")]

    @staticmethod
    def _build_result_item(
        query: str,
        *,
        query_type: str,
        success: bool,
        items: list[WebPageItem],
        result_text: str,
        data: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "query_type": query_type,
            "success": success,
            "items": items,
            "result_text": result_text,
            "data": data,
            "error": error,
        }

    async def _run_queries(
        self,
        *,
        queries: list[RoutedSearchQuery],
        api_url: str,
        min_score: float,
        count: int,
        freshness: str,
        site: str | None,
        language: str | None,
        debug: bool = False,
        backend_env: str = "missing",
    ) -> list[dict[str, Any]]:
        tasks = [
            self._fetch_web_search(
                query.query,
                query_type=query.query_type,
                api_url=api_url,
                min_score=min_score,
                freshness=freshness,
                site=site,
                language=language,
            )
            for query in queries
        ]
        responses: list[dict[str, Any] | BaseException] = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        result_items: list[dict[str, Any]] = []
        for query, response in zip(queries, responses):
            query_text = query.query
            query_type = query.query_type
            if isinstance(response, BaseException):
                result_items.append(
                    self._build_result_item(
                        query_text,
                        query_type=query_type,
                        success=False,
                        items=[],
                        result_text="",
                        data={},
                        error=str(response),
                    )
                )
                continue

            if not isinstance(response, dict):
                result_items.append(
                    self._build_result_item(
                        query_text,
                        query_type=query_type,
                        success=False,
                        items=[],
                        result_text="",
                        data={},
                        error=f"unexpected response type: {type(response).__name__}",
                    )
                )
                continue

            if debug or backend_env == "EDITORIAL_STATE":
                logger.info(
                    "[TOOL] WebSearchAgent query result: "
                    f"query={query_text}, query_type={query_type}, "
                    f"response={json.dumps(response, ensure_ascii=False)}"
                )

            web_items = self._extract_result_items(response, count=count)
            result_text = self._build_result_text(web_items)
            success = bool(web_items)
            error = None
            if not success:
                if self._pick_result_candidates(response):
                    error = (
                        "search api returned candidates, but no supported title/url/summary "
                        "fields were extracted"
                    )
                else:
                    error = "search api returned no result candidates"
            result_items.append(
                self._build_result_item(
                    query_text,
                    query_type=query_type,
                    success=success,
                    items=web_items,
                    result_text=result_text,
                    data=response,
                    error=error,
                )
            )
        return result_items

    async def _summarize_single_result(
        self,
        *,
        query: str,
        result: str,
        summarize_prompt: str,
    ) -> str:
        result_text = result.strip()
        if not result_text:
            return "未获取到有效搜索结果。"
        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {
                        "role": "system",
                        "content": summarize_prompt,
                    },
                    {
                        "role": "user",
                        "content": (
                            f"{self._current_date_text()}\n\n"
                            f"子问题：{query}\n\n搜索结果：{result_text}\n\n"
                            "请总结关键要点，并指出不确定信息。"
                        ),
                    },
                ]
            )
        )
        outcome.raise_for_status()
        self._accumulate_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        return outcome.content.strip()

    async def _summarize_multi_result(
        self,
        *,
        original_query: str,
        items: list[dict[str, Any]],
        summarize_prompt: str,
    ) -> tuple[list[dict[str, str]], str]:
        summaries: list[dict[str, str]] = []
        for item in items:
            sub_query = str(item.get("query") or "").strip()
            query_type = str(item.get("query_type") or "").strip()
            if not sub_query:
                continue

            error = str(item.get("error") or "").strip()
            if error:
                summary = f"查询失败：{error}"
            else:
                raw_items = item.get("items")
                summary_items = (
                    [
                        raw_item
                        for raw_item in raw_items
                        if isinstance(raw_item, WebPageItem)
                    ]
                    if isinstance(raw_items, list)
                    else []
                )
                summary_input = (
                    self._build_summary_result_text(summary_items)
                    if summary_items
                    else self._strip_url_lines(str(item.get("result_text") or ""))
                )
                summary = await self._summarize_single_result(
                    query=sub_query,
                    result=summary_input,
                    summarize_prompt=summarize_prompt,
                )

            item["summary"] = summary
            summaries.append(
                {
                    "query": sub_query,
                    "query_type": query_type,
                    "summary": summary,
                }
            )

        if not summaries:
            return [], ""

        return summaries, self._build_disassembly_summary_content(summaries)

    async def _summarize_result(
        self,
        result: str,
        query: str,
        *,
        summarize_prompt: str,
    ) -> str:
        if not result.strip():
            return ""

        user_content = (
            f"{self._current_date_text()}\n\n"
            f"用户问题：{query}\n\n搜索结果：{result}\n\n"
            "请用不超过6条要点输出，必要时点明信息不确定性。"
        )
        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {"role": "system", "content": summarize_prompt},
                    {"role": "user", "content": user_content},
                ]
            )
        )
        outcome.raise_for_status()
        self._accumulate_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        return outcome.content.strip()

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(f"[TOOL] WebSearchAgent run started, query={request.query}")
            tool_context = self._resolve_tool_context(request)
            count = max(1, min(int(self.default_result_count), 50))
            raw_freshness = self.default_freshness
            freshness = self._normalize_freshness(raw_freshness)
            if freshness is None:
                logger.warning(
                    "[TOOL] WebSearchAgent unsupported freshness="
                    f"{raw_freshness}, fallback to noLimit"
                )
                freshness = "noLimit"
            site = self._normalize_site(self.default_site)
            language = self._normalize_language(self.default_language)
            api_url = self._resolve_api_url(self.default_api_url)
            request_extra = request.extra or {}
            min_score = self._resolve_min_score(request_extra.get("min_score"))
            debug = tool_context.debug
            summarize_prompt = self._read_prompt(tool_context.summarize_prompt) or (
                WebSearchToolContext.model_fields["summarize_prompt"].default
            )
            enable_query_disassembly = self._read_bool(
                tool_context.enable_query_disassembly, default=False
            )
            enable_disassembly_summary = self._read_bool(
                tool_context.enable_disassembly_summary, default=False
            )

            try:
                if enable_query_disassembly:
                    sub_queries = await self.disassemble_queries(
                        request.query,
                        system_prompt=self._read_prompt(
                            tool_context.disassembly_system_prompt
                        ),
                        user_prompt=self._read_prompt(
                            tool_context.disassembly_user_prompt
                        ),
                    )
                    all_queries = self._build_query_list(request.query, sub_queries)
                    if debug:
                        logger.info(
                            "[TOOL] WebSearchAgent decomposed queries: "
                            f"{json.dumps([item.model_dump() for item in all_queries], ensure_ascii=False)}"
                        )
                    query_items = await self._run_queries(
                        queries=all_queries,
                        api_url=api_url,
                        min_score=min_score,
                        count=count,
                        freshness=freshness,
                        site=site,
                        language=language,
                        debug=debug,
                        backend_env=request_extra.get("backend_env", "missing"),
                    )
                    summaries: list[dict[str, str]] = []
                    final_summary = ""
                    if enable_disassembly_summary:
                        summaries, final_summary = await self._summarize_multi_result(
                            original_query=request.query,
                            items=query_items,
                            summarize_prompt=summarize_prompt,
                        )
                    else:
                        final_summary = self._build_disassembly_result_content(
                            query_items
                        )
                    success = any(item.get("success") for item in query_items)
                    error = None
                    if not success:
                        errors = [
                            str(item.get("error"))
                            for item in query_items
                            if item.get("error")
                        ]
                        error = errors[0] if errors else "all queries failed"

                    logger.info(
                        "[TOOL] WebSearchAgent disassembly flow completed successfully"
                    )
                    return AgentResult(
                        success=success,
                        name=self.name,
                        content=final_summary,
                        data_source={
                            "source": "multi_source_web_search",
                            "count": sum(
                                len(item.get("items", [])) for item in query_items
                            ),
                            "freshness": freshness,
                            "site": site,
                            "language": language,
                            "api_url": api_url,
                            "min_score": min_score,
                            "data": query_items,
                        },
                        meta_data={
                            "decomposed_queries": [
                                item.model_dump() for item in sub_queries
                            ],
                            "all_queries": [item.model_dump() for item in all_queries],
                            "summaries": summaries,
                        },
                        error=error,
                    )

                data = await self._fetch_web_search(
                    request.query,
                    query_type="general",
                    api_url=api_url,
                    min_score=min_score,
                    freshness=freshness,
                    site=site,
                    language=language,
                )
                result_items = self._extract_result_items(data, count=count)
                result_text = self._build_result_text(result_items)
                content = result_text
                if self.default_summarize:
                    logger.debug("[TOOL] WebSearchAgent summarizing result")
                    content = await self._summarize_result(
                        self._build_summary_result_text(result_items),
                        request.query,
                        summarize_prompt=summarize_prompt,
                    )
                logger.info("[TOOL] WebSearchAgent run completed successfully")
                return AgentResult(
                    name=self.name,
                    content=content,
                    data_source={
                        "source": "multi_source_web_search",
                        "count": len(result_items),
                        "freshness": freshness,
                        "site": site,
                        "language": language,
                        "api_url": api_url,
                        "min_score": min_score,
                        "query_type": "general",
                        "data": data,
                    },
                )
            except Exception as exc:
                logger.error(f"[TOOL] WebSearchAgent run failed, error={exc}")
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )
