from __future__ import annotations

import inspect
import secrets
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Awaitable, Literal, Sequence, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ...schema.agent_schema import Message
from ...schema.scene_classification_schema import SceneClassificationResult
from ...utils.llm_engine import LLMEngine
from ...utils.term_replacer import replace_request_query_for_agent


class ExecutionResult(BaseModel):
    success: bool = True
    model_config = ConfigDict(extra="allow")
    content: str = ""
    error: str | None = None
    exit: dict[str, Any] | None = None
    data_source: dict[str, Any] = Field(default_factory=dict)
    tool_observations: list[dict[str, Any]] | None = None
    meta_data: dict[str, Any] = Field(default_factory=dict)
    extra_result: dict[str, Any] | None = None


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    query: str
    original_query: str | None = None
    staff_code: str
    history: Sequence[Message | dict[str, Any]] | None = None
    scene_result: SceneClassificationResult | None = None
    dispatch_results: Sequence[AgentResult] | None = None
    summarize: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentResult(ExecutionResult):
    name: str


class ToolResult(ExecutionResult):
    name: str | None = None


class AgentActionEvent(BaseModel):
    type: Literal["agent_action"] = "agent_action"
    agent_code: str
    agent_name: str | None = None
    step: int | None = None
    action: str
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BaseAgent(ABC):
    name: str
    description: str = ""
    supported_big_scenes: set[str] | None = None
    supported_sub_scenes: set[str] | None = None
    timeout: float | None = None

    def __init__(
        self, llm: LLMEngine, agent_id: str | None = None, name: str = "BaseAgent"
    ) -> None:
        self.llm = llm
        self.agent_id = agent_id or secrets.token_hex(10)
        self.name = name
        self.agent_display_name = name
        self._token_usage: dict[str, int] = {}

    def _accumulate_usage(self, usage: dict[str, int] | None) -> None:
        if not usage:
            return
        for k, v in usage.items():
            if isinstance(v, int):
                self._token_usage[k] = self._token_usage.get(k, 0) + v

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    async def preprocess_request(
        self, request: AgentRequest, *, parid: str = "-"
    ) -> AgentRequest:
        """Hook for request normalization or query rewriting before execution."""
        return cast(
            AgentRequest,
            replace_request_query_for_agent(request, agent_code=self.name),
        )

    async def execute(
        self, request: AgentRequest, *, parid: str = "-", **run_kwargs: Any
    ) -> AgentResult | AsyncGenerator[str, None] | Any:
        prepared = self.preprocess_request(request, parid=parid)
        if inspect.isawaitable(prepared):
            request = await prepared
        else:
            request = prepared
        return await self.run(request, parid=parid, **run_kwargs)

    def supports(self, scene_result: SceneClassificationResult | None) -> bool:
        if scene_result is None:
            return (
                self.supported_big_scenes is None and self.supported_sub_scenes is None
            )

        if not self.supported_big_scenes and not self.supported_sub_scenes:
            return True

        if self.supported_big_scenes:
            for item in scene_result.big_scenes:
                if item.big_scene in self.supported_big_scenes:
                    return True

        if self.supported_sub_scenes:
            for sub in scene_result.sub_scenes:
                if any(ss in self.supported_sub_scenes for ss in sub.sub_scenes):
                    return True

        return False

    @abstractmethod
    def run(
        self, request: AgentRequest, *, parid: str = "-"
    ) -> Awaitable[AgentResult | AsyncGenerator[str, None] | Any]:
        # AgentResult | AsyncGenerator[str, None] | Any
        raise NotImplementedError
