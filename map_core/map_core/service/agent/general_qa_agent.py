from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...utils.model_invocation import ModelInvocationRequest
from .base import AgentRequest, AgentResult
from .traceable_agent import TraceableAgent


class GeneralQAQueryParams(BaseModel):
    query: str = Field(..., description="通用问答问题")


class GeneralQAAgent(TraceableAgent):
    name = "general_qa_agent"
    description = "通用问答工具，直接基于大模型回答用户问题"

    tool_name = name
    tool_description = description

    @classmethod
    def get_tool_spec(cls) -> dict[str, Any]:
        return {
            "name": cls.tool_name,
            "description": cls.tool_description,
            "parameters": GeneralQAQueryParams.model_json_schema(),
        }

    def __init__(self, llm, **kwargs):
        super().__init__(llm, name=self.tool_name, **kwargs)

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        query = (request.query or "").strip()
        self.record_tool_call(self.tool_name, {"query": query})

        system_prompt = (
            "你是企业通用问答助手。回答必须准确、简洁、中文输出。"
            "如果问题要求介绍城市/地区，请从定位、特色、经济文化、旅游建议等方面分点说明。"
        )

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ]
            outcome = await self.llm.invoke(
                ModelInvocationRequest(
                    messages=messages,
                    max_tokens=2048,
                )
            )
            outcome.raise_for_status()
            self._accumulate_usage(
                outcome.usage.to_dict() if outcome.usage else None
            )
            content = (outcome.content or "").strip()
            if not content:
                content = "我暂时没有生成有效回答，请换一种问法试试。"

            self.record_tool_result(
                self.tool_name,
                {
                    "success": True,
                    "content": content,
                    "model": outcome.model,
                    "usage": outcome.usage.to_dict() if outcome.usage else None,
                },
            )
            self.record_message("assistant", content)
            return AgentResult(
                success=True,
                name=self.name,
                content=content,
                data_source={
                    "source": "general_qa_agent",
                    "model": outcome.model,
                    "request_id": outcome.request_id,
                },
                meta_data={
                    "model": outcome.model,
                    "usage": outcome.usage.to_dict() if outcome.usage else {},
                    "request_id": outcome.request_id,
                },
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.record_tool_result(
                self.tool_name,
                {
                    "success": False,
                    "error": error,
                },
            )
            return AgentResult(
                success=False,
                name=self.name,
                content="",
                error=error,
                data_source={
                    "source": "general_qa_agent",
                },
            )
