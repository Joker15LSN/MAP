from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, cast
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, Field

from .. import config as app_config
from ..config.config_schema import LLMConfig
from ..schema.scene_agent_config_schema import ScenePostSummaryConfig
from ..utils.llm_engine import LLMEngine
from .agent.base import AgentActionEvent, AgentRequest, AgentResult
from .agent.tool_call_agent import (
    SCENE_POST_SUMMARY_SYSTEM_PROMPT,
    SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE,
    ScenePostSummaryRuntimeConfig,
    Tool,
    ToolCallAgent,
    ToolSet,
)
from .agent_memory_store import AgentMemoryStore
from .state_store import safe_serialize


class AgentExecutionSpec(BaseModel):
    name: str
    system_prompt: str
    additional_user_prompt: str = ""
    tool_names: list[str] = Field(default_factory=list)
    max_steps: int = 6
    force_tool_call: bool = False
    llm_config: LLMConfig | None = None
    scene_post_summary: ScenePostSummaryConfig | None = None
    agent_name: str | None = None


@dataclass
class AgentExecutionHooks:
    on_agent_start: (
        Callable[[ToolCallAgent, AgentRequest], Any | Awaitable[Any]] | None
    ) = None
    on_agent_end: (
        Callable[[ToolCallAgent, str, dict[str, Any]], Any | Awaitable[Any]] | None
    ) = None


class _ProducerDone:
    """Queue sentinel used to mark one producer task as finished."""


class AgentRuntime:
    DEFAULT_TIMEZONE = ZoneInfo("Asia/Shanghai")

    def __init__(
        self,
        *,
        llm: LLMEngine,
        tool_registry: dict[str, Tool] | None = None,
        agent_memory_store: Any | None = None,
        logger_: Any | None = None,
    ) -> None:
        self.llm = llm
        self.tool_registry: dict[str, Tool] = tool_registry or {}
        self.agent_memory_store = (
            agent_memory_store
            if agent_memory_store is not None
            else AgentMemoryStore(logger_=logger_)
        )
        self.state_store: Any | None = None
        self.state_id: str | None = None
        self._logger = logger_ or logger

    def set_execution_context(self, state_store: Any, state_id: str) -> None:
        self.state_store = state_store
        self.state_id = state_id

    def _now(self) -> datetime:
        return datetime.now(self.DEFAULT_TIMEZONE)

    async def _invoke_hook(self, hook: Callable[..., Any] | None, *args: Any) -> None:
        if hook is None:
            return
        emitted = hook(*args)
        if inspect.isawaitable(emitted):
            await emitted

    @staticmethod
    def attach_agent_identity(
        result: AgentResult,
        agent: ToolCallAgent,
    ) -> AgentResult:
        meta_data = dict(result.meta_data or {})
        meta_data.setdefault("agent_code", agent.name)
        meta_data.setdefault("agent_name", agent.agent_display_name)
        return result.model_copy(update={"meta_data": meta_data})

    @staticmethod
    def build_agent_output_summary(result: Any) -> dict[str, Any]:
        if isinstance(result, AgentResult):
            return {
                "type": "AgentResult",
                "name": result.name,
                "success": result.success,
                "data_source": safe_serialize(result.data_source),
                "error": result.error,
            }
        if result is None:
            return {"type": "none"}
        return {"type": "raw", "value": safe_serialize(result)}

    def build_agent(self, spec: AgentExecutionSpec) -> ToolCallAgent:
        tools = [
            self.tool_registry[tool_name]
            for tool_name in spec.tool_names
            if tool_name in self.tool_registry
        ]
        if not tools:
            self._logger.warning(
                "Agent '{}' has no available tools in registry",
                spec.name,
            )
        toolset = ToolSet(tools, include_terminate=False)
        agent = ToolCallAgent(
            llm=self._build_agent_llm(spec),
            name=spec.name,
            system_prompt=spec.system_prompt,
            additional_user_prompt=spec.additional_user_prompt,
            toolset=toolset,
            max_steps=spec.max_steps,
            force_tool_call=spec.force_tool_call,
            scene_post_summary=self._build_scene_post_summary(spec),
        )
        if self.state_store and self.state_id:
            agent.set_execution_context(self.state_store, self.state_id)
        if isinstance(spec.agent_name, str) and spec.agent_name.strip():
            agent.agent_display_name = spec.agent_name.strip()
        return agent

    def _build_agent_llm(self, spec: AgentExecutionSpec) -> LLMEngine:
        if spec.llm_config is None:
            return self.llm

        try:
            return LLMEngine(config=spec.llm_config)
        except Exception as exc:
            self._logger.warning(
                "Failed to build custom LLMEngine for agent '{}' from llm_config: {}. Falling back to runtime llm.",
                spec.name,
                exc,
            )
            return self.llm

    def _build_scene_post_summary(
        self,
        spec: AgentExecutionSpec,
    ) -> ScenePostSummaryRuntimeConfig | None:
        config = spec.scene_post_summary
        if config is None or not config.enabled:
            return None

        llm = self._build_scene_post_summary_llm(spec, config)
        llm_model = getattr(getattr(llm, "config", None), "model", None)
        self._logger.info(
            "Agent '{}' enabled scene post-summary: {{'llm_model': {!r}, 'custom_llm': {}, 'custom_system_prompt': {}, 'custom_user_prompt_template': {}}}".format(
                spec.name,
                llm_model,
                config.llm_config is not None,
                config.system_prompt is not None,
                config.user_prompt_template is not None,
            )
        )
        return ScenePostSummaryRuntimeConfig(
            llm=llm,
            system_prompt=(
                config.system_prompt
                if config.system_prompt is not None
                else SCENE_POST_SUMMARY_SYSTEM_PROMPT
            ),
            user_prompt_template=(
                config.user_prompt_template
                if config.user_prompt_template is not None
                else SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE
            ),
        )

    def _build_scene_post_summary_llm(
        self,
        spec: AgentExecutionSpec,
        config: ScenePostSummaryConfig,
    ) -> LLMEngine:
        if config.llm_config is not None:
            return LLMEngine(config=config.llm_config)
        if spec.llm_config is not None:
            return self._build_agent_llm(spec)
        return self.llm

    def _agent_memory_enabled(self, agent_name: str) -> bool:
        enabled_agent_codes = getattr(
            app_config,
            "AGENT_MEMORY_ENABLED_AGENT_CODES",
            set(),
        )
        return agent_name in set(enabled_agent_codes or [])

    def _resolve_agent_memory_context(
        self,
        agent: ToolCallAgent,
        request: AgentRequest,
    ) -> tuple[Any, Any, str] | None:
        if not self._agent_memory_enabled(agent.name):
            return None
        if self.agent_memory_store is None:
            return None

        extra = request.extra or {}
        session_id = extra.get("session_id")
        intention_id = (
            extra.get("intention_id")
            or getattr(app_config, "AGENT_MEMORY_DEFAULT_INTENTION_ID", "default")
        )
        if not session_id:
            return None
        return session_id, intention_id, agent.name

    async def _build_execution_request(
        self,
        agent: ToolCallAgent,
        request: AgentRequest,
    ) -> AgentRequest:
        execution_request = request.model_copy(update={"history": None})
        memory_context = self._resolve_agent_memory_context(agent, request)
        if memory_context is None:
            return execution_request
        session_id, intention_id, agent_code = memory_context

        try:
            history = await asyncio.wait_for(
                self.agent_memory_store.get_history(
                    session_id=session_id,
                    intention_id=intention_id,
                    agent_code=agent_code,
                    max_messages=getattr(app_config, "AGENT_MEMORY_MAX_MESSAGES", 20),
                ),
                timeout=getattr(app_config, "AGENT_MEMORY_LOOKUP_TIMEOUT_S", 1.0),
            )
        except Exception as exc:
            self._logger.warning(
                "Agent memory lookup failed for agent '{}': {}",
                agent.name,
                exc,
            )
            return execution_request

        if not history:
            return execution_request
        self._logger.info(
            "Agent '{}' loaded {} memory messages for session_id={!r}, intention_id={!r}",
            agent_code,
            len(history),
            session_id,
            intention_id,
        )
        return execution_request.model_copy(update={"history": history})

    async def _record_agent_memory(
        self,
        agent: ToolCallAgent,
        request: AgentRequest,
        result: AgentResult,
    ) -> None:
        memory_context = self._resolve_agent_memory_context(agent, request)
        if memory_context is None:
            return
        session_id, intention_id, agent_code = memory_context

        data_source = result.data_source if isinstance(result.data_source, dict) else {}
        history = data_source.get("history")
        if not isinstance(history, list) or not history:
            return

        try:
            await asyncio.wait_for(
                self.agent_memory_store.upsert_history(
                    session_id=session_id,
                    intention_id=intention_id,
                    agent_code=agent_code,
                    history=history,
                ),
                timeout=getattr(app_config, "AGENT_MEMORY_RECORD_TIMEOUT_S", 1.0),
            )
        except Exception as exc:
            self._logger.warning(
                "Agent memory record failed for agent '{}': {}",
                agent_code,
                exc,
            )

    async def run_agent(
        self,
        agent: ToolCallAgent,
        request: AgentRequest,
        *,
        action_handler: Callable[[AgentActionEvent], Awaitable[None] | None]
        | None = None,
        hooks: AgentExecutionHooks | None = None,
    ) -> AgentResult:
        start_ts = self._now()
        end_status: str | None = None
        end_data: dict[str, Any] | None = None
        previous_action_handler = getattr(agent, "action_handler", None)
        agent.set_action_handler(action_handler)
        execution_request = await self._build_execution_request(agent, request)
        try:
            await self._invoke_hook(
                getattr(hooks, "on_agent_start", None), agent, request
            )
            result = await agent.execute(execution_request)
            if isinstance(result, AgentResult):
                final_result = result
            elif hasattr(result, "__aiter__"):
                chunks: list[str] = []
                async for chunk in cast(AsyncGenerator[Any, None], result):
                    chunks.append(str(chunk))
                final_result = AgentResult(
                    name=agent.name,
                    content="".join(chunks),
                    data_source={"source": "stream"},
                )
            else:
                final_result = AgentResult(
                    name=agent.name,
                    content="" if result is None else str(result),
                    data_source={"source": "raw"},
                )
            final_result = self.attach_agent_identity(final_result, agent)
            end_status = "success"
            end_ts = self._now()
            duration_s = (end_ts - start_ts).total_seconds()
            token_usage = agent.token_usage
            final_result.meta_data["duration_s"] = duration_s
            if token_usage:
                final_result.meta_data["token_usage"] = token_usage
            await self._record_agent_memory(agent, request, final_result)
            end_data = {
                "agent_id": agent.agent_id,
                "output": self.build_agent_output_summary(final_result),
                "meta": {
                    "duration_s": duration_s,
                    "token_usage": token_usage,
                },
            }
            return final_result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._logger.warning(
                "Agent {} timed out after {}", agent.name, agent.timeout
            )
            end_status = "failed"
            end_ts = self._now()
            end_data = {
                "agent_id": agent.agent_id,
                "error": "timeout",
                "error_type": "TimeoutError",
                "meta": {
                    "duration_s": (end_ts - start_ts).total_seconds(),
                },
            }
            return self.attach_agent_identity(
                AgentResult(
                    success=False,
                    name=agent.name,
                    content="",
                    error="timeout",
                    data_source={
                        "source": "agent_execution_error",
                        "error_type": "TimeoutError",
                    },
                    meta_data=end_data["meta"],
                ),
                agent,
            )
        except Exception as exc:
            self._logger.exception("Agent {} failed, exception: {}", agent.name, exc)
            end_status = "failed"
            end_ts = self._now()
            end_data = {
                "agent_id": agent.agent_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "meta": {
                    "duration_s": (end_ts - start_ts).total_seconds(),
                },
            }
            return self.attach_agent_identity(
                AgentResult(
                    success=False,
                    name=agent.name,
                    content="",
                    error=str(exc),
                    data_source={
                        "source": "agent_execution_error",
                        "error_type": type(exc).__name__,
                    },
                    meta_data=end_data["meta"],
                ),
                agent,
            )
        finally:
            agent.set_action_handler(previous_action_handler)
            if end_status and end_data is not None:
                await self._invoke_hook(
                    getattr(hooks, "on_agent_end", None),
                    agent,
                    end_status,
                    end_data,
                )

    async def run_stream(
        self,
        agents: list[ToolCallAgent],
        request: AgentRequest,
        *,
        hooks: AgentExecutionHooks | None = None,
    ) -> AsyncGenerator[AgentResult | AgentActionEvent, None]:
        queue: asyncio.Queue[AgentResult | AgentActionEvent | _ProducerDone] = (
            asyncio.Queue()
        )
        producer_done = _ProducerDone()

        async def _publish_action(event: AgentActionEvent) -> None:
            await queue.put(event)

        async def _run_and_publish(agent: ToolCallAgent) -> None:
            try:
                result = await self.run_agent(
                    agent,
                    request,
                    action_handler=_publish_action,
                    hooks=hooks,
                )
                await queue.put(result)
            finally:
                await queue.put(producer_done)

        tasks = [asyncio.create_task(_run_and_publish(agent)) for agent in agents]
        completed_count = 0
        yielded_count = 0
        action_count = 0
        try:
            while completed_count < len(tasks):
                item = await queue.get()
                if isinstance(item, _ProducerDone):
                    completed_count += 1
                    continue
                if isinstance(item, AgentActionEvent):
                    action_count += 1
                    yield item
                    continue
                self._logger.info(
                    "Dispatch yielding result: agent={} success={} error={}",
                    getattr(item, "name", "unknown"),
                    getattr(item, "success", None),
                    getattr(item, "error", None),
                )
                yielded_count += 1
                yield item
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._logger.info(
                "Dispatch end: completed={} yielded={} actions={}",
                completed_count,
                yielded_count,
                action_count,
            )
