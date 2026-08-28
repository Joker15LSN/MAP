from __future__ import annotations

import datetime
import inspect
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any, Awaitable
from zoneinfo import ZoneInfo

from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ThinkingBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import Msg, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolBase, Toolkit
from loguru import logger

from ...utils.global_context import agent_log_context
from ..agent.base import AgentActionEvent, AgentRequest, AgentResult, ExecutionResult
from ..agent.tool_call_exit import (
    ScenePostSummaryRuntimeConfig,
    ToolCallExitHandler,
)
from ..agent.tool_call_session import ToolCallSession
from ..agent.tool_executor import ToolExecutor
from ..agent.tool_runtime import Tool, ToolSet
from ..agent.traceable_agent import TraceableAgent
from ..prompt.tool_call_prompt import (
    NEXT_STEP_PROMPT,
    UPLOADED_KB_FILE_SYSTEM_PROMPT_TEMPLATE,
)
from ..state_store import safe_serialize
from .event import model_usage
from .message import (
    map_history_to_agentscope,
)
from .model import MapChatModelAdapter
from .offloader import AgentScopeArtifactOffloader, ArtifactStorePort
from .tool import MapToolAdapter, extract_result_for_llm_context

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class _StepSystemPromptMiddleware(MiddlewareBase):
    def __init__(self, owner: "AgentScopeSceneAgent", request: AgentRequest) -> None:
        self.owner = owner
        self.request = request

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        del current_prompt
        return self.owner.format_system_prompt(
            self.request,
            step=agent.state.cur_iter,
        )


class AgentScopeSceneAgent(TraceableAgent):
    """MAP-compatible scene agent powered by AgentScope's ReAct runtime."""

    TERMINATE_TOOL_NAME = "terminate"

    def __init__(
        self,
        llm: Any,
        *,
        name: str,
        system_prompt: str | None,
        additional_user_prompt: str | None,
        tools: Sequence[Tool],
        max_steps: int,
        force_tool_call: bool,
        scene_post_summary: ScenePostSummaryRuntimeConfig | None,
        # EXPERIMENTAL: no production call site passes an artifact store yet
        # (see offloader.py); the offloader branch below is dormant.
        artifact_store: ArtifactStorePort | None = None,
        tools_timeout: float = 180.0,
    ) -> None:
        super().__init__(llm=llm, name=name)
        self.system_prompt = system_prompt or ""
        self.additional_user_prompt = additional_user_prompt or ""
        self.toolset = ToolSet(tools, include_terminate=False)
        self.max_steps = max_steps
        self.force_tool_call = force_tool_call
        self.scene_post_summary = scene_post_summary
        self.artifact_store = artifact_store
        self.tools_timeout = tools_timeout
        self.action_handler: (
            Callable[[AgentActionEvent], Any | Awaitable[Any]] | None
        ) = None
        self.current_step = 0
        self.agentscope_agent: Agent | None = None
        self.model_adapter: MapChatModelAdapter | None = None
        self._calls_by_step: dict[int, list[Any]] = {}
        self._call_ids_by_name: dict[tuple[int, str], deque[str]] = defaultdict(deque)
        self._tool_names_by_call_id: dict[str, str] = {}
        self._call_order: list[str] = []
        self._results_by_call_id: dict[str, Any] = {}
        self._observations_by_call_id: dict[str, dict[str, Any]] = {}
        self._framework_result_text_by_call_id: dict[str, list[str]] = defaultdict(list)
        self._adapter_names: set[str] = set()
        self._selected_emitted_steps: set[int] = set()
        self._tool_call_action_ids: set[str] = set()
        self._completed_call_ids: set[str] = set()
        self._flushed_steps: set[int] = set()
        self._max_steps_reached = False
        self._compat_session: ToolCallSession | None = None

        self.tool_executor = ToolExecutor(
            owner=self,
            toolset=self.toolset,
            tools_timeout=tools_timeout,
            terminate_tool_name=self.TERMINATE_TOOL_NAME,
            log_tag_getter=self._agent_log_tag,
            result_log_preview_builder=self._build_tool_result_log_preview,
            tool_observation_builder=self._build_tool_observation,
            action_emitter=self._emit_action_event,
        )
        self.exit_handler = ToolCallExitHandler(
            owner=self,
            terminate_tool_name=self.TERMINATE_TOOL_NAME,
            force_tool_call=force_tool_call,
            scene_post_summary=scene_post_summary,
            log_tag_getter=self._agent_log_tag,
            parse_tool_args=ToolExecutor._parse_tool_args,
            build_data_source=self._build_data_source,
            resolve_terminate_content=self._resolve_terminate_content,
            action_emitter=self._emit_action_event,
        )

    def _agent_log_tag(self) -> str:
        return f"[{self.name} AGENT]"

    def set_action_handler(
        self,
        action_handler: Callable[[AgentActionEvent], Any | Awaitable[Any]] | None,
    ) -> None:
        self.action_handler = action_handler

    async def _emit_action_event(
        self,
        *,
        action: str,
        step: int | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if action == "tool_call" and payload:
            tool_call_id = payload.get("tool_call_id")
            if tool_call_id is not None:
                self._tool_call_action_ids.add(str(tool_call_id))
        if self.action_handler is None:
            return
        event = AgentActionEvent(
            agent_code=self.name,
            agent_name=self.agent_display_name,
            step=step,
            action=action,
            message=message,
            payload=safe_serialize(payload or {}),
        )
        emitted = self.action_handler(event)
        if inspect.isawaitable(emitted):
            await emitted

    def _reset_run_state(self) -> None:
        self.current_step = 0
        self._calls_by_step.clear()
        self._call_ids_by_name.clear()
        self._tool_names_by_call_id.clear()
        self._call_order.clear()
        self._results_by_call_id.clear()
        self._observations_by_call_id.clear()
        self._framework_result_text_by_call_id.clear()
        self._adapter_names.clear()
        self._selected_emitted_steps.clear()
        self._tool_call_action_ids.clear()
        self._completed_call_ids.clear()
        self._flushed_steps.clear()
        self._max_steps_reached = False
        self._compat_session = None

    def _register_model_response(self, step: int, response: Any) -> None:
        calls = list(response.tool_calls or [])
        self._calls_by_step[step] = calls
        for call in calls:
            name = str(call.function.name)
            call_id = str(call.id)
            self._call_ids_by_name[(step, name)].append(call_id)
            self._tool_names_by_call_id[call_id] = name
            self._call_order.append(call_id)
        if calls and self._compat_session is not None:
            self._compat_session.append_assistant_tool_calls(response)

    @staticmethod
    def _normalize_history(history: Sequence[Any] | None) -> list[dict[str, Any]]:
        if not history:
            return []
        normalized: list[dict[str, Any]] = []
        for item in history:
            if hasattr(item, "to_dict") and callable(item.to_dict):
                value = item.to_dict()
                normalized.append(
                    dict(value)
                    if isinstance(value, dict)
                    else {"role": "user", "content": str(value)}
                )
            elif isinstance(item, dict):
                normalized.append(dict(item))
            else:
                normalized.append({"role": "user", "content": str(item)})
        return normalized

    def claim_tool_call_id(self, tool_name: str, step: int) -> str:
        queue = self._call_ids_by_name[(step, tool_name)]
        if queue:
            return queue.popleft()
        # AgentScope validates call IDs, so this is only a defensive fallback
        # for malformed third-party model responses.
        return f"{self.agent_id}-{step}-{tool_name}-{len(self._call_order)}"

    def register_tool_result(
        self,
        *,
        step: int,
        tool_name: str,
        tool_call_id: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        self._results_by_call_id[tool_call_id] = result
        self._observations_by_call_id[tool_call_id] = self._build_tool_observation(
            step_index=step,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=args,
            result=result,
        )

    async def record_tool_timeout(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        step: int,
        result: dict[str, Any],
    ) -> None:
        await self._emit_action_event(
            action="tool_result",
            step=step,
            message=f"工具“{tool_name}”执行超时",
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "result": result,
            },
        )
        self.record_tool_result(
            tool_name=tool_name,
            result=result,
            step_index=step,
        )

    def _build_tool_observation(
        self,
        *,
        step_index: int,
        tool_name: str,
        tool_call_id: str,
        args: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        return {
            "step": step_index,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_args": safe_serialize(args),
            "tool_result": extract_result_for_llm_context(result),
        }

    def _build_tool_result_log_preview(self, result: Any) -> Any:
        payload = self._serialize_tool_result(result)
        if not isinstance(payload, dict):
            return payload
        preview = {
            key: payload[key]
            for key in ("success", "name", "content", "error")
            if key in payload
        }
        data_source = payload.get("data_source")
        if isinstance(data_source, dict):
            preview["data_source"] = self._build_data_source_log_preview(data_source)
        elif data_source is not None:
            preview["data_source"] = self._basic_log_stats(data_source)
        return preview

    @classmethod
    def _build_data_source_log_preview(
        cls,
        data_source: dict[str, Any],
    ) -> dict[str, Any]:
        preview: dict[str, Any] = {}
        if "source" in data_source:
            preview["source"] = data_source["source"]
        for key, value in data_source.items():
            if key != "source":
                preview[key] = cls._basic_log_stats(value)
        return preview

    @staticmethod
    def _basic_log_stats(value: Any) -> dict[str, Any]:
        if isinstance(value, list):
            return {"type": "list", "count": len(value)}
        if isinstance(value, dict):
            return {"type": "dict", "count": len(value)}
        if isinstance(value, str):
            return {"type": "str", "chars": len(value)}
        return {"type": type(value).__name__}

    def _serialize_tool_result(self, result: Any) -> Any:
        if isinstance(result, ExecutionResult):
            return safe_serialize(result.model_dump(exclude_none=True))
        return safe_serialize(result)

    def _build_data_source(
        self,
        *,
        source: str,
        messages: list[dict[str, Any]],
        step: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": source,
            "history": safe_serialize(messages),
        }
        if step is not None:
            payload["step"] = step
        return payload

    @staticmethod
    def _resolve_terminate_content(
        messages: list[dict[str, Any]], response: Any | None
    ) -> str:
        direct = ((getattr(response, "content", "") if response else "") or "").strip()
        if direct:
            return direct
        return ToolCallSession(messages=messages).latest_non_empty_content()

    def format_system_prompt(
        self,
        request: AgentRequest,
        *,
        step: int | None = None,
    ) -> str:
        uploaded_kb_files = request.extra.get("uploaded_kb_files")
        uploaded_prompt = ""
        if uploaded_kb_files:
            uploaded_file_id_names = "\n".join(
                f"{item['file_id']}: {item['file_name']}" for item in uploaded_kb_files
            )
            uploaded_prompt = UPLOADED_KB_FILE_SYSTEM_PROMPT_TEMPLATE.format(
                kb_file_id_and_names=uploaded_file_id_names
            )
        step_prompt = NEXT_STEP_PROMPT
        if step is not None:
            step_prompt = (
                f"{NEXT_STEP_PROMPT} Current step: {step + 1}/{self.max_steps}."
            )
        system_prompt = "\n\n".join(
            item
            for item in (self.system_prompt.strip(), uploaded_prompt, step_prompt)
            if item
        )
        try:
            now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
            current_datetime = (
                f"{now:%Y-%m-%d %H:%M:%S}（{WEEKDAY_NAMES[now.weekday()]}）"
            )
            return system_prompt.format(
                staff_code=request.staff_code,
                current_datetime=current_datetime,
            )
        except Exception as exc:
            logger.warning(
                "{} System prompt format failed, using raw prompt: {}",
                self._agent_log_tag(),
                exc,
            )
            return system_prompt

    async def _emit_selected_calls(self, step: int) -> None:
        if step in self._selected_emitted_steps:
            return
        calls = self._calls_by_step.get(step) or []
        if not calls:
            return
        self._selected_emitted_steps.add(step)
        await self._emit_action_event(
            action="tool_calls_selected",
            step=step,
            message=f"第 {step + 1} 步：已选择 {len(calls)} 个工具调用",
            payload={
                "tool_calls": [
                    {
                        "tool_name": call.function.name,
                        "tool_call_id": call.id,
                        "args": ToolExecutor._parse_tool_args(call.function.arguments),
                    }
                    for call in calls
                ]
            },
        )

    async def _emit_framework_tool_call_if_missing(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
    ) -> None:
        if tool_call_id in self._tool_call_action_ids:
            return
        call = next(
            (
                item
                for item in self._calls_by_step.get(self.current_step, [])
                if str(item.id) == tool_call_id
            ),
            None,
        )
        args = ToolExecutor._parse_tool_args(
            call.function.arguments if call is not None else None
        )
        await self._emit_action_event(
            action="tool_call",
            step=self.current_step,
            message=f"正在调用工具“{tool_name}”",
            payload={
                "tool_name": tool_name,
                "tool_display_name": tool_name,
                "tool_call_id": tool_call_id,
                "query": "",
                "args": args,
            },
        )
        self.record_tool_call(
            tool_name=tool_name,
            tool_args=args,
            step_index=self.current_step,
            meta={"tool_id": tool_call_id},
        )

    async def _handle_event(self, event: Any) -> None:
        if isinstance(event, ModelCallStartEvent):
            if self.agentscope_agent is not None:
                self.current_step = self.agentscope_agent.state.cur_iter
            await self._emit_action_event(
                action="step_start",
                step=self.current_step,
                message=f"第 {self.current_step + 1} 步：选择下一步动作",
            )
            return
        if isinstance(event, ModelCallEndEvent):
            self._accumulate_usage(model_usage(event))
            await self._emit_selected_calls(self.current_step)
            if (
                self.model_adapter
                and self.model_adapter.last_terminate_call is not None
            ):
                call = self.model_adapter.last_terminate_call
                await self._emit_action_event(
                    action="terminate",
                    step=self.current_step,
                    message=f"第 {self.current_step + 1} 步：已选择终止循环",
                    payload={
                        "tool_name": self.TERMINATE_TOOL_NAME,
                        "tool_call_id": call.id,
                        "args": ToolExecutor._parse_tool_args(call.function.arguments),
                    },
                )
            return
        if isinstance(event, ThinkingBlockDeltaEvent):
            if event.delta:
                self.record_thought(event.delta, step_index=self.current_step)
            return
        if isinstance(event, ToolCallStartEvent):
            await self._emit_selected_calls(self.current_step)
            if event.tool_call_name not in self._adapter_names:
                await self._emit_framework_tool_call_if_missing(
                    tool_name=event.tool_call_name,
                    tool_call_id=event.tool_call_id,
                )
            return
        if isinstance(event, ToolResultTextDeltaEvent):
            self._framework_result_text_by_call_id[event.tool_call_id].append(
                event.delta
            )
            return
        if isinstance(event, ToolResultEndEvent):
            self._completed_call_ids.add(event.tool_call_id)
            if event.tool_call_id not in self._results_by_call_id:
                tool_name = self._tool_names_by_call_id.get(
                    event.tool_call_id,
                    "unknown_tool",
                )
                text = "".join(
                    self._framework_result_text_by_call_id.get(
                        event.tool_call_id,
                        [],
                    )
                )
                result: Any = text or {
                    "error": "AgentScope rejected the tool call.",
                    "tool": tool_name,
                }
                call = next(
                    (
                        item
                        for item in self._calls_by_step.get(self.current_step, [])
                        if str(item.id) == event.tool_call_id
                    ),
                    None,
                )
                args = ToolExecutor._parse_tool_args(
                    call.function.arguments if call is not None else None
                )
                await self._emit_framework_tool_call_if_missing(
                    tool_name=tool_name,
                    tool_call_id=event.tool_call_id,
                )
                self.register_tool_result(
                    step=self.current_step,
                    tool_name=tool_name,
                    tool_call_id=event.tool_call_id,
                    args=args,
                    result=result,
                )
                await self._emit_action_event(
                    action="tool_result",
                    step=self.current_step,
                    message=f"工具“{tool_name}”执行失败",
                    payload={
                        "tool_name": tool_name,
                        "tool_call_id": event.tool_call_id,
                        "result": result,
                    },
                )
                self.record_tool_result(
                    tool_name=tool_name,
                    result=result,
                    step_index=self.current_step,
                    meta={"tool_id": event.tool_call_id},
                )
            expected_ids = [
                str(call.id)
                for call in self._calls_by_step.get(self.current_step, [])
                if call.function.name != self.TERMINATE_TOOL_NAME
            ]
            if expected_ids and all(
                call_id in self._completed_call_ids for call_id in expected_ids
            ):
                self._flush_step_results(self.current_step)
                await self._emit_action_event(
                    action="step_complete",
                    step=self.current_step,
                    message=f"第 {self.current_step + 1} 步：已完成 {len(expected_ids)} 个工具调用",
                    payload={"tool_call_ids": expected_ids},
                )
            return
        if isinstance(event, ExceedMaxItersEvent):
            self._max_steps_reached = True
            await self._emit_action_event(
                action="max_steps",
                step=self.max_steps,
                message=f"已达到最大步骤数（{self.max_steps}）",
                payload={"max_steps": self.max_steps},
            )

    def _flush_step_results(self, step: int) -> None:
        if step in self._flushed_steps or self._compat_session is None:
            return
        calls = self._calls_by_step.get(step) or []
        results = {
            str(call.id): self._results_by_call_id[str(call.id)]
            for call in calls
            if str(call.id) in self._results_by_call_id
        }
        if not results:
            return
        self._flushed_steps.add(step)
        self._compat_session.append_tool_results(
            tool_calls=calls,
            results=results,
            terminate_tool_name=self.TERMINATE_TOOL_NAME,
            serializer=self._serialize_tool_result,
        )
        self._compat_session.mark_tool_called(True)
        self._compat_session.add_tool_observations(
            [
                self._observations_by_call_id[str(call.id)]
                for call in calls
                if str(call.id) in self._observations_by_call_id
            ]
        )

    def _build_tool_adapters(
        self,
        request: AgentRequest,
    ) -> list[ToolBase]:
        adapters: list[ToolBase] = []
        for tool in self.toolset.enabled_tools(request):
            schema = tool.to_openai_tool(request, owner_agent_name=self.name)
            adapter = MapToolAdapter(
                tool=tool,
                function_schema=schema,
                owner=self,
                request=request,
            )
            adapters.append(adapter)
            self._adapter_names.add(adapter.name)
        return adapters

    def _build_compat_session(self) -> ToolCallSession:
        if self._compat_session is None:
            return ToolCallSession(messages=[])
        return ToolCallSession(
            messages=[dict(item) for item in self._compat_session.messages],
            tool_called=self._compat_session.tool_called,
            tool_observations=list(self._compat_session.tool_observations),
        )

    async def _finalize_result(self, request: AgentRequest) -> AgentResult:
        session = self._build_compat_session()

        if self.model_adapter and self.model_adapter.last_terminate_call is not None:
            call = self.model_adapter.last_terminate_call
            response = self.model_adapter.last_terminate_response
            args = ToolExecutor._parse_tool_args(call.function.arguments)
            meta = {"tool_id": call.id, "tool_name": self.TERMINATE_TOOL_NAME}
            self.record_tool_call(
                tool_name=self.TERMINATE_TOOL_NAME,
                tool_args=args,
                step_index=self.current_step,
                meta=meta,
            )
            self.record_tool_result(
                tool_name=self.TERMINATE_TOOL_NAME,
                result={"status": "terminated"},
                step_index=self.current_step,
                meta=meta,
            )
            return await self.exit_handler.finalize(
                request=request,
                response=response,
                session=session,
                step=self.current_step,
                source="tool_terminate",
                exit_reason="terminate",
                extra_exit_metadata={
                    "tool_call_id": call.id,
                    "tool_args": safe_serialize(args),
                    "step": self.current_step,
                },
            )

        if self._max_steps_reached:
            result = await self.exit_handler.finalize(
                request=request,
                response=None,
                session=session,
                step=self.max_steps,
                source="max_steps",
                exit_reason="max_steps",
                extra_exit_metadata={"max_steps": self.max_steps},
            )
            if session.tool_called or (result.content and result.content.strip()):
                return result.model_copy(update={"success": True, "error": None})
            return AgentResult(
                success=False,
                name=self.name,
                content=result.content,
                reasoning_content=getattr(result, "reasoning_content", None),
                error="Max tool-call steps reached without any successful tool execution.",
                data_source=result.data_source,
                meta_data=result.meta_data,
            )

        last_response = self.model_adapter.last_response if self.model_adapter else None
        response = SimpleNamespace(
            content=str(getattr(last_response, "content", "") or ""),
            reasoning_content=getattr(last_response, "reasoning_content", None),
        )
        return await self.exit_handler.handle_final_answer(
            response=response,
            step=self.current_step,
            request=request,
            session=session,
        )

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        self.parid = parid
        self.check_cancelled()
        self._reset_run_state()
        request = self.exit_handler.prepare_request_for_execution(request)
        self._compat_session = ToolCallSession.from_request(
            request_query=request.query,
            history=request.history,
            history_normalizer=self._normalize_history,
            additional_user_prompt=self.additional_user_prompt,
        )
        adapters = self._build_tool_adapters(request)
        if not adapters:
            logger.warning(
                "{} No tools enabled for this request", self._agent_log_tag()
            )
            return AgentResult(
                success=False,
                name=self.name,
                content="",
                error="No tools enabled for this request.",
            )

        self.model_adapter = MapChatModelAdapter(
            self.llm,
            force_tool_call=self.force_tool_call,
            response_handler=self._register_model_response,
        )
        self.model_adapter.cancel_event = self.cancel_event
        offloader = None
        if self.artifact_store is not None:
            offloader = AgentScopeArtifactOffloader(
                self.artifact_store,
                agent_code=self.name,
                request_metadata={
                    key: request.extra.get(key)
                    for key in (
                        "request_id",
                        "session_id",
                        "tenant_id",
                        "x_userid",
                    )
                    if request.extra.get(key) not in (None, "", "missing")
                },
            )
        self.agentscope_agent = Agent(
            name=self.name,
            system_prompt=self.format_system_prompt(request, step=0),
            model=self.model_adapter,
            toolkit=Toolkit(tools=adapters),
            middlewares=[_StepSystemPromptMiddleware(self, request)],
            offloader=offloader,
            react_config=ReActConfig(max_iters=self.max_steps),
        )

        inputs: list[Msg] = map_history_to_agentscope(request.history)
        query = request.query
        if self.additional_user_prompt:
            query = f"{self.additional_user_prompt}\n{query}"
        inputs.append(UserMsg(name="user", content=query))

        config = getattr(self.llm, "config", None)
        agentscope_agent = self.agentscope_agent
        if agentscope_agent is None:
            raise RuntimeError("AgentScope agent was not initialized")
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(
                "{} AgentScopeSceneAgent '{}' starting with query: {}, LLM: {}, temperature: {}, max_steps: {}",
                self._agent_log_tag(),
                self.name,
                request.query,
                getattr(config, "model", "adapted-model"),
                getattr(config, "temperature", None),
                self.max_steps,
            )
            async for event in agentscope_agent.reply_stream(inputs):
                await self._handle_event(event)
        self.check_cancelled()
        return await self._finalize_result(request)
