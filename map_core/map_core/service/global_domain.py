import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, cast, overload
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Request
from loguru import logger

from ..config.common import (
    AGENT_MEMORY_DEFAULT_INTENTION_ID,
    DEEPSEEKV3_LOCAL_CONFIG,
    DS_V4_FLASH_AGENT_CONFIG,
    DS_V4_FLASH_LLM_CONFIG,
    QWEN3_NEXT_80B_CONFIG,
    SCENE_SELECTION_LLM_CONFIG,
    SUMMARIZATION_LLM_CONFIG,
)
from ..schema.attachment_schema import AttachmentSchema
from ..schema.global_domain_schema import (
    GlobalDomainChatSchema,
    GlobalDomainChatV3Schema,
    GlobalDomainDemoResponse,
    GlobalDomainStreamContext,
    GlobalDomainStreamEvent,
    SceneAgentDebugRequest,
    SceneAgentDebugResponse,
    ToolAgentDebugRequest,
    ToolAgentDebugResponse,
)
from ..schema.scene_classification_schema import (
    SceneClassificationResult,
)
from ..schema.state_event_schema import AgentEventSchema
from ..schema.tool_extra_result_schema import ToolExtraResultSchema
from ..service.agent.base import AgentActionEvent, AgentRequest, AgentResult
from ..service.agent.summarize_agent import SummarizeAgent
from ..service.agent.tool_call_agent import Tool
from ..service.agent.tool_registry import build_tool_registry, get_registered_agent_tool
from ..service.scene_selector import SceneSelector
from ..utils.llm_engine import LLMEngine
from ..utils.llm_trace_context import llm_trace_context
from ..utils.query_rewriter import QueryRewriter
from ..utils.term_replacer import replace_request_query_for_global_domain
from .agent.agent_mapping import SceneAgentConfig
from .agent_dispatcher import AgentDispatchConfig, AgentDispatcher
from .attachment_collector import AttachmentCollector
from .chart_plotting import (
    build_chart_plotting_payload,
    collect_chart_plotting_meta,
    generate_and_persist_chart_plotting,
)
from .global_domain_helpers import (
    build_dispatch_token_meta,
    normalize_attachment_results,
    normalize_tool_extra_results,
    record_summarize_failure,
    record_summarize_start,
    record_summarize_success,
    serialize_attachment_results,
    serialize_tool_extra_results,
    stream_event_data_as_dict,
)
from .scene_agent_config_provider import SceneAgentConfigProvider
from .state_store import (
    GlobalAgentStateStore,
    fire_and_forget,
    record_agent_call,
    safe_serialize,
)
from .tool_extra_result_collector import ToolExtraResultCollector

GlobalDomainRequest = GlobalDomainChatSchema | GlobalDomainChatV3Schema


class _GlobalDomainSceneSelectionObserver:
    def __init__(
        self,
        *,
        state_store: GlobalAgentStateStore,
        state_id: str,
        base_state: dict[str, Any],
    ) -> None:
        self.state_store = state_store
        self.state_id = state_id
        self.base_state = base_state

    def on_stage_start(self, stage: str, data: dict[str, Any]) -> None:
        record_kwargs: dict[str, Any] = {}
        if stage in {"big_scene", "direct_sub_agent_route"}:
            record_kwargs["base_state"] = self.base_state
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="scene_selector",
                payload=AgentEventSchema(
                    category="workflow",
                    component=stage,
                    stage="start",
                    data=data,
                ).model_dump(),
                **record_kwargs,
            )
        )

    def on_stage_end(
        self,
        stage: str,
        status: Literal["success", "failed"],
        data: dict[str, Any],
    ) -> None:
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="scene_selector",
                payload=AgentEventSchema(
                    category="workflow",
                    component=stage,
                    stage="end",
                    status=status,
                    data=data,
                ).model_dump(),
            )
        )


class GlobalDomain:
    DEFAULT_AGENT_DISPLAY_NAMES: dict[str, str] = {
        "General_Assistant": "通用问答助手",
    }
    CHART_PLOTTING_FINISH_BUDGET_S = 3
    ACTION_TEXT_PREVIEW_CHARS = 280
    RESULT_TEXT_PREVIEW_CHARS = 420

    def __init__(
        self,
        llm: LLMEngine | None = None,
        scene_selector: SceneSelector | None = None,
        request: GlobalDomainRequest | None = None,
        http_request: Request | None = None,
        staff_code: str | None = None,
    ) -> None:
        self.staff_code = (
            getattr(request, "staff_code", "missing")
            if request
            else staff_code or "missing"
        )
        self.request_id: str = (
            getattr(http_request.state, "request_id", None) if http_request else None
        ) or "missing"
        self.session_id: str | None = (
            getattr(http_request.state, "session_id", None) if http_request else None
        )
        self.workspace_id: str | None = (
            getattr(http_request.state, "workspace_id", None) if http_request else None
        )
        raw_request_token = (
            getattr(http_request.state, "request_token", None) if http_request else None
        )
        self.request_token: str | None = (
            raw_request_token.strip()
            if isinstance(raw_request_token, str) and raw_request_token.strip()
            else None
        )
        self.x_userid: str = (
            getattr(http_request.state, "x_userid", "missing")
            if http_request
            else "missing"
        )
        self.x_username: str = (
            getattr(http_request.state, "x_username", "missing")
            if http_request
            else "missing"
        )
        self.llm = llm or LLMEngine(config=DS_V4_FLASH_LLM_CONFIG)
        self.scene_selector = scene_selector or SceneSelector(
            llm=LLMEngine(config=SCENE_SELECTION_LLM_CONFIG)
            # llm=LLMEngine(config=QWEN3_NEXT_80B_CONFIG)
        )
        self.summarize_agent = SummarizeAgent(
            llm=LLMEngine(config=SUMMARIZATION_LLM_CONFIG)
        )
        self.scene_agent_config_provider = SceneAgentConfigProvider()
        self.agent_dispatcher = AgentDispatcher(
            llm=LLMEngine(config=DS_V4_FLASH_AGENT_CONFIG),
            tool_registry=cast(
                dict[str, Tool],
                build_tool_registry(llm=LLMEngine(config=DS_V4_FLASH_AGENT_CONFIG)),
            ),
            scene_agent_config_fetcher=self.scene_agent_config_provider.fetch_by_refs,
        )  # must to be request isolated instance!
        self.query_rewriter = QueryRewriter(
            llm=LLMEngine(config=DS_V4_FLASH_AGENT_CONFIG),
            logger_=logger,
        )
        self.attachment_collector = AttachmentCollector()
        self.tool_extra_result_collector = ToolExtraResultCollector()
        self.state_id = str(uuid4())
        self.state_store = GlobalAgentStateStore.instance()
        self._scene_token_usage: dict[str, int] = {}
        self.base_state = {
            "_id": self.state_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "staff_code": self.staff_code,
            "meta": {},
            "agent_code": "GlobalDomainOrchestrator",
            "agent_name": "GlobalDomainOrchestrator",
        }
        # self.backend_env = getattr(request, "backend_env", "missing") if request else "missing"

    async def select_scene(
        self,
        request: GlobalDomainRequest,
        dispatch_config: AgentDispatchConfig | None = None,
    ) -> SceneClassificationResult:
        return await self.select_scene_two_steps(
            request=request, dispatch_config=dispatch_config
        )

    async def select_scene_two_steps(
        self,
        request: GlobalDomainRequest,
        dispatch_config: AgentDispatchConfig | None = None,
    ) -> SceneClassificationResult:
        """Run two-level scene classification while keeping decision logic in SceneSelector."""
        observer = None
        if (
            getattr(self, "state_store", None) is not None
            and getattr(self, "state_id", None) is not None
            and getattr(self, "base_state", None) is not None
        ):
            observer = _GlobalDomainSceneSelectionObserver(
                state_store=self.state_store,
                state_id=self.state_id,
                base_state=self.base_state,
            )
        outcome = await self.scene_selector.select_scene_two_steps(
            request,
            observer=observer,
        )
        if not hasattr(self, "_scene_token_usage"):
            self._scene_token_usage = {}
        for k, v in outcome.token_usage.items():
            self._scene_token_usage[k] = self._scene_token_usage.get(k, 0) + v
        return outcome.result

    @record_agent_call(
        component="agent_dispatcher",
        category="workflow",
        meta_extractor=build_dispatch_token_meta,
    )
    async def dispatch_agents(
        self,
        request: GlobalDomainRequest,
        scene_result: SceneClassificationResult,
        state_id: str | None = None,
        state_store: GlobalAgentStateStore | None = None,
        dispatch_config: AgentDispatchConfig | None = None,
    ):
        agent_request = AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            scene_result=scene_result,
            history=request.history,
            extra=self._build_agent_extra(request),
        )
        return await self.agent_dispatcher.dispatch(
            agent_request,
            config=dispatch_config,
            state_store=state_store,
            state_id=state_id,
            tool_context=getattr(request, "tool_context", None),
        )

    async def dispatch_agents_stream(
        self,
        request: GlobalDomainRequest,
        scene_result: SceneClassificationResult,
        state_id: str | None = None,
        state_store: GlobalAgentStateStore | None = None,
        dispatch_config: AgentDispatchConfig | None = None,
    ) -> AsyncGenerator[Any, None]:
        agent_request = AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            scene_result=scene_result,
            history=request.history,
            extra=self._build_agent_extra(request),
        )
        async for result in self.agent_dispatcher.dispatch_stream(
            agent_request,
            config=dispatch_config,
            state_store=state_store,
            state_id=state_id,
            tool_context=getattr(request, "tool_context", None),
        ):
            yield result

    def _build_agent_extra(self, request: GlobalDomainRequest) -> dict[str, Any]:
        original_query = self._resolve_original_query(request)
        extra = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "intention_id": AGENT_MEMORY_DEFAULT_INTENTION_ID,
            "staff_code": self.staff_code,
            "original_query": original_query,
            "backend_env": getattr(request, "backend_env", "missing"),
            "backend_env_base_url": getattr(request, "backend_env_base_url", "missing"),
            "request_token": self.request_token,
            "x_userid": self.x_userid,
            "x_username": self.x_username,
            "attachment_collector": self.attachment_collector,
            "tool_extra_result_collector": self.tool_extra_result_collector,
            "agent_code_name_map": self._resolve_scene_selection_agent_name_map(
                request
            ),
        }
        if isinstance(request.attachments, list):
            extra["attachments"] = [item.model_dump() for item in request.attachments]

        if request.uploaded_kb_files:
            extra["uploaded_kb_file_schemas"] = list(request.uploaded_kb_files)
            extra["uploaded_kb_files"] = [
                item.model_dump() for item in request.uploaded_kb_files
            ]

        if getattr(request, "term_replacements", None):
            extra["term_replacements"] = [
                item.model_dump(exclude_none=True)
                for item in request.term_replacements or []
            ]
        extra["query_term_replacer_enabled"] = getattr(
            request,
            "query_term_replacer_enabled",
            False,
        )
        extra["rerank_model_config"] = request.rerank_model_config.model_dump()
        # Deprecated: do not flatten request.tool_context into request.extra.
        # Tool-agent configs should be read from request.extra.<agent_name>.
        # tool_context = request.tool_context
        # if isinstance(tool_context, dict):
        #     for key, value in tool_context.items():
        #         extra.setdefault(key, value)
        return extra

    @staticmethod
    def _resolve_original_query(request: GlobalDomainRequest) -> str:
        original_query = getattr(request, "original_query", None)
        if isinstance(original_query, str) and original_query:
            return original_query
        return request.query

    def _prepare_runtime_request(
        self,
        request: GlobalDomainRequest,
    ) -> GlobalDomainRequest:
        incoming_query = request.query
        original_query = self._resolve_original_query(request)
        request = request.model_copy(
            update={
                "original_query": original_query,
            }
        )
        scene_selection = getattr(request, "scene_selection", None)
        if (
            getattr(request, "summarize_config", None) is None
            and scene_selection is not None
            and getattr(scene_selection, "summary_prompt", None)
        ):
            summary_payload = {
                "system_prompt": scene_selection.summary_prompt,
            }
            if getattr(scene_selection, "summary_llm_config", None) is not None:
                summary_payload["llm_config"] = scene_selection.summary_llm_config
            request = request.model_copy(
                update={
                    "summarize_config": summary_payload
                }
            )
        prepared_request = cast(
            GlobalDomainRequest,
            replace_request_query_for_global_domain(request),
        )
        logger.info(
            "Term replacement: request_id={}, state_id={}, "
            "query={}, original_query={}, query_changed={}",
            self.request_id,
            self.state_id,
            prepared_request.query,
            self._resolve_original_query(prepared_request),
            prepared_request.query != incoming_query,
        )
        return prepared_request

    async def _rewrite_runtime_query(
        self,
        request: GlobalDomainRequest,
    ) -> GlobalDomainRequest:
        if not getattr(request, "query_rewrite_enabled", False):
            return request

        if not request.history and not request.uploaded_kb_files:
            logger.info(
                "No history/uploaded files available for query rewriting. Skip"
            )
            return request

        original_query = self._resolve_original_query(request)
        rewritten_query = await self.query_rewriter.rewrite(
            request.query,
            request.history,
            uploaded_kb_files=request.uploaded_kb_files,
        )
        if rewritten_query == request.query:
            return request

        logger.info(
            "Global query rewritten: request_id={}, state_id={}, query={}, rewritten_query={}",
            self.request_id,
            self.state_id,
            request.query,
            rewritten_query,
        )
        return cast(
            GlobalDomainRequest,
            request.model_copy(
                update={
                    "query": rewritten_query,
                    "original_query": original_query,
                }
            ),
        )

    @overload
    async def summarize(
        self,
        request: GlobalDomainRequest,
        dispatch_results: list[Any],
        *,
        stream: Literal[False] = False,
    ) -> str: ...

    @overload
    async def summarize(
        self,
        request: GlobalDomainRequest,
        dispatch_results: list[Any],
        *,
        stream: Literal[True],
    ) -> AsyncGenerator[str | dict[str, Any], None]: ...

    async def summarize(
        self,
        request: GlobalDomainRequest,
        dispatch_results: list[Any],
        *,
        stream: bool = False,
    ) -> str | AsyncGenerator[str | dict[str, Any], None]:
        start_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        summarize_extra: dict[str, Any] = {
            "original_query": self._resolve_original_query(request)
        }
        summarize_config = getattr(request, "summarize_config", None)
        if summarize_config is not None:
            summarize_extra["summarize_config"] = (
                summarize_config.model_dump(exclude_none=True)
                if hasattr(summarize_config, "model_dump")
                else summarize_config
                if isinstance(summarize_config, dict)
                else {}
            )
        summarize_request = AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            history=request.history,
            dispatch_results=dispatch_results,
            extra=summarize_extra,
        )
        summarize_debug_payload = self.summarize_agent.build_summarize_debug_payload(
            summarize_request
        )
        summarize_input = {
            "query": request.query,
            "stream": stream,
            "raw_dispatch_result_count": len(dispatch_results),
            **summarize_debug_payload,
        }

        record_summarize_start(
            state_store=self.state_store,
            state_id=self.state_id,
            base_state=self.base_state,
            summarize_input=summarize_input,
        )

        if stream:
            try:
                with llm_trace_context(
                    state_store=self.state_store,
                    state_id=self.state_id,
                    request_id=self.request_id,
                    session_id=self.session_id,
                    staff_code=self.staff_code,
                    agent_code="Master",
                    agent_name="Master 智能体",
                    component="summarize_agent",
                    phase="master_summary",
                    call_kind="summarize",
                ):
                    summary_stream = await self.summarize_agent.execute(
                        summarize_request,
                        stream=True,
                    )
            except Exception as exc:
                record_summarize_failure(
                    state_store=self.state_store,
                    state_id=self.state_id,
                    summarize_input=summarize_input,
                    start_ts=start_ts,
                    error=exc,
                    stream=True,
                )
                raise

            async def _tracked_summary_stream() -> AsyncGenerator[
                str | dict[str, Any], None
            ]:
                summary_parts: list[str] = []
                try:
                    with llm_trace_context(
                        state_store=self.state_store,
                        state_id=self.state_id,
                        request_id=self.request_id,
                        session_id=self.session_id,
                        staff_code=self.staff_code,
                        agent_code="Master",
                        agent_name="Master 智能体",
                        component="summarize_agent",
                        phase="master_summary",
                        call_kind="summarize",
                    ):
                        async for chunk in cast(
                            AsyncGenerator[str | dict[str, Any], None], summary_stream
                        ):
                            if isinstance(chunk, dict):
                                if chunk.get("type") != "content" or "data" not in chunk:
                                    continue
                                text = str(chunk.get("data") or "")
                                summary_parts.append(text)
                                yield chunk
                                continue

                            text = str(chunk)
                            if text:
                                summary_parts.append(text)
                            yield text
                except Exception as exc:
                    record_summarize_failure(
                        state_store=self.state_store,
                        state_id=self.state_id,
                        summarize_input=summarize_input,
                        start_ts=start_ts,
                        error=exc,
                        stream=True,
                    )
                    raise

                record_summarize_success(
                    state_store=self.state_store,
                    state_id=self.state_id,
                    output="".join(summary_parts),
                    start_ts=start_ts,
                    stream=True,
                )

            return _tracked_summary_stream()

        try:
            with llm_trace_context(
                state_store=self.state_store,
                state_id=self.state_id,
                request_id=self.request_id,
                session_id=self.session_id,
                staff_code=self.staff_code,
                agent_code="Master",
                agent_name="Master 智能体",
                component="summarize_agent",
                phase="master_summary",
                call_kind="summarize",
            ):
                result = await self.summarize_agent.execute(summarize_request)
        except Exception as exc:
            record_summarize_failure(
                state_store=self.state_store,
                state_id=self.state_id,
                summarize_input=summarize_input,
                start_ts=start_ts,
                error=exc,
                stream=False,
            )
            raise
        token_usage = self.summarize_agent.token_usage
        if token_usage:
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="token_usage",
                    payload=AgentEventSchema(
                        category="llm",
                        component="summarize_agent",
                        data={
                            "agent": "summarize_agent",
                            "token_usage": token_usage,
                        },
                    ).model_dump(),
                )
            )
        if not isinstance(result, AgentResult):
            error = (
                "Summarize agent returned unsupported result type: "
                f"{type(result).__name__}"
            )
            record_summarize_failure(
                state_store=self.state_store,
                state_id=self.state_id,
                summarize_input=summarize_input,
                start_ts=start_ts,
                error=error,
                stream=False,
            )
            raise RuntimeError(error)
        if not result.success:
            record_summarize_failure(
                state_store=self.state_store,
                state_id=self.state_id,
                summarize_input=summarize_input,
                start_ts=start_ts,
                error=result.error or "Summarize agent returned unsuccessful result",
                stream=False,
            )
            return ""
        record_summarize_success(
            state_store=self.state_store,
            state_id=self.state_id,
            output=result.content,
            start_ts=start_ts,
            stream=False,
        )
        return result.content

    def _resolve_dispatch_config(
        self,
        request: GlobalDomainRequest,
        dispatch_config: AgentDispatchConfig | None,
    ) -> AgentDispatchConfig | None:
        if dispatch_config is not None:
            return dispatch_config

        request_config = getattr(request, "dispatch_config", None)
        if request_config is None:
            if isinstance(request, GlobalDomainChatV3Schema):
                return AgentDispatchConfig(fetch_selected_agent_configs=True)
            return None

        scene_agent_configs = None
        if getattr(request_config, "scene_agent_configs", None) is not None:
            scene_agent_configs = {
                name: SceneAgentConfig(**config.model_dump())
                for name, config in request_config.scene_agent_configs.items()
            }

        return AgentDispatchConfig(
            scene_agent_configs=scene_agent_configs,
            mcp_servers=getattr(request_config, "mcp_servers", None) or [],
            skills=getattr(request_config, "skills", None) or [],
            flow_skill_descriptors=getattr(request_config, "flow_skill_descriptors", None)
            or [],
            fetch_selected_agent_configs=isinstance(request, GlobalDomainChatV3Schema),
            merge_fallback_scene_agent_configs=not isinstance(
                request, SceneAgentDebugRequest
            ),
            engine=getattr(request_config, "engine", None),
        )

    def _resolve_scene_agent_config(
        self,
        request: SceneAgentDebugRequest,
    ) -> SceneAgentConfig | None:
        if request.scene_agent_config is not None:
            return SceneAgentConfig(**request.scene_agent_config.model_dump())

        dispatch_config = self._resolve_dispatch_config(request, None)
        if dispatch_config is None or dispatch_config.scene_agent_configs is None:
            return None
        return dispatch_config.scene_agent_configs.get(request.agent_code)

    def _resolve_scene_selection_agent_name_map(
        self,
        request: GlobalDomainRequest,
    ) -> dict[str, str]:
        resolved: dict[str, str] = dict(self.DEFAULT_AGENT_DISPLAY_NAMES)

        scene_selection = getattr(request, "scene_selection", None)
        enabled_agent_codes = getattr(scene_selection, "enabled_agent_codes", None)
        if isinstance(enabled_agent_codes, dict):
            resolved.update(
                {
                    agent_code: agent_config.agent_name
                    for agent_code, agent_config in enabled_agent_codes.items()
                }
            )

        return resolved

    def _resolve_dispatch_agent_names(
        self,
        request: GlobalDomainRequest,
        scene_result: SceneClassificationResult,
        dispatch_config: AgentDispatchConfig | None,
    ) -> list[dict[str, str]]:
        mapped_agents = self.agent_dispatcher.resolve_agents(scene_result)
        agent_name_map = self._resolve_agent_display_name(request)

        merged: dict[str, str] = {}
        for agent_code in mapped_agents:
            if agent_code in merged:
                continue
            merged[agent_code] = agent_name_map.get(agent_code, agent_code)
        return [
            {"agent_code": agent_code, "agent_name": agent_name}
            for agent_code, agent_name in merged.items()
        ]

    def _resolve_agent_display_name(
        self,
        request: GlobalDomainRequest,
    ) -> dict[str, str]:
        return self._resolve_scene_selection_agent_name_map(request)

    def _build_stream_context(
        self,
        scene_result: SceneClassificationResult,
        dispatch_results: list[Any],
    ) -> GlobalDomainStreamContext:
        meta = build_dispatch_token_meta(dispatch_results)
        if self._scene_token_usage:
            token_meta = meta.setdefault("token_usage", {"total": {}, "by_agent": {}})
            token_meta["by_agent"]["scene_selector"] = dict(self._scene_token_usage)
            for k, v in self._scene_token_usage.items():
                token_meta["total"][k] = token_meta["total"].get(k, 0) + v
        return GlobalDomainStreamContext(
            request_id=self.request_id,
            state_id=self.state_id,
            scene_result=scene_result,
            attachment_results=self.attachment_collector.list_items() or None,
            tool_extra_results=self.tool_extra_result_collector.list_items() or None,
            meta=meta,
        )

    async def consume_event_stream(
        self,
        request: GlobalDomainRequest,
        *,
        dispatch_config: AgentDispatchConfig | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        event_stream = self.pipeline_stream(
            request,
            dispatch_config=dispatch_config,
            **kwargs,
        )

        summary_parts: list[str] = []
        attachment_results: list[AttachmentSchema] | None = None
        tool_extra_results: list[ToolExtraResultSchema] | None = None
        meta: dict[str, Any] = {}
        error_message: str | None = None

        async for event in event_stream:
            if event.event == "start":
                data = stream_event_data_as_dict(event)
                if attachment_results is None:
                    attachment_results = normalize_attachment_results(
                        data.get("attachment_results")
                    )
                if tool_extra_results is None:
                    tool_extra_results = normalize_tool_extra_results(
                        data.get("tool_extra_results")
                    )
                continue

            if event.event == "meta":
                data = stream_event_data_as_dict(event)
                if data.get("phase") == "agent_action":
                    continue
                meta.update(data)
                continue

            if event.event == "content_delta":
                data = stream_event_data_as_dict(event)
                content = data.get("content")
                if content:
                    summary_parts.append(str(content))
                continue

            if event.event == "done":
                data = stream_event_data_as_dict(event)
                if attachment_results is None:
                    attachment_results = normalize_attachment_results(
                        data.get("attachment_results")
                    )
                if tool_extra_results is None:
                    tool_extra_results = normalize_tool_extra_results(
                        data.get("tool_extra_results")
                    )
                final_content = data.get("content")
                if isinstance(final_content, str):
                    final_meta = dict(meta)
                    done_meta = data.get("meta")
                    if isinstance(done_meta, dict):
                        final_meta.update(done_meta)
                    return {
                        "content": final_content,
                        "attachment_results": attachment_results,
                        "tool_extra_results": tool_extra_results,
                        "meta": final_meta,
                    }
                continue

            if event.event == "error":
                data = stream_event_data_as_dict(event)
                error_message = str(data.get("error") or "stream failed")

        if error_message:
            raise RuntimeError(error_message)

        return {
            "content": "".join(summary_parts),
            "attachment_results": attachment_results,
            "tool_extra_results": tool_extra_results,
            "meta": meta,
        }

    def _build_dispatch_action_meta(
        self,
        request: GlobalDomainRequest,
        action_event: AgentActionEvent,
    ) -> dict[str, Any]:
        agent_name_map = self._resolve_agent_display_name(request)
        payload = safe_serialize(action_event.payload)
        tool_name: str | None = None
        tool_call_id: str | None = None
        tool_display_name: str | None = None
        query_preview: str | None = None
        result_summary: str | None = None
        selected_tool_calls: list[dict[str, Any]] | None = None
        action_status: str | None = None

        if isinstance(payload, dict):
            tool_name = str(payload.get("tool_name") or "").strip() or None
            tool_call_id = str(payload.get("tool_call_id") or "").strip() or None
            tool_display_name = (
                str(payload.get("tool_display_name") or "").strip() or None
            )
            query_preview = self._truncate_preview_text(payload.get("query"))
            if action_event.action == "tool_result":
                result_summary = self._summarize_payload_content(payload.get("result"))
                payload.pop("result", None)
                action_status = (
                    "failed"
                    if isinstance(result_summary, str) and "error" in result_summary
                    else "success"
                )
            elif action_event.action == "tool_call":
                action_status = "running"
            elif action_event.action == "tool_calls_selected":
                selected_calls_raw = payload.get("tool_calls")
                if isinstance(selected_calls_raw, list):
                    selected_tool_calls = []
                    for item in selected_calls_raw:
                        if not isinstance(item, dict):
                            continue
                        selected_tool_calls.append(
                            {
                                "tool_name": item.get("tool_name"),
                                "tool_call_id": item.get("tool_call_id"),
                                "args_summary": self._summarize_payload_content(
                                    item.get("args")
                                ),
                            }
                        )
                action_status = "selected"
        if action_event.action == "tool_result" and isinstance(payload, dict):
            payload.pop("result", None)
        return {
            "event_schema_version": "map-agent-action-v1",
            "agent_code": action_event.agent_code,
            "agent_name": action_event.agent_name
            or agent_name_map.get(action_event.agent_code, action_event.agent_code),
            "step": action_event.step,
            "action": action_event.action,
            "message": action_event.message,
            "status": action_status,
            "tool_name": tool_name,
            "tool_display_name": tool_display_name,
            "tool_call_id": tool_call_id,
            "query": query_preview,
            "selected_tool_calls": selected_tool_calls,
            "result_summary": result_summary,
            "payload": payload,
        }

    def _truncate_preview_text(self, value: Any, max_chars: int | None = None) -> str | None:
        text = self._normalize_preview_text(value)
        if not text:
            return None
        limit = max_chars or self.ACTION_TEXT_PREVIEW_CHARS
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _normalize_preview_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = str(safe_serialize(value))
            except Exception:
                text = str(value)
        return " ".join(text.split())

    def _summarize_payload_content(self, payload: Any) -> str | None:
        if payload is None:
            return None
        if isinstance(payload, dict):
            if payload.get("error"):
                return self._truncate_preview_text(
                    {"error": payload.get("error")},
                    max_chars=self.RESULT_TEXT_PREVIEW_CHARS,
                )
            content = payload.get("content")
            if content:
                return self._truncate_preview_text(
                    content, max_chars=self.RESULT_TEXT_PREVIEW_CHARS
                )
            if payload.get("success") is False:
                return self._truncate_preview_text(
                    payload, max_chars=self.RESULT_TEXT_PREVIEW_CHARS
                )
        return self._truncate_preview_text(payload, max_chars=self.RESULT_TEXT_PREVIEW_CHARS)

    def _build_dispatch_result_meta(
        self,
        request: GlobalDomainRequest,
        result: Any,
    ) -> dict[str, Any]:
        agent_code = getattr(result, "name", "unknown")
        agent_name_map = self._resolve_agent_display_name(request)
        success = bool(getattr(result, "success", True))
        meta_data = getattr(result, "meta_data", None)
        duration_s = None
        if isinstance(meta_data, dict):
            duration_val = meta_data.get("duration_s")
            if isinstance(duration_val, (int, float)):
                duration_s = float(duration_val)
        error = getattr(result, "error", None)
        output_summary = self._truncate_preview_text(
            getattr(result, "content", None),
            max_chars=self.RESULT_TEXT_PREVIEW_CHARS,
        )
        exit_payload = getattr(result, "exit", None)
        exit_reason: str | None = None
        if isinstance(exit_payload, dict):
            for key in ("reason", "exit_reason", "type"):
                value = exit_payload.get(key)
                if isinstance(value, str) and value.strip():
                    exit_reason = value.strip()
                    break

        observations = getattr(result, "tool_observations", None)
        tool_observations_count = (
            len(observations) if isinstance(observations, list) else 0
        )

        data_source_summary: dict[str, Any] | None = None
        data_source = getattr(result, "data_source", None)
        if isinstance(data_source, dict):
            data_source_summary = {
                "source": data_source.get("source"),
                "history_count": (
                    len(data_source.get("history"))
                    if isinstance(data_source.get("history"), list)
                    else None
                ),
            }

        return {
            "agent_code": agent_code,
            "agent_name": agent_name_map.get(agent_code, agent_code),
            "duration_s": duration_s,
            "success": success,
            "error": str(error) if error else None,
            "output_summary": output_summary,
            "exit_reason": exit_reason,
            "tool_observations_count": tool_observations_count,
            "data_source_summary": data_source_summary,
        }

    def _build_dispatch_failure_meta(
        self,
        request: GlobalDomainRequest,
        *,
        agent_code: str,
        error: str,
    ) -> dict[str, Any]:
        agent_name_map = self._resolve_agent_display_name(request)
        return {
            "agent_code": agent_code,
            "agent_name": agent_name_map.get(agent_code, agent_code),
            "duration_s": None,
            "success": False,
            "error": error,
            "output_summary": None,
            "exit_reason": None,
            "tool_observations_count": 0,
            "data_source_summary": None,
        }

    async def pipeline_stream(
        self,
        request: GlobalDomainRequest,
        *,
        dispatch_config: AgentDispatchConfig | None = None,
        **kwargs,
    ) -> AsyncGenerator[GlobalDomainStreamEvent, None]:
        request = self._prepare_runtime_request(request)
        request_start_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        resolved_dispatch_config = self._resolve_dispatch_config(
            request, dispatch_config
        )
        chart_task: asyncio.Task[dict[str, Any] | None] | None = None

        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="request.start",
                payload={
                    "request_id": self.request_id,
                    "session_id": self.session_id,
                    "workspace_id": self.workspace_id,
                    "staff_code": self.staff_code,
                    "query": request.query,
                    "original_query": self._resolve_original_query(request),
                    "start_ts": request_start_ts,
                },
            )
        )

        yield GlobalDomainStreamEvent(
            event="start",
            data={
                "request_id": self.request_id,
                "state_id": self.state_id,
                "chart_plotting_enabled": getattr(
                    request, "chart_plotting_enabled", False
                ),
                "backend_env": getattr(request, "backend_env", "missing"),
                "content_review_enabled": getattr(
                    request, "content_review_enabled", False
                ),
            },
        )

        try:
            # quote and query joint
            try:
                if getattr(request, "quote"):
                    query = f'"{request.quote}"\n{request.query}'
                    request = request.model_copy(update={"query": query})
            except Exception as exc:
                logger.error("Failed to process quote and query: {}", exc)
            request = await self._rewrite_runtime_query(request)
            try:
                scene_result = await self.select_scene(
                    request,
                    dispatch_config=resolved_dispatch_config,
                )
            except Exception as exc:
                logger.error(
                    "Scene selection failed, degrading to empty scene result: {}", exc
                )
                scene_result = SceneClassificationResult.model_construct(
                    big_scenes=[], sub_scenes=[]
                )
            dispatch_agent_names = self._resolve_dispatch_agent_names(
                request,
                scene_result,
                resolved_dispatch_config,
            )

            yield GlobalDomainStreamEvent(
                event="meta",
                data={
                    "phase": "scene_selected",
                    "agents": dispatch_agent_names,
                    "scene_result": safe_serialize(scene_result.model_dump()),
                },
            )

            dispatch_results: list[Any] = []
            dispatch_summary: list[dict[str, Any]] = []
            emitted_agent_codes: set[str] = set()
            try:
                async for result in self.dispatch_agents_stream(
                    request=request,
                    scene_result=scene_result,
                    dispatch_config=resolved_dispatch_config,
                    state_id=self.state_id,
                    state_store=self.state_store,
                ):
                    if isinstance(result, AgentActionEvent):
                        yield GlobalDomainStreamEvent(
                            event="meta",
                            data={
                                "phase": "agent_action",
                                "agents": [
                                    self._build_dispatch_action_meta(request, result)
                                ],
                            },
                        )
                        continue
                    dispatch_results.append(result)
                    result_meta = self._build_dispatch_result_meta(request, result)
                    agent_code = result_meta.get("agent_code")
                    if isinstance(agent_code, str) and agent_code:
                        emitted_agent_codes.add(agent_code)
                    dispatch_summary.append(result_meta)
                    yield GlobalDomainStreamEvent(
                        event="meta",
                        data={
                            "phase": "agent_result",
                            "agents": [result_meta],
                        },
                    )
            except Exception as exc:
                logger.error(
                    "Dispatch failed, continuing with collected dispatch results: {}",
                    exc,
                )
                error_message = str(exc)
                for agent in dispatch_agent_names:
                    agent_code = str(agent.get("agent_code") or "").strip()
                    if not agent_code or agent_code in emitted_agent_codes:
                        continue
                    failure_meta = self._build_dispatch_failure_meta(
                        request,
                        agent_code=agent_code,
                        error=error_message,
                    )
                    dispatch_summary.append(failure_meta)
                    emitted_agent_codes.add(agent_code)
                    yield GlobalDomainStreamEvent(
                        event="meta",
                        data={
                            "phase": "agent_result",
                            "agents": [failure_meta],
                        },
                    )

            stream_context = self._build_stream_context(scene_result, dispatch_results)
            yield GlobalDomainStreamEvent(
                event="meta",
                data={
                    "phase": "post_dispatch",
                    "agents": [
                        {
                            "agent_code": item.get("agent_code"),
                            "agent_name": item.get("agent_name"),
                            "duration_s": item.get("duration_s"),
                            "success": item.get("success"),
                            "error": item.get("error"),
                        }
                        for item in dispatch_summary
                    ],
                },
            )
            chart_plotting_payload: dict[str, Any] | None = None
            chart_plotting_attempted = False
            if getattr(request, "chart_plotting_enabled", False):
                chart_plotting_payload = build_chart_plotting_payload(
                    request_id=self.request_id,
                    state_id=self.state_id,
                    session_id=self.session_id,
                    query=request.query,
                    staff_code=self.staff_code,
                    backend_env=getattr(request, "backend_env", "EDITORIAL_STATE"),
                    backend_env_base_url=getattr(
                        request, "backend_env_base_url", "missing"
                    ),
                    dispatch_results=dispatch_results,
                )
                chart_plotting_attempted = chart_plotting_payload is not None
                chart_task = asyncio.create_task(
                    generate_and_persist_chart_plotting(
                        request=request,
                        dispatch_results=dispatch_results,
                        request_id=self.request_id,
                        state_id=self.state_id,
                        session_id=self.session_id,
                        query=request.query,
                        staff_code=self.staff_code,
                        request_token=self.request_token,
                        payload=chart_plotting_payload,
                    )
                )

            summary_stream = await self.summarize(
                request=request, dispatch_results=dispatch_results, stream=True
            )
            summary_parts: list[str] = []
            async for chunk in summary_stream:
                if isinstance(chunk, dict):
                    if chunk.get("type") != "content" or "data" not in chunk:
                        continue
                    text = str(chunk.get("data") or "")
                    summary_parts.append(text)
                    event_data: dict[str, Any] = {"content": text}
                    for key in (
                        "id",
                        "object",
                        "created",
                        "model",
                        "choices",
                        "prompt_token_ids",
                        "logprobs",
                    ):
                        if key in chunk:
                            event_data[key] = chunk.get(key)
                    yield GlobalDomainStreamEvent(
                        event="content_delta",
                        data=event_data,
                    )
                    continue

                text = str(chunk)
                if not text:
                    continue
                summary_parts.append(text)
                yield GlobalDomainStreamEvent(
                    event="content_delta",
                    data={"content": text},
                )

            yield GlobalDomainStreamEvent(
                event="meta",
                data={
                    "phase": "summarize_finished",
                    "chart_plotting_attempted": chart_plotting_attempted,
                },
            )

            summarize_usage = self.summarize_agent.token_usage
            if summarize_usage:
                fire_and_forget(
                    self.state_store.record_event(
                        state_id=self.state_id,
                        event_type="token_usage",
                        payload=AgentEventSchema(
                            category="llm",
                            component="summarize_agent",
                            data={
                                "agent": "summarize_agent",
                                "token_usage": summarize_usage,
                            },
                        ).model_dump(),
                    )
                )
                token_meta = stream_context.meta.setdefault(
                    "token_usage", {"total": {}, "by_agent": {}}
                )
                token_meta["by_agent"]["summarize_agent"] = summarize_usage
                for k, v in summarize_usage.items():
                    token_meta["total"][k] = token_meta["total"].get(k, 0) + v

            chart_plotting_meta: dict[str, Any] | None = None
            if chart_task is not None:
                chart_plotting_meta = await collect_chart_plotting_meta(
                    chart_task,
                    request_id=self.request_id,
                    timeout_s=self.CHART_PLOTTING_FINISH_BUDGET_S,
                )
            if chart_plotting_meta is not None:
                stream_context.meta["chart_plotting"] = chart_plotting_meta
                yield GlobalDomainStreamEvent(
                    event="meta",
                    data={
                        "phase": "chart_persisted",
                        "chart_plotting": chart_plotting_meta,
                    },
                )

            final_content = "".join(summary_parts)
            end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
            agents_called = [r.name for r in dispatch_results if hasattr(r, "name")]
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="request.end",
                    payload={
                        "request_id": self.request_id,
                        "session_id": self.session_id,
                        "workspace_id": self.workspace_id,
                        "status": "success",
                        "duration_s": (end_ts - request_start_ts).total_seconds(),
                        "scene_result": safe_serialize(scene_result),
                        "agents_called": agents_called,
                        "token_usage_total": stream_context.meta.get("token_usage"),
                        "error": None,
                    },
                )
            )

            yield GlobalDomainStreamEvent(
                event="done",
                data={
                    "content": final_content,
                    "attachment_results": serialize_attachment_results(
                        stream_context.attachment_results
                    ),
                    "tool_extra_results": serialize_tool_extra_results(
                        stream_context.tool_extra_results
                    ),
                    "meta": stream_context.meta,
                    "request_id": self.request_id,
                    "state_id": self.state_id,
                    "finished": True,
                },
            )
        except Exception as exc:
            if chart_task is not None:
                if not chart_task.done():
                    chart_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await chart_task
            logger.exception("Global domain event stream failed")
            end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="request.end",
                    payload={
                        "request_id": self.request_id,
                        "session_id": self.session_id,
                        "workspace_id": self.workspace_id,
                        "status": "failed",
                        "duration_s": (end_ts - request_start_ts).total_seconds(),
                        "error": str(exc),
                    },
                )
            )
            yield GlobalDomainStreamEvent(
                event="error",
                data={
                    "error": str(exc),
                    "request_id": self.request_id,
                    "state_id": self.state_id,
                },
            )

    async def debug_scene_agent(
        self,
        request: SceneAgentDebugRequest,
    ) -> SceneAgentDebugResponse:
        agent_request = AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            scene_result=request.scene_result,
            history=request.history,
            extra=self._build_agent_extra(request),
        )
        result = await self.agent_dispatcher.run_single_agent(
            request.agent_code,
            agent_request,
            config=self._resolve_scene_agent_config(request),
            state_store=self.state_store,
            state_id=self.state_id,
            tool_context=getattr(request, "tool_context", None),
        )
        logger.debug(
            "Debug scene agent executed: agent_code={}, result={}",
            request.agent_code,
            result,
        )

        return SceneAgentDebugResponse(
            request_id=self.request_id,
            state_id=self.state_id,
            agent_code=request.agent_code,
            result=result,
            attachment_results=self.attachment_collector.list_items() or None,
            tool_extra_results=self.tool_extra_result_collector.list_items() or None,
        )

    async def debug_tool_agent(
        self,
        request: ToolAgentDebugRequest,
    ) -> ToolAgentDebugResponse:
        extra = self._build_agent_extra(request)
        if request.tool_context is not None:
            extra["tool_context"] = request.tool_context

        agent_request = AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            scene_result=request.scene_result,
            history=request.history,
            extra=extra,
        )
        tool = get_registered_agent_tool(
            self.agent_dispatcher.tool_registry,
            request.tool_name,
        )
        agent, tool_request, _meta = tool.prepare_traceable_invocation(
            args=dict(request.tool_args or {}),
            request=agent_request,
            caller_agent_name=request.caller_agent_name,
        )
        agent.set_execution_context(self.state_store, self.state_id)
        result = await agent.execute(tool_request, parid="-")
        if not isinstance(result, AgentResult):
            raise RuntimeError(
                f"Tool agent '{request.tool_name}' returned unsupported result type: "
                f"{type(result).__name__}"
            )

        return ToolAgentDebugResponse(
            request_id=self.request_id,
            state_id=self.state_id,
            tool_name=request.tool_name,
            result=result,
            attachment_results=self.attachment_collector.list_items() or None,
            tool_extra_results=self.tool_extra_result_collector.list_items() or None,
        )

    async def pipeline(
        self,
        request: GlobalDomainRequest,
        *,
        dispatch_config: AgentDispatchConfig | None = None,
        **kwargs,
    ) -> tuple[SceneClassificationResult, list[Any], str]:
        request = self._prepare_runtime_request(request)
        resolved_dispatch_config = self._resolve_dispatch_config(
            request, dispatch_config
        )
        request = await self._rewrite_runtime_query(request)
        try:
            scene_result = await self.select_scene(
                request,
                dispatch_config=resolved_dispatch_config,
            )
        except Exception:
            scene_result = SceneClassificationResult.model_construct(
                big_scenes=[], sub_scenes=[]
            )
        try:
            dispatch_results = await self.dispatch_agents(
                request=request,
                scene_result=scene_result,
                dispatch_config=resolved_dispatch_config,
                state_id=self.state_id,
                state_store=self.state_store,
            )
        except Exception:
            dispatch_results = []

        chart_task: asyncio.Task[dict[str, Any] | None] | None = None
        if getattr(request, "chart_plotting_enabled", False):
            chart_plotting_payload = build_chart_plotting_payload(
                request_id=self.request_id,
                state_id=self.state_id,
                session_id=self.session_id,
                query=request.query,
                staff_code=self.staff_code,
                backend_env=getattr(request, "backend_env", "EDITORIAL_STATE"),
                backend_env_base_url=getattr(
                    request, "backend_env_base_url", "missing"
                ),
                dispatch_results=dispatch_results,
            )
            chart_task = asyncio.create_task(
                generate_and_persist_chart_plotting(
                    request=request,
                    dispatch_results=dispatch_results,
                    request_id=self.request_id,
                    state_id=self.state_id,
                    session_id=self.session_id,
                    query=request.query,
                    staff_code=self.staff_code,
                    request_token=self.request_token,
                    payload=chart_plotting_payload,
                )
            )

        summary_parts: list[str] = []
        try:
            summary_stream = await self.summarize(
                request=request,
                dispatch_results=dispatch_results,
                stream=True,
            )

            async for chunk in summary_stream:
                summary_parts.append(str(chunk))
        except Exception:
            if chart_task is not None:
                if not chart_task.done():
                    chart_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await chart_task
            raise

        chart_plotting_meta: dict[str, Any] | None = None
        if chart_task is not None:
            chart_plotting_meta = await collect_chart_plotting_meta(
                chart_task,
                request_id=self.request_id,
                timeout_s=self.CHART_PLOTTING_FINISH_BUDGET_S,
            )
        if chart_plotting_meta is not None:
            logger.info(
                "Pipeline chart plotting finished: request_id={}, status={}",
                self.request_id,
                chart_plotting_meta.get("status"),
            )

        return scene_result, dispatch_results, "".join(summary_parts)

    async def demo(self, request: GlobalDomainRequest) -> GlobalDomainDemoResponse:
        try:
            logger.info(f"Received global domain request:\n{request}")

            _, dispatch_results, summary = await self.pipeline(request)

            return GlobalDomainDemoResponse(
                summary=summary,
                dispatch_results=dispatch_results,
            )
        except Exception as exc:
            logger.exception("Pipeline demo failed!")
            return GlobalDomainDemoResponse(
                summary="",
                dispatch_results=[],
                error=str(exc),
            )

    # async def record_tool_call(self, tool_name: str, payload: dict[str, Any]) -> None:
    #     """Optional hook for tool-call recording."""

    #     await self.state_store.record_event(
    #         self.state_id,
    #         "tool_call",
    #         {"tool": tool_name, **payload},
    #     )


async def main():
    gd = GlobalDomain()
    query = "经营与资源治理和业务运营与交付近况如何?"
    query = "公司的财务状况如何?"
    query = "去年MAP的合同额怎么样?"
    query = "今年公司的有效人力有多少?"
    query = "公司的研发项目进度如何?"
    query = "市场开发和销售情况如何?"
    query = "王宽心的25年加班时间详情"

    print(f"Query: {query}")

    result = await gd.demo(
        GlobalDomainChatSchema(
            query=query,
        )
    )
    print("\n--- Scene Classification Result ---")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
