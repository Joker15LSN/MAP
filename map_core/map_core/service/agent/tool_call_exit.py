from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Sequence
from zoneinfo import ZoneInfo

from loguru import logger

from ...schema.state_event_schema import AgentEventSchema
from ...utils.llm_engine import LLMEngine
from ..prompt.tool_call_prompt import (
    SCENE_POST_SUMMARY_SYSTEM_PROMPT,
    SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE,
)
from ..state_store import safe_serialize
from .base import AgentRequest, AgentResult
from .tool_call_session import ToolCallSession
from .traceable_agent import TraceableAgent


@dataclass(frozen=True)
class ScenePostSummaryRuntimeConfig:
    llm: LLMEngine
    system_prompt: str = SCENE_POST_SUMMARY_SYSTEM_PROMPT
    user_prompt_template: str = SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE


class ToolCallExitHandler:
    def __init__(
        self,
        *,
        owner: TraceableAgent,
        terminate_tool_name: str,
        force_tool_call: bool,
        scene_post_summary: ScenePostSummaryRuntimeConfig | None,
        log_tag_getter: Callable[[], str],
        parse_tool_args: Callable[[str | dict[str, Any] | None], dict[str, Any]],
        build_data_source: Callable[..., dict[str, Any]],
        resolve_terminate_content: Callable[[list[dict[str, Any]], Any | None], str],
        action_emitter: Callable[..., Awaitable[None]] | None,
    ) -> None:
        self.owner = owner
        self.terminate_tool_name = terminate_tool_name
        self.force_tool_call = force_tool_call
        self.scene_post_summary = scene_post_summary
        self.log_tag_getter = log_tag_getter
        self.parse_tool_args = parse_tool_args
        self.build_data_source = build_data_source
        self.resolve_terminate_content = resolve_terminate_content
        self.action_emitter = action_emitter

    async def _emit_action(
        self,
        *,
        action: str,
        step: int | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.action_emitter is None:
            return
        await self.action_emitter(
            action=action,
            step=step,
            message=message,
            payload=payload or {},
        )

    def scene_post_summary_log_meta(self) -> dict[str, Any]:
        config = self.scene_post_summary
        if config is None:
            return {"enabled": False}

        llm_model = getattr(getattr(config.llm, "config", None), "model", None)
        return {
            "enabled": True,
            "llm_model": llm_model,
            "custom_system_prompt": (
                config.system_prompt != SCENE_POST_SUMMARY_SYSTEM_PROMPT
            ),
            "custom_user_prompt_template": (
                config.user_prompt_template != SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE
            ),
        }

    def scene_post_summary_enabled(self) -> bool:
        return self.scene_post_summary is not None

    def prepare_request_for_execution(self, request: AgentRequest) -> AgentRequest:
        if not self.scene_post_summary_enabled():
            return request

        logger.info(
            f"{self.log_tag_getter()} Scene post-summary enabled for '{self.owner.name}': {self.scene_post_summary_log_meta()}"
        )
        extra = dict(request.extra or {})
        extra["_force_disable_agent_summarize"] = True
        return request.model_copy(update={"extra": extra, "summarize": False})

    def _format_scene_post_summary_user_prompt(
        self,
        *,
        request: AgentRequest,
        tool_observations: list[dict[str, Any]],
        exit_metadata: dict[str, Any],
    ) -> str:
        if self.scene_post_summary is None:
            return ""

        payload = {
            "query": request.query,
            "tool_observations": safe_serialize(tool_observations),
            "tool_observations_json": json.dumps(
                safe_serialize(tool_observations), ensure_ascii=False, indent=2
            ),
            "terminate_metadata": safe_serialize(exit_metadata),
            "terminate_metadata_json": json.dumps(
                safe_serialize(exit_metadata), ensure_ascii=False, indent=2
            ),
            "exit_metadata": safe_serialize(exit_metadata),
            "exit_metadata_json": json.dumps(
                safe_serialize(exit_metadata), ensure_ascii=False, indent=2
            ),
        }
        try:
            return self.scene_post_summary.user_prompt_template.format(**payload)
        except Exception as exc:
            logger.warning(
                f"{self.log_tag_getter()} Scene post-summary user prompt format failed, using default template: {exc}"
            )
            return SCENE_POST_SUMMARY_USER_PROMPT_TEMPLATE.format(**payload)

    def _record_agent_conclusion(
        self,
        *,
        content: str,
        source: str,
        step: int,
        exit_metadata: dict[str, Any],
        tool_observation_count: int,
        data_source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        status: Literal["success", "failed"] = "success",
    ) -> None:
        self.owner.record_agentic_event(
            "agent_conclusion",
            AgentEventSchema(
                category="agent",
                component=self.owner.name,
                stage="end",
                status=status,
                data={
                    "agent_code": self.owner.name,
                    "agent_name": self.owner.agent_display_name,
                    "content": content,
                    "source": source,
                    "step": step,
                    "exit": safe_serialize(exit_metadata),
                    "has_content": bool(content.strip()),
                    "tool_observation_count": tool_observation_count,
                    "data_source": safe_serialize(data_source or {"source": source}),
                    "meta": safe_serialize(meta or {}),
                },
            ).model_dump(),
        )

    async def _run_scene_post_summary(
        self,
        *,
        request: AgentRequest,
        session: ToolCallSession,
        step: int,
        exit_metadata: dict[str, Any],
    ) -> AgentResult:
        if self.scene_post_summary is None:
            return AgentResult(
                name=self.owner.name,
                content=self.resolve_terminate_content(session.messages, None),
                data_source=self.build_data_source(
                    source="tool_terminate",
                    messages=session.messages,
                    step=step,
                ),
            )

        user_prompt = self._format_scene_post_summary_user_prompt(
            request=request,
            tool_observations=session.tool_observations,
            exit_metadata=exit_metadata,
        )
        messages = [
            {
                "role": "system",
                "content": self.scene_post_summary.system_prompt,
            },
            {"role": "user", "content": user_prompt},
        ]
        start_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        logger.info(
            "{}[Step {}] Invoking scene post-summary LLM for '{}' with {} tool observations and exit metadata {}".format(
                self.log_tag_getter(),
                step,
                self.owner.name,
                len(session.tool_observations),
                {
                    "reason": exit_metadata.get("reason"),
                    "tool_call_id": exit_metadata.get("tool_call_id"),
                    "step": exit_metadata.get("step"),
                },
            )
        )
        try:
            response = await self.scene_post_summary.llm.ainvoke(messages)
        except Exception:
            logger.exception(
                f"{self.log_tag_getter()}[Step {step}] Scene post-summary LLM call failed for '{self.owner.name}'"
            )
            raise
        self.owner._accumulate_usage(response.usage)
        summary_content = (response.content or "").strip()
        end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        logger.info(
            "{}[Step {}] Scene post-summary LLM completed for '{}' in {:.3f}s with usage {} request_id={!r}".format(
                self.log_tag_getter(),
                step,
                self.owner.name,
                getattr(response, "response_time", 0.0),
                response.usage or {},
                getattr(response, "request_id", None),
            )
        )

        data_source = self.build_data_source(
            source="scene_post_summary",
            messages=session.messages,
            step=step,
        )

        result = AgentResult(
            name=self.owner.name,
            content=summary_content,
            exit=safe_serialize(exit_metadata),
            data_source=data_source,
            tool_observations=safe_serialize(session.tool_observations),
        )
        self._record_agent_conclusion(
            content=summary_content,
            source="scene_post_summary",
            step=step,
            exit_metadata=exit_metadata,
            tool_observation_count=len(session.tool_observations),
            data_source={"source": "scene_post_summary"},
            meta={
                "duration_s": (end_ts - start_ts).total_seconds(),
                "token_usage": response.usage or {},
                "llm_request_id": getattr(response, "request_id", None),
                "llm_response_time": getattr(response, "response_time", None),
                "exit_reason": exit_metadata.get("reason"),
                "scene_post_summary": self.scene_post_summary_log_meta(),
            },
        )
        return result

    async def finalize(
        self,
        *,
        request: AgentRequest,
        response: Any | None,
        session: ToolCallSession,
        step: int,
        source: str,
        exit_reason: str,
        extra_exit_metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        exit_metadata = {
            "reason": exit_reason,
            "step": step,
            "had_tool_calls": bool(session.tool_observations),
        }
        if extra_exit_metadata:
            exit_metadata.update(safe_serialize(extra_exit_metadata))

        if self.scene_post_summary_enabled():
            logger.info(
                f"{self.log_tag_getter()}[Step {step}] Triggering scene post-summary for '{self.owner.name}' on exit reason '{exit_reason}'"
            )
            return await self._run_scene_post_summary(
                request=request,
                session=session,
                step=step,
                exit_metadata=exit_metadata,
            )

        content = ""
        if response is not None:
            content = (response.content or "").strip()
        elif not session.tool_called:
            content = session.latest_non_empty_content()
        if source == "tool_terminate":
            content = self.resolve_terminate_content(session.messages, response)

        data_source = self.build_data_source(
            source=source,
            messages=session.messages,
            step=step,
        )

        result = AgentResult(
            name=self.owner.name,
            content=content,
            exit=safe_serialize(exit_metadata),
            data_source=data_source,
            tool_observations=safe_serialize(session.tool_observations)
            if session.tool_observations
            else None,
        )
        self._record_agent_conclusion(
            content=content,
            source=source,
            step=step,
            exit_metadata=exit_metadata,
            tool_observation_count=len(session.tool_observations),
            data_source={"source": source},
            meta={"exit_reason": exit_reason},
        )
        return result

    async def handle_final_answer(
        self,
        *,
        response: Any,
        step: int,
        request: AgentRequest,
        session: ToolCallSession,
    ) -> AgentResult:
        logger.debug(
            f"{self.log_tag_getter()}[Step {step}] LLM returned final answer without tool calls"
        )
        await self._emit_action(
            action="final_answer",
            step=step,
            message=f"第 {step + 1} 步：模型给出最终答案",
            payload={"assistant_content": (response.content or "").strip()},
        )

        if self.force_tool_call and not session.tool_called:
            logger.info(
                f"{self.log_tag_getter()}[Step {step}] force_tool_call is enabled but model returned a final answer without tool calls; exiting"
            )

        exit_reason = (
            "final_answer_after_tools" if session.tool_called else "final_answer"
        )
        logger.info(
            f"{self.log_tag_getter()} ToolCallAgent '{self.owner.name}' completed on exit reason '{exit_reason}'"
        )
        final_messages = session.final_messages_with_assistant(response.content or "")
        final_session = ToolCallSession(
            messages=final_messages,
            tool_called=session.tool_called,
            tool_observations=list(session.tool_observations),
        )
        return await self.finalize(
            request=request,
            response=response,
            session=final_session,
            step=step,
            source=exit_reason,
            exit_reason=exit_reason,
        )

    async def maybe_handle_terminate(
        self,
        *,
        tool_calls: Sequence[Any],
        response: Any,
        step: int,
        request: AgentRequest,
        session: ToolCallSession,
    ) -> AgentResult | None:
        for call in tool_calls:
            if call.function.name == self.terminate_tool_name:
                terminate_args = self.parse_tool_args(call.function.arguments)
                terminate_meta = {
                    "tool_id": call.id,
                    "tool_name": self.terminate_tool_name,
                }
                await self._emit_action(
                    action="terminate",
                    step=step,
                    message=f"第 {step + 1} 步：已选择终止循环",
                    payload={
                        "tool_name": self.terminate_tool_name,
                        "tool_call_id": call.id,
                        "args": safe_serialize(terminate_args),
                    },
                )
                self.owner.record_tool_call(
                    tool_name=self.terminate_tool_name,
                    tool_args=terminate_args,
                    step_index=step,
                    meta=terminate_meta,
                )
                self.owner.record_tool_result(
                    tool_name=self.terminate_tool_name,
                    result={"status": "terminated"},
                    step_index=step,
                    meta=terminate_meta,
                )
                logger.info(
                    f"{self.log_tag_getter()}[Step {step}] Terminate tool called, ending execution"
                )
                terminate_metadata = {
                    "tool_call_id": call.id,
                    "tool_args": safe_serialize(terminate_args),
                    "step": step,
                }
                return await self.finalize(
                    request=request,
                    response=response,
                    session=session,
                    step=step,
                    source="tool_terminate",
                    exit_reason="terminate",
                    extra_exit_metadata=terminate_metadata,
                )
        return None
