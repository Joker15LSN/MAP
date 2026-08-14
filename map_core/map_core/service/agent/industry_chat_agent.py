from __future__ import annotations

import json
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ...utils.sensitive_data import redact_mapping, redact_text
from .base import AgentRequest, AgentResult
from .traceable_agent import TraceableAgent

CAPABILITY_CONFIG_MISSING = "CAPABILITY_CONFIG_MISSING"


class IndustryChatQueryParams(BaseModel):
    query: str = Field(..., description="行业问答问题")


class IndustryChatAgent(TraceableAgent):
    name = "industry_chat_agent"
    description = "调用行业问答接口并直接返回结果"
    # P0-SEC-01: endpoint and credential come exclusively from environment;
    # unset values fail closed at request time (httpx refuses empty URLs).
    # R-07: endpoint and credential come exclusively from environment.
    # run() refuses to make any network call when either is missing
    # (CAPABILITY_CONFIG_MISSING) - no empty-token requests ever leave.
    _api_url = (os.getenv("MAP_INDUSTRY_CHAT_URL") or "").strip()
    _api_key = (os.getenv("MAP_INDUSTRY_CHAT_API_KEY") or "").strip()
    _service_request_id = "test"
    _model = "qwen2"
    _deep_think = False
    _using_network = True
    _stream = False
    _top_k = 5
    timeout = 60.0

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": IndustryChatQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, name=self.tool_name, **kwargs)

    def _build_payload(self, query: str) -> dict[str, Any]:
        return {
            "api_key": self._api_key,
            "messages": [{"role": "user", "content": query}],
            "service_request_id": self._service_request_id,
            "stream": self._stream,
            "model": self._model,
            "deep_think": self._deep_think,
            "using_network": self._using_network,
            "top_k": self._top_k,
        }

    def _format_exception_detail(self, exc: Exception) -> str:
        # R-07: every fragment is redacted - the upstream may echo the
        # request (including api_key) back in the error body.
        detail = redact_text(str(exc).strip() or repr(exc))

        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            status = response.status_code if response is not None else "unknown"
            body = redact_text(response.text) if response is not None else ""
            return f"{type(exc).__name__}: {detail}; status={status}; body={body}"

        if isinstance(exc, httpx.RequestError):
            request = exc.request
            method = request.method if request is not None else "unknown"
            url = redact_text(str(request.url)) if request is not None else "unknown"
            return f"{type(exc).__name__}: {detail}; request={method} {url}"

        return f"{type(exc).__name__}: {detail}"

    @staticmethod
    def _extract_content(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in (
                "content",
                "answer",
                "response",
                "result",
                "output",
                "text",
                "message",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return json.dumps(payload, ensure_ascii=False)

        if isinstance(payload, list):
            return json.dumps(payload, ensure_ascii=False)

        if isinstance(payload, str):
            return payload

        return json.dumps(payload, ensure_ascii=False)

    async def _call_industry_chat(self, query: str) -> dict[str, Any]:
        payload = self._build_payload(query)
        async with httpx.AsyncClient(timeout=(self.timeout or 60) * 0.8) as client:
            response = await client.post(self._api_url, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("industry_chat_agent response must be a JSON object")
            return data

    def _safe_context(self, query: str) -> dict[str, Any]:
        """Redacted context for results, logs and traces (R-07).

        Never carries the api_key: the request is reduced to the query, the
        api_url is userinfo-redacted, and any response goes through
        redact_mapping before being recorded.
        """
        return {
            "source": "industry_chat",
            "api_url": redact_text(self._api_url),
            "request": {"query": query},
        }

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        query = request.query.strip()
        payload = self._build_payload(query)
        self.record_tool_call(self.tool_name, {"query": query})

        # R-07: fail closed BEFORE the first network call when the
        # capability config is incomplete - never send an empty token/URL.
        if not self._api_url or not self._api_key:
            error = (
                f"{CAPABILITY_CONFIG_MISSING}: MAP_INDUSTRY_CHAT_URL and "
                "MAP_INDUSTRY_CHAT_API_KEY are required (capability unavailable)"
            )
            self.record_tool_result(
                self.tool_name,
                {"success": False, "error": error},
            )
            return AgentResult(
                success=False,
                name=self.name,
                content="",
                error=error,
                data_source=self._safe_context(query),
                meta_data={},
            )

        try:
            response_data = await self._call_industry_chat(query)
            content = self._extract_content(response_data)
            # R-07: the recorded response must never leak the api_key even
            # when the upstream echoes the request back.
            self.record_tool_result(
                self.tool_name, redact_mapping(response_data)
            )
            self.record_message("assistant", content)
            return AgentResult(
                success=True,
                name=self.name,
                content=content,
                data_source={
                    **self._safe_context(query),
                    "response": redact_mapping(response_data),
                },
                meta_data={},
            )
        except Exception as exc:
            error = self._format_exception_detail(exc)
            self.record_tool_result(
                self.tool_name,
                {"success": False, "error": error},
            )
            return AgentResult(
                success=False,
                name=self.name,
                content="",
                error=error,
                data_source=self._safe_context(query),
                meta_data={},
            )
