import datetime
import inspect
import json
from typing import Any, Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from loguru import logger

from ...schema.agent_schema import Function, ToolCall
from ...utils.global_context import agent_log_context
from ...utils.llm_trace_context import llm_trace_context
from ...utils.model_invocation import (
    ModelInvocation,
    ModelInvocationOutcome,
    ModelInvocationRequest,
)
from ..prompt.tool_call_prompt import (
    NEXT_STEP_PROMPT,
    SCENE_POST_SUMMARY_SYSTEM_PROMPT,  # noqa: F401  # public re-export seam
    SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE,  # noqa: F401  # public re-export seam
    SYSTEM_PROMPT,
    UPLOADED_KB_FILE_SYSTEM_PROMPT_TEMPLATE,
)
from ..state_store import safe_serialize
from .base import AgentActionEvent, AgentRequest, AgentResult, ExecutionResult
from .tool_call_exit import ScenePostSummaryRuntimeConfig, ToolCallExitHandler
from .tool_call_session import ToolCallSession
from .tool_executor import ToolExecutor
from .tool_runtime import (  # noqa: F401  # public re-export seam
    AgentTool,
    RuntimeSchemaTool,
    Tool,
    ToolSet,
)
from .traceable_agent import TraceableAgent


class _OutcomeView:
    """Private view over ``ModelInvocationOutcome`` for the legacy agent loop."""

    def __init__(self, outcome: ModelInvocationOutcome) -> None:
        self.content: str = outcome.content or ""
        self.tool_calls: list[ToolCall] = []
        for call in outcome.tool_calls or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            self.tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    function=Function(
                        name=str(function.get("name") or ""),
                        arguments=str(function.get("arguments") or ""),
                    ),
                )
            )
        self.model: str | None = outcome.model
        self.usage: dict[str, int] | None = (
            outcome.usage.to_dict() if outcome.usage else None
        )
        self.finish_reason: str | None = outcome.finish_reason
        self.response_time: float = outcome.latency_ms / 1000.0
        self.request_id: str | None = outcome.request_id
        self.raw: dict[str, Any] | None = outcome.raw


class ToolCallAgent(TraceableAgent):
    TERMINATE_TOOL_NAME = "terminate"

    def __init__(
        self,
        llm: ModelInvocation,
        *,
        name: str = "tool_call_agent",
        aid: str | None = None,
        parid: str = "-",
        system_prompt: str | None = None,
        additional_user_prompt: str | None = None,
        toolset: ToolSet | None = None,
        max_steps: int = 4,
        force_tool_call: bool = False,
        tools_timeout: float = 120.0,
        scene_post_summary: ScenePostSummaryRuntimeConfig | None = None,
        request_preprocessor: (
            Callable[[AgentRequest], AgentRequest | Awaitable[AgentRequest]] | None
        ) = None,
        action_handler: (
            Callable[[AgentActionEvent], Any | Awaitable[Any]] | None
        ) = None,
    ) -> None:
        super().__init__(llm, name=name, aid=aid or None)  # use parent init for aid
        self.parid = parid
        self.toolset = toolset or ToolSet()
        self.max_steps = max_steps
        self.force_tool_call = force_tool_call
        self.tools_timeout = tools_timeout
        self.scene_post_summary = scene_post_summary
        self.system_prompt = system_prompt
        self.additional_user_prompt = additional_user_prompt
        self.request_preprocessor = request_preprocessor
        self.action_handler = action_handler
        self.tool_executor = ToolExecutor(
            owner=self,
            toolset=self.toolset,
            tools_timeout=self.tools_timeout,
            terminate_tool_name=self.TERMINATE_TOOL_NAME,
            log_tag_getter=self._agent_log_tag,
            result_log_preview_builder=self._build_tool_result_log_preview,
            tool_observation_builder=self._build_tool_observation,
            action_emitter=self._emit_action_event,
        )
        self.exit_handler = ToolCallExitHandler(
            owner=self,
            terminate_tool_name=self.TERMINATE_TOOL_NAME,
            force_tool_call=self.force_tool_call,
            scene_post_summary=self.scene_post_summary,
            log_tag_getter=self._agent_log_tag,
            parse_tool_args=self._parse_tool_args,
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

    def _normalize_history(self, history: Sequence[Any] | None) -> list[dict[str, Any]]:
        if not history:
            return []
        normalized: list[dict[str, Any]] = []
        for msg in history:
            if hasattr(msg, "to_dict") and callable(msg.to_dict):
                serialized = msg.to_dict()
                if isinstance(serialized, dict):
                    normalized.append(dict(serialized))
                else:
                    normalized.append({"role": "user", "content": str(serialized)})
            elif isinstance(msg, dict):
                normalized.append(dict(msg))
            else:
                normalized.append({"role": "user", "content": str(msg)})
        return normalized

    def _parse_tool_args(self, raw_args: str | dict[str, Any] | None) -> dict[str, Any]:
        return ToolExecutor._parse_tool_args(raw_args)

    async def preprocess_request(
        self, request: AgentRequest, *, parid: str = "-"
    ) -> AgentRequest:
        request = await super().preprocess_request(request, parid=parid)
        if self.request_preprocessor is None:
            return request

        prepared = self.request_preprocessor(request)
        if isinstance(prepared, AgentRequest):
            return prepared
        return await prepared

    def _llm_tool_choice(self, tool_called: bool) -> str | None:
        """Soft force tool call parameter"""
        if self.force_tool_call and not tool_called:
            return "required"
        return None

    def _scene_post_summary_log_meta(self) -> dict[str, Any]:
        return self.exit_handler.scene_post_summary_log_meta()

    def _scene_post_summary_enabled(self) -> bool:
        return self.exit_handler.scene_post_summary_enabled()

    def _list_non_terminate_tool_names(
        self, request: AgentRequest | None = None
    ) -> list[str]:
        return [
            tool.name
            for tool in self.toolset.enabled_tools(request)
            if tool.name != self.TERMINATE_TOOL_NAME
        ]

    def _prepare_request_for_execution(self, request: AgentRequest) -> AgentRequest:
        return self.exit_handler.prepare_request_for_execution(request)

    def _build_tool_observation(
        self,
        *,
        step_index: int,
        tool_name: str,
        tool_call_id: str,
        args: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        """For independent llm summarization of agent execution conclusion"""
        return {
            "step": step_index,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_args": safe_serialize(args),
            "tool_result": self._extract_result_for_llm_context(result),
        }

    def _parse_execution_result_like_dict(self, result: Any) -> dict[str, Any] | None:
        if isinstance(result, ExecutionResult):
            return result.model_dump(exclude_none=True)
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, str):
            raw = result.strip()
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            payload = parsed
        else:
            return None

        if "content" not in payload and "data_source" not in payload:
            return None
        return payload

    def _extract_result_for_llm_context(self, result: Any) -> Any:
        payload = self._parse_execution_result_like_dict(result)
        if payload is None:
            return safe_serialize(result)

        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if "data_source" in payload:
            return safe_serialize(payload.get("data_source"))
        return safe_serialize(payload)

    async def run(self, request: AgentRequest, *, parid: str = "-") -> AgentResult:
        self.parid = parid
        with agent_log_context(self.agent_id, parent_id=parid):
            logger.info(
                f"{self._agent_log_tag()} ToolCallAgent '{self.name}' starting with query: {request.query}"
            )
            return await self._run_with_context(request)

    def _build_next_step_prompt(self, step: int | None = None) -> str:
        if step is None:
            return NEXT_STEP_PROMPT
        return (
            f"{NEXT_STEP_PROMPT} "
            f"Current step: {step + 1}/{self.max_steps}."
        )

    def _format_system_prompt(
        self, request: AgentRequest, *, step: int | None = None
    ) -> str:
        """Format the system prompt safely with request context."""
        extra = request.extra
        uploaded_kb_files = extra.get("uploaded_kb_files", None)
        uploaded_kb_file_prompt = ""
        # If there are uploaded user files, include their IDs and names in the system prompt to inform the agent about available resources.
        if uploaded_kb_files:
            uploaded_file_id_names = "\n".join(
                [
                    f"{uploaded_kb_file['file_id']}: {uploaded_kb_file['file_name']}"
                    for uploaded_kb_file in uploaded_kb_files
                ]
            )
            uploaded_kb_file_prompt = UPLOADED_KB_FILE_SYSTEM_PROMPT_TEMPLATE.format(
                kb_file_id_and_names=uploaded_file_id_names
            )
        system_parts = [
            SYSTEM_PROMPT,
            self.system_prompt.strip() if self.system_prompt else None,
            uploaded_kb_file_prompt if uploaded_kb_file_prompt else None,
            self._build_next_step_prompt(step),
        ]
        system_prompt = "\n\n".join([p for p in system_parts if p])
        try:
            return system_prompt.format(
                staff_code=request.staff_code,
                current_datetime=datetime.datetime.now(ZoneInfo("Asia/Shanghai")),
            )
        except Exception as exc:
            logger.warning(
                f"{self._agent_log_tag()} System prompt format failed, using raw prompt: {exc}"
            )
            return system_prompt

    def _append_tool_message(
        self, messages: list[dict[str, Any]], call_id: str, payload: Any
    ) -> None:
        """Append a tool result message to the conversation."""
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(
                    self._serialize_tool_result(payload), ensure_ascii=False
                ),
            }
        )

    def _serialize_tool_result(self, result: Any) -> Any:
        """Store full tool output payload in runtime events and tool history."""
        if isinstance(result, ExecutionResult):
            return safe_serialize(result.model_dump(exclude_none=True))
        return safe_serialize(result)

    def _build_tool_result_log_preview(self, result: Any) -> Any:
        """Build a lightweight tool result preview for logs and action events."""
        payload = self._serialize_tool_result(result)
        if not isinstance(payload, dict):
            return payload

        preview: dict[str, Any] = {}
        for key in ("success", "name", "content", "error"):
            if key in payload:
                preview[key] = payload[key]

        data_source = payload.get("data_source")
        if isinstance(data_source, dict):
            preview["data_source"] = self._build_data_source_log_preview(data_source)
        elif data_source is not None:
            preview["data_source"] = self._basic_log_stats(data_source)

        return preview

    @classmethod
    def _build_data_source_log_preview(
        cls, data_source: dict[str, Any]
    ) -> dict[str, Any]:
        preview: dict[str, Any] = {}
        if "source" in data_source:
            preview["source"] = data_source["source"]
        for key, value in data_source.items():
            if key == "source":
                continue
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

    def _create_session(self, request: AgentRequest) -> ToolCallSession:
        return ToolCallSession.from_request(
            request_query=request.query,
            history=request.history,
            history_normalizer=self._normalize_history,
            additional_user_prompt=self.additional_user_prompt,
        )

    def _record_assistant_tool_calls(
        self, messages: list[dict[str, Any]], response: Any
    ) -> None:
        """Record assistant tool calls for continuity in the conversation."""
        ToolCallSession(messages=messages).append_assistant_tool_calls(response)

    def _resolve_terminate_content(
        self,
        messages: list[dict[str, Any]],
        response: Any | None,
    ) -> str:
        """Resolve final content for terminate path with robust fallbacks."""
        direct_content = (
            (response.content if response is not None else "") or ""
        ).strip()
        if direct_content:
            return direct_content

        return ToolCallSession(messages=messages).latest_non_empty_content()

    def _build_data_source(
        self,
        *,
        source: str,
        messages: list[dict[str, Any]],
        step: int | None = None,
    ) -> dict[str, Any]:
        """Build data source payload with source marker and full agent history."""
        payload: dict[str, Any] = {
            "source": source,
            "history": self._sanitize_history_for_data_source(messages),
        }
        if step is not None:
            payload["step"] = step
        return payload

    def _sanitize_history_for_data_source(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Sanitize history for storage without truncating tool payloads."""
        sanitized: list[dict[str, Any]] = []
        for msg in messages:
            item = dict(msg)
            sanitized.append(item)
        return safe_serialize(sanitized)

    def _final_llm_result(
        self, response: Any, messages: list[dict[str, Any]], step: int | None = None
    ) -> AgentResult:
        """Build a final AgentResult from a plain LLM response."""
        return AgentResult(
            name=self.name,
            content=(response.content or "").strip(),
            data_source=self._build_data_source(
                source="llm",
                messages=messages,
                step=step,
            ),
        )

    async def _run_tool_calls_concurrently(
        self,
        tool_calls: Sequence[Any],
        request: AgentRequest,
        step: int,
    ) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
        return await self.tool_executor.execute_concurrently(
            tool_calls=tool_calls,
            request=request,
            step=step,
            parid=self.agent_id,
        )

    async def _run_with_context(self, request: AgentRequest) -> AgentResult:
        self.check_cancelled()
        request = self._prepare_request_for_execution(request)
        session = self._create_session(request)
        tools = self.toolset.to_openai_tools(
            request,
            owner_agent_name=self.name,
        )
        non_terminate_tool_names = self._list_non_terminate_tool_names(request)
        logger.debug(
            f"{self._agent_log_tag()} Available tools: {[t.get('function', {}).get('name') for t in tools]}"
        )
        if not tools:
            logger.warning(f"{self._agent_log_tag()} No tools enabled for this request")
            return AgentResult(
                success=False,
                name=self.name,
                content="",
                error="No tools enabled for this request.",
            )

        if self._scene_post_summary_enabled() and not non_terminate_tool_names:
            logger.warning(
                f"{self._agent_log_tag()} Scene post-summary is enabled for '{self.name}', but no executable tools are configured besides terminate. If the model replies directly without calling terminate, scene post-summary will not run"
            )

        for step in range(self.max_steps):
            # await asyncio.sleep(random.uniform(2, 6))
            formatted_system_prompt = self._format_system_prompt(request, step=step)

            await self._emit_action_event(
                action="step_start",
                step=step,
                message=f"第 {step + 1} 步：选择下一步动作",
            )
            logger.debug(
                f"{self._agent_log_tag()}[Step {step}/{self.max_steps}] Calling LLM for tool selection"
            )
            self.check_cancelled()
            with llm_trace_context(
                state_store=self.state_store,
                state_id=self.state_id,
                agent_code=self.name,
                agent_name=self.agent_display_name,
                component=self.name,
                phase="sub_agent_tool_selection",
                step=step,
                call_kind="tool_selection",
            ):
                all_messages = [
                    {"role": "system", "content": formatted_system_prompt},
                    *session.messages,
                ]
                outcome = await self.llm.invoke(
                    ModelInvocationRequest(
                        messages=all_messages,
                        tools=tools,
                        tool_choice=self._llm_tool_choice(session.tool_called) or "auto",
                    )
                )
                outcome.raise_for_status()
                response = _OutcomeView(outcome)
            self.check_cancelled()
            self._accumulate_usage(response.usage)
            logger.debug(
                "{}[Step {}/{}] Tool-selection LLM completed in {:.3f}s with finish_reason={} tool_calls={} request_id={!r}".format(
                    self._agent_log_tag(),
                    step,
                    self.max_steps,
                    response.response_time,
                    response.finish_reason,
                    len(response.tool_calls or []),
                    response.request_id,
                )
            )

            tool_calls = response.tool_calls or []
            if not tool_calls:
                final_result = await self.exit_handler.handle_final_answer(
                    response=response,
                    step=step,
                    request=request,
                    session=session,
                )
                return final_result

            await self._emit_action_event(
                action="tool_calls_selected",
                step=step,
                message=f"第 {step + 1} 步：已选择 {len(tool_calls)} 个工具调用",
                payload={
                    "tool_calls": [
                        {
                            "tool_name": call.function.name,
                            "tool_call_id": call.id,
                            "args": self._parse_tool_args(call.function.arguments),
                        }
                        for call in tool_calls
                    ]
                },
            )
            session.append_assistant_tool_calls(response)

            terminate_result = await self.exit_handler.maybe_handle_terminate(
                tool_calls=tool_calls,
                response=response,
                step=step,
                request=request,
                session=session,
            )
            if terminate_result:
                return terminate_result

            results, called, observations = await self._run_tool_calls_concurrently(
                tool_calls, request, step
            )
            session.mark_tool_called(called)
            session.add_tool_observations(observations)

            if not results:
                return self._final_llm_result(response, session.messages, step=step)

            session.append_tool_results(
                tool_calls=tool_calls,
                results=results,
                terminate_tool_name=self.TERMINATE_TOOL_NAME,
                serializer=self._serialize_tool_result,
            )
            await self._emit_action_event(
                action="step_complete",
                step=step,
                message=f"第 {step + 1} 步：已完成 {len(results)} 个工具调用",
                payload={"tool_call_ids": list(results.keys())},
            )

        logger.warning(
            f"{self._agent_log_tag()} ToolCallAgent '{self.name}' reached max steps ({self.max_steps})"
        )
        await self._emit_action_event(
            action="max_steps",
            step=self.max_steps,
            message=f"已达到最大步骤数（{self.max_steps}）",
            payload={"max_steps": self.max_steps},
        )
        result = await self.exit_handler.finalize(
            request=request,
            response=None,
            session=session,
            step=self.max_steps,
            source="max_steps",
            exit_reason="max_steps",
            extra_exit_metadata={"max_steps": self.max_steps},
        )
        if session.tool_called or (
            isinstance(result.content, str) and bool(result.content.strip())
        ):
            result.success = True
            result.error = None
            return result
        return AgentResult(
            success=False,
            name=result.name,
            content=result.content,
            error="Max tool-call steps reached without any successful tool execution.",
            data_source=result.data_source,
            meta_data=result.meta_data,
        )
