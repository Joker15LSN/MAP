from __future__ import annotations

"""AnnualPerformanceAgent.

tool_context 契约说明：

- 本 agent tool 当前不消费 `request.extra["tool_context"]` 中的任何字段。
- 实际请求参数仅来自 `AgentRequest.staff_code`；工具调用方需要通过
  `annual_performance_agent` 的 tool args 传入 `staff_code`，而不是放在
  `tool_context` 中。
- 因此本文件没有定义专属的 tool_context schema；若未来需要引入额外上下文，
  必须先在文件头补充字段定义、作用说明、必填性和兼容策略，再接入运行逻辑。
"""

import json
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from ...utils.global_context import agent_log_context
from ...utils.model_invocation import ModelInvocationRequest
from .base import AgentRequest, AgentResult
from .traceable_agent import TraceableAgent


class AnnualPerformanceQueryParams(BaseModel):
    staff_code: str = Field(..., description="员工工号")
    # summarize: bool = Field(default=False, description="是否让工具生成摘要")


class AnnualPerformanceAgent(TraceableAgent):
    name = "annual_performance_agent"
    description = "查询员工年度绩效报告"
    _fixed_query = "查询年度绩效报告"
    _api_url = "http://10.50.56.46:9977/bpm/query/stream"
    timeout = 10.0

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": AnnualPerformanceQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, **kwargs)
        self.name = "annual_performance_agent"
        self.description = "查询员工年度绩效报告"

    async def _fetch_external_report(
        self, query: str, staff_code: str | None = None
    ) -> dict[str, Any]:
        payload = {
            "queryList": [
                {
                    "context": query,
                    "type": "text",
                }
            ],
            "history": [],
            "specMap": {"annual_report_raw": True},
        }
        async with httpx.AsyncClient(timeout=(self.timeout or 60) * 0.8) as client:
            try:
                response = await client.post(
                    self._api_url,
                    json=payload,
                    headers={
                        "content-type": "application/json",
                        "staffcode": staff_code or "",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                body = exc.response.text if exc.response else ""
                logger.exception(
                    "annual_performance_agent API call failed with HTTP status "
                    f"{status}. Body: {body}"
                )
                raise
            except httpx.RequestError:
                logger.exception(
                    "annual_performance_agent API call failed (request error)"
                )
                raise

        try:
            return response.json()
        except ValueError:
            text = response.text or ""
            logger.warning(
                "annual_performance_agent API returned non-JSON response, "
                "attempting to parse stream event content"
            )
            return self._parse_stream_event_text(text)

    def _parse_stream_event_text(self, text: str) -> dict[str, Any]:
        if not text:
            return {"content_md": ""}

        data_chunks: list[str] = []
        json_payloads: list[Any] = []

        current_data_lines: list[str] = []

        def _flush_event() -> None:
            if not current_data_lines:
                return
            payload = "\n".join(current_data_lines).strip()
            current_data_lines.clear()
            if not payload or payload == "[DONE]":
                return
            data_chunks.append(payload)
            try:
                json_payloads.append(json.loads(payload))
            except json.JSONDecodeError:
                return

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                _flush_event()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                current_data_lines.append(line[5:].lstrip())
                continue

        _flush_event()

        if json_payloads:
            content_parts: list[str] = []
            for payload in json_payloads:
                if isinstance(payload, dict):
                    for key in ("content_md", "content", "result", "text", "message"):
                        value = payload.get(key)
                        if isinstance(value, str) and value.strip():
                            content_parts.append(value)
                            break
            if content_parts:
                return {
                    "content_md": "".join(content_parts),
                    "stream_events": json_payloads,
                }
            return {
                "content_md": "\n".join(data_chunks),
                "stream_events": json_payloads,
            }

        if data_chunks:
            return {
                "content_md": "\n".join(data_chunks),
                "stream_events_raw": data_chunks,
            }

        return {"content_md": text}

    def _extract_result(self, data: dict[str, Any]) -> str:
        content = data.get("content_md") if isinstance(data, dict) else ""
        return (content or "").strip()

    async def _summarize_result(self, result: str, query: str) -> str:
        if not result.strip():
            return ""
        prompt = (
            "你是绩效分析助手。请基于年度绩效报告生成简明摘要，"
            "突出关键结论与时间范围，并与用户问题相关。"
        )
        user_content = (
            f"用户问题：{query}\n\n年度绩效报告：{result}\n\n"
            "请用不超过6条要点输出。"
        )
        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {"role": "system", "content": prompt},
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
            logger.info(
                "[TOOL] AnnualPerformanceAgent run started, "
                f"query={request.query}"
            )
            staff_code = request.staff_code
            query = self._fixed_query
            try:
                data = await self._fetch_external_report(
                    query, staff_code=staff_code
                )
                result = self._extract_result(data)

                content = ""
                if request.summarize:
                    logger.info("[TOOL] AnnualPerformanceAgent summarizing result")
                    content = await self._summarize_result(result, query)

                logger.info("[TOOL] AnnualPerformanceAgent run completed successfully")
                return AgentResult(
                    name=self.name,
                    content=content,
                    data_source={"source": "annual_performance_api", "data": data},
                )
            except Exception as exc:
                logger.error(
                    "[TOOL] AnnualPerformanceAgent run failed, "
                    f"staff_code={request.staff_code}, error={exc}"
                )
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )
