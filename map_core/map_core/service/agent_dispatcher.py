from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Awaitable, Callable, Literal, Sequence, cast

from loguru import logger
from pydantic import BaseModel

from ..schema.agent_schema import Message
from ..schema.attachment_schema import UploadedKBFileSchema
from ..schema.scene_classification_schema import SceneClassificationResult
from ..schema.state_event_schema import AgentEventSchema
from ..utils.llm_engine import LLMEngine
from ..utils.query_rewriter import QueryRewriter
from .agent.agent_mapping import (
    SCENE_AGENT_CONFIGS,
    SceneAgentConfig,
)
from .agent.base import AgentActionEvent, AgentRequest, AgentResult
from .agent.fallback_agent_configs import get_fallback_scene_agent_configs
from .agent.tool_call_agent import Tool, ToolCallAgent
from .agent_runtime import AgentExecutionHooks, AgentExecutionSpec, AgentRuntime
from .dynamic_tools import build_mcp_tools, build_prompt_skill_tools
from .state_store import (
    GlobalAgentStateStore,
    fire_and_forget,
)


class AgentDispatchConfig(BaseModel):
    scene_agent_configs: dict[str, SceneAgentConfig] | None = None
    fetch_selected_agent_configs: bool = False
    merge_fallback_scene_agent_configs: bool = True
    mcp_servers: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    flow_skill_descriptors: list[dict[str, Any]] = []


class AgentDispatcher:
    AGENT_EVENT_CATEGORY = "agent"

    def __init__(
        self,
        *,
        llm: LLMEngine,
        tool_registry: dict[str, Tool] | None = None,
        scene_agent_configs: dict[str, SceneAgentConfig] | None = None,
        scene_agent_config_fetcher: (
            Callable[
                [list[str], AgentRequest, dict[str, Any] | None],
                Awaitable[Any],
            ]
            | None
        ) = None,
        query_rewrite_enabled: bool = False,
        logger_: Any | None = None,
    ) -> None:
        self.llm = llm
        self.tool_registry: dict[str, Tool] = tool_registry or {}
        self.scene_agent_configs: dict[str, SceneAgentConfig] = (
            scene_agent_configs or SCENE_AGENT_CONFIGS
        )
        self.scene_agent_config_fetcher = scene_agent_config_fetcher
        self.state_store: GlobalAgentStateStore | None = None
        self.state_id = None
        self.query_rewrite_enabled = query_rewrite_enabled
        self._logger = logger_ or logger
        self.query_rewriter = QueryRewriter(llm=self.llm, logger_=self._logger)
        self.agent_runtime = AgentRuntime(
            llm=llm,
            tool_registry=self.tool_registry,
            logger_=self._logger,
        )

    def register_tool(self, tool: Tool) -> None:
        self.tool_registry[tool.name] = tool

    def register_scene_agent_config(self, name: str, config: SceneAgentConfig) -> None:
        self.scene_agent_configs[name] = config

    def _register_dynamic_tools(self, config: AgentDispatchConfig) -> None:
        dynamic_tools = {}
        dynamic_tools.update(build_mcp_tools(config.mcp_servers or []))
        dynamic_tools.update(
            build_prompt_skill_tools(
                skills=config.skills or [],
                descriptors=config.flow_skill_descriptors or [],
                llm=self.llm,
            )
        )
        if not dynamic_tools:
            return
        self.tool_registry.update(dynamic_tools)
        self.agent_runtime.tool_registry = self.tool_registry

    def _get_scene_configs(
        self, config: AgentDispatchConfig
    ) -> dict[str, SceneAgentConfig]:
        return (
            config.scene_agent_configs
            if config.scene_agent_configs is not None
            else self.scene_agent_configs
        )

    @staticmethod
    def _merge_fallback_scene_agent_configs(
        config: AgentDispatchConfig,
    ) -> AgentDispatchConfig:
        if not config.merge_fallback_scene_agent_configs:
            return config

        scene_agent_configs = config.scene_agent_configs
        if scene_agent_configs is None:
            return config

        fallback_configs = get_fallback_scene_agent_configs()
        missing_configs = {
            name: fallback_config.model_copy(deep=True)
            for name, fallback_config in fallback_configs.items()
            if name not in scene_agent_configs
        }
        if not missing_configs:
            return config

        merged_configs = {
            **missing_configs,
            **scene_agent_configs,
        }
        return config.model_copy(update={"scene_agent_configs": merged_configs})

    @staticmethod
    def _merge_fetched_term_replacements(
        request: AgentRequest,
        fetched_term_replacements: Any,
    ) -> AgentRequest:
        if not fetched_term_replacements:
            return request

        extra = dict(request.extra or {})
        existing_term_replacements = extra.get("term_replacements")
        merged_term_replacements = (
            list(existing_term_replacements)
            if isinstance(existing_term_replacements, list)
            else []
        )
        for item in fetched_term_replacements:
            if isinstance(item, BaseModel):
                merged_term_replacements.append(item.model_dump(exclude_none=True))
            else:
                merged_term_replacements.append(item)

        extra["term_replacements"] = merged_term_replacements
        return request.model_copy(update={"extra": extra})

    async def _materialize_scene_configs(
        self,
        request: AgentRequest,
        config: AgentDispatchConfig,
        *,
        tool_context: dict[str, Any] | None,
    ) -> tuple[AgentRequest, AgentDispatchConfig, dict[str, Any] | None]:
        self._register_dynamic_tools(config)
        agents_to_fetch = self._resolve_agents_to_fetch(request, config)
        if not agents_to_fetch:
            return (
                request,
                self._merge_fallback_scene_agent_configs(config),
                tool_context,
            )

        if self.scene_agent_config_fetcher is None:
            raise ValueError(
                "fetch_selected_agent_configs is enabled but scene_agent_config_fetcher is not configured."
            )

        fetch_result = await self.scene_agent_config_fetcher(
            agents_to_fetch,
            request,
            tool_context,
        )
        request = self._merge_fetched_term_replacements(
            request,
            getattr(fetch_result, "term_replacements", None),
        )
        merged_tool_context = {
            **(tool_context or {}),
            **fetch_result.tool_context,
        }
        updated_config = config.model_copy(
            update={
                "scene_agent_configs": fetch_result.scene_agent_configs,
            }
        )
        return (
            request,
            self._merge_fallback_scene_agent_configs(updated_config),
            merged_tool_context,
        )

    @staticmethod
    def _resolve_agents_to_fetch(
        request: AgentRequest,
        config: AgentDispatchConfig,
    ) -> list[str]:
        if not config.fetch_selected_agent_configs:
            return []

        scene_result = request.scene_result
        if scene_result is None:
            return []
        return AgentDispatcher.resolve_agents(scene_result)

    def _inject_tool_context(
        self,
        request: AgentRequest,
        tool_context: dict[str, Any] | None,
    ) -> AgentRequest:
        """Important: tool agent parameters injecting method 2, via request.extra.tool_context."""
        if tool_context is None:
            return request

        extra = dict(request.extra or {})
        extra["tool_context"] = tool_context
        return request.model_copy(update={"extra": extra})

    @staticmethod
    def _resolve_agent_name_map(request: AgentRequest) -> dict[str, str]:
        raw = request.extra.get("agent_code_name_map")
        if not isinstance(raw, dict):
            return {}

        resolved: dict[str, str] = {}
        for raw_code, raw_name in raw.items():
            if not isinstance(raw_code, str):
                continue
            agent_code = raw_code.strip()
            if not agent_code or agent_code in resolved:
                continue
            if isinstance(raw_name, str) and raw_name.strip():
                resolved[agent_code] = raw_name.strip()
            else:
                resolved[agent_code] = agent_code
        return resolved

    async def _prepare_request(
        self,
        request: AgentRequest,
        *,
        tool_context: dict[str, Any] | None = None,
    ) -> AgentRequest:
        request_with_context = self._inject_tool_context(request, tool_context)

        original_query = request_with_context.query
        resolved_original_query = request_with_context.original_query or original_query
        updated_query = request_with_context.query
        if self.query_rewrite_enabled:
            uploaded_kb_files = request_with_context.extra.get(
                "uploaded_kb_file_schemas"
            )
            if uploaded_kb_files is None:
                uploaded_kb_files = request_with_context.extra.get(
                    "uploaded_kb_files"
                )
            typed_uploaded_kb_files = (
                uploaded_kb_files
                if isinstance(uploaded_kb_files, list)
                and all(
                    isinstance(item, UploadedKBFileSchema)
                    for item in uploaded_kb_files
                )
                else None
            )
            updated_query = await self._query_rewrite(
                request_with_context.query,
                request_with_context.history,
                uploaded_kb_files=typed_uploaded_kb_files,
            )
        if updated_query != original_query:
            self._logger.info(f"Dispatch query rewritten: {updated_query}")
        return request_with_context.model_copy(
            update={
                "query": updated_query,
                "original_query": resolved_original_query,
            }
        )

    def available_agents(
        self, request: AgentRequest, config: AgentDispatchConfig
    ) -> list[ToolCallAgent]:
        configs = self._get_scene_configs(config)
        if not configs:
            self._logger.warning("No scene agent configs available")
            return []
        agent_name_map = self._resolve_agent_name_map(request)
        agents = [
            self._build_scene_agent(
                name,
                config=configs.get(name),
                agent_name=agent_name_map.get(name),
            )
            for name in configs
        ]
        resolved = [agent for agent in agents if agent is not None]
        if not resolved:
            self._logger.warning("No agents matched the current request config")
        return resolved

    def set_execution_context(self, state_store: GlobalAgentStateStore, state_id: str):
        """
        Inject global agent state for all registered agents.
        Take care of your agents.
        """
        self.state_store = state_store
        self.state_id = state_id
        self.agent_runtime.set_execution_context(state_store, state_id)

    def _emit_agent_event(
        self,
        agent: ToolCallAgent,
        *,
        stage: Literal["start", "end"],
        status: Literal["success", "failed"] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if not self.state_store or not self.state_id:
            return
        event = AgentEventSchema(
            category=self.AGENT_EVENT_CATEGORY,
            component=agent.name,
            stage=stage,
            status=status,
            data={
                "agent_code": agent.name,
                "agent_name": agent.agent_display_name,
                **(data or {}),
            },
        )
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="agent_execution",
                payload=event.model_dump(),
            )
        )

    @staticmethod
    def resolve_agents(
        scene_result: SceneClassificationResult,
    ) -> list[str]:
        logger.debug("Resolving agents from scene result")
        resolved: list[str] = []
        for sub_scene_result in scene_result.sub_scenes:
            resolved.extend(sub_scene_result.sub_scenes)
        logger.debug(f"Resolved agent sequence: {', '.join(resolved) or '<empty>'}")
        return resolved

    def _build_scene_agent(
        self,
        name: str,
        config: SceneAgentConfig | None = None,
        agent_name: str | None = None,
    ) -> ToolCallAgent | None:
        scene_config = config or self.scene_agent_configs.get(name)
        if scene_config is None:
            self._logger.warning(f"No scene agent config registered for name: {name}")
            return None
        return self.agent_runtime.build_agent(
            AgentExecutionSpec(
                name=name,
                system_prompt=scene_config.prompt,
                additional_user_prompt=scene_config.additional_user_prompt,
                tool_names=list(scene_config.tool_names),
                max_steps=scene_config.max_steps,
                force_tool_call=scene_config.force_tool_call,
                llm_config=scene_config.llm_config,
                scene_post_summary=scene_config.scene_post_summary,
                agent_name=agent_name,
            )
        )

    def _build_execution_hooks(self) -> AgentExecutionHooks:
        return AgentExecutionHooks(
            on_agent_start=lambda current_agent, current_request: (
                self._emit_agent_event(
                    current_agent,
                    stage="start",
                    data={
                        "agent_id": current_agent.agent_id,
                        "input": {
                            "query": current_request.query,
                            "staff_code": current_request.staff_code,
                            "scene_result": current_request.scene_result,
                        },
                    },
                )
            ),
            on_agent_end=lambda current_agent, status, data: self._emit_agent_event(
                current_agent,
                stage="end",
                status=cast(Literal["success", "failed"], status),
                data=data,
            ),
        )

    def _agents_from_sequence(
        self,
        sequence: list[str],
        config: AgentDispatchConfig,
        agent_name_map: dict[str, str] | None = None,
    ) -> list[ToolCallAgent]:
        scene_configs = self._get_scene_configs(config)
        resolved_name_map = agent_name_map or {}
        resolved: list[ToolCallAgent] = []
        for name in sequence:
            agent = self._build_scene_agent(
                name,
                config=scene_configs.get(name),
                agent_name=resolved_name_map.get(name),
            )
            if agent is None:
                continue
            resolved.append(agent)
        return resolved

    async def _run_agent(
        self,
        agent: ToolCallAgent,
        request: AgentRequest,
        *,
        action_handler: Callable[[AgentActionEvent], Awaitable[None] | None]
        | None = None,
    ) -> AgentResult:
        return await self.agent_runtime.run_agent(
            agent,
            request,
            action_handler=action_handler,
            hooks=self._build_execution_hooks(),
        )

    async def _query_rewrite(
        self,
        query: str,
        history: Sequence[dict[str, Any]] | Sequence[Message] | Sequence[Any] | None,
        *,
        uploaded_kb_files: Sequence[UploadedKBFileSchema] | None = None,
    ) -> str:
        return await self.query_rewriter.rewrite(
            query,
            history,
            uploaded_kb_files=uploaded_kb_files,
        )

    async def dispatch(
        self,
        request: AgentRequest,
        config: AgentDispatchConfig | None = None,
        state_store: GlobalAgentStateStore | None = None,
        state_id: str | None = None,
        tool_context: dict[str, Any] | None = None,
    ) -> list[AgentResult]:
        results: list[AgentResult] = []
        async for result in self.dispatch_stream(
            request=request,
            config=config,
            state_store=state_store,
            state_id=state_id,
            tool_context=tool_context,
        ):
            if isinstance(result, AgentResult):
                results.append(result)
        return results

    async def dispatch_stream(
        self,
        request: AgentRequest,
        config: AgentDispatchConfig | None = None,
        state_store: GlobalAgentStateStore | None = None,
        state_id: str | None = None,
        tool_context: dict[str, Any] | None = None,
    ) -> AsyncGenerator[AgentResult | AgentActionEvent, None]:
        request, config, tool_context = await self._materialize_scene_configs(
            request,
            config or AgentDispatchConfig(),
            tool_context=tool_context,
        )
        updated_request = await self._prepare_request(
            request,
            tool_context=tool_context,
        )
        agent_name_map = self._resolve_agent_name_map(updated_request)

        if state_store and state_id:
            self.set_execution_context(state_store, state_id)

        scene_result = request.scene_result
        if scene_result is not None:
            big_scene_value = [item.big_scene for item in scene_result.big_scenes]
            sub_scene_value = [
                ss for item in scene_result.sub_scenes for ss in item.sub_scenes
            ]
        else:
            big_scene_value = None
            sub_scene_value = None
        self._logger.info(
            f"Dispatch start: big_scenes={big_scene_value} sub_scenes={sub_scene_value}"
        )
        if scene_result is not None:
            agents = self._agents_from_sequence(
                self.resolve_agents(scene_result),
                config,
                agent_name_map=agent_name_map,
            )
        else:
            agents = self.available_agents(updated_request, config)
        if not agents:
            self._logger.warning("Dispatch end: no agents selected")
            return
        self._logger.debug(
            f"Dispatching to agents: {', '.join(agent.name for agent in agents)}"
        )

        async for item in self.agent_runtime.run_stream(
            agents,
            updated_request,
            hooks=self._build_execution_hooks(),
        ):
            yield item

    async def run_single_agent(
        self,
        name: str,
        request: AgentRequest,
        *,
        config: SceneAgentConfig | None = None,
        state_store: GlobalAgentStateStore | None = None,
        state_id: str | None = None,
        tool_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        if state_store and state_id:
            self.set_execution_context(state_store, state_id)

        updated_request = await self._prepare_request(
            request,
            tool_context=tool_context,
        )
        agent_name_map = self._resolve_agent_name_map(updated_request)
        agent = self._build_scene_agent(
            name,
            config=config,
            agent_name=agent_name_map.get(name),
        )
        if agent is None:
            raise ValueError(f"Unknown scene agent: {name}")

        result = await self._run_agent(
            agent,
            updated_request,
        )
        return result
