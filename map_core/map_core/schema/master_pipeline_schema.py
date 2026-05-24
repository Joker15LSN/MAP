from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..config.config_schema import LLMConfig
from ..service.agent.base import AgentResult
from ..service.agent.tool_registry import (
    find_invalid_tool_names,
    list_registered_tool_names,
)
from .agent_schema import Message
from .attachment_schema import AttachmentSchema, UploadedKBFileSchema
from .rerank_model_schema import (
    RerankModelConfigSchema,
    create_default_rerank_model_conf,
)
from .tool_extra_result_schema import ToolExtraResultSchema


class MasterAgentConfigSchema(BaseModel):
    prompt: str = (
        "你是主控智能助手。你负责理解用户问题，选择合适的工具完成检索与分析。"
        "优先调用最相关的工具；必要时可连续调用多个工具交叉验证。"
        "最终直接给出结论，并明确说明信息来源不足或工具返回为空的情况。"
    )
    additional_user_prompt: str = ""
    tool_names: list[str] = Field(
        default_factory=lambda: ["web_search_agent", "zhiwen_agent"]
    )
    max_steps: int = 4
    force_tool_call: bool = True
    llm_config: LLMConfig | None = None

    @field_validator("tool_names")
    @classmethod
    def validate_tool_names(cls, value: list[str]) -> list[str]:
        invalid_tool_names = find_invalid_tool_names(value)
        if not invalid_tool_names:
            return value

        allowed = ", ".join(list_registered_tool_names())
        invalid_values = ", ".join(invalid_tool_names)
        raise ValueError(
            "master agent config contains unknown tool_names: "
            f"{invalid_values}. Allowed values: {allowed}"
        )


class MasterAgentChatSchema(BaseModel):
    query: str
    original_query: str | None = None
    staff_code: str = "missing"
    backend_env: str = "EDITORIAL_STATE"
    backend_env_base_url: str = "missing"
    attachments: list[AttachmentSchema] | None = None
    uploaded_kb_files: list[UploadedKBFileSchema] | None = None
    rerank_model_config: RerankModelConfigSchema = Field(
        default_factory=create_default_rerank_model_conf
    )
    history: Sequence[Message | dict[str, Any]] | None = None
    tool_context: dict[str, Any] | None = None
    master_config: MasterAgentConfigSchema | None = None


class MasterPipelineChatResponse(BaseModel):
    content: str
    result: AgentResult
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class MasterPipelineStreamEvent(BaseModel):
    event: Literal["start", "meta", "content_delta", "done", "error"]
    data: dict[str, Any]
