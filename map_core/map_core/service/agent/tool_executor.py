from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Sequence
from uuid import uuid4

from loguru import logger
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from ...schema.tool_extra_result_schema import ToolExtraResultSchema
from ...utils.llm_trace_context import llm_trace_context
from ..tool_extra_result_collector import ToolExtraResultCollector
from .base import AgentRequest, BaseAgent, ExecutionResult
from .disabled_capabilities import (
    build_capability_disabled_result,
    is_disabled_capability,
)
from .skill_policy_checker import SkillPolicyChecker
from .tool_runtime import AgentTool, ToolSet
from .traceable_agent import TraceableAgent

TOOL_DISPLAY_NAME_MAP: dict[str, str] = {
    "general_qa_agent": "通用问答",
    "efficiency_pi_agent": "效率派",
    "web_search_agent": "互联网检索",
    "zhiwen_agent": "智问",
    "wenshu_agent": "问数",
    "industry_chat_agent": "工业亿问",
    "search_mounted_kb_agent": "知识库检索",
    "ask_database_agent": "问表",
    "search_uploaded_file": "上传文件检索",
}
TOOL_QUERY_PREVIEW_LENGTH = 30


def classify_tool_result(result: Any) -> tuple[bool, str]:
    """Unified tool outcome check shared by both engines.

    Returns ``(success, reason)``. Failure covers policy denials, timeouts,
    caught-exception error dicts and ``ExecutionResult(success=False)`` so
    that TOOL span status, ``map.tool.success`` and ``ToolChunk.state``
    always agree.
    """
    if isinstance(result, ExecutionResult):
        if not result.success or result.error:
            return False, str(result.error or "tool returned success=False")
        return True, ""
    if isinstance(result, dict):
        error = result.get("error")
        if error:
            if result.get("code") == "tool_forbidden":
                return False, f"policy denied: {result.get('reason') or error}"
            return False, str(error)
        return True, ""
    return True, ""


class ToolExecutor:
    def __init__(
        self,
        *,
        owner: TraceableAgent,
        toolset: ToolSet,
        tools_timeout: float,
        terminate_tool_name: str = "terminate",
        log_tag_getter: Callable[[], str] | None = None,
        result_log_preview_builder: Callable[[Any], Any] | None = None,
        tool_observation_builder: Callable[..., dict[str, Any]] | None = None,
        action_emitter: (Callable[..., Awaitable[None]] | None) = None,
    ) -> None:
        self.owner = owner
        self.toolset = toolset
        self.tools_timeout = tools_timeout
        self.terminate_tool_name = terminate_tool_name
        self.log_tag_getter = log_tag_getter or (lambda: f"[{owner.name} AGENT]")
        self.result_log_preview_builder = result_log_preview_builder or (lambda x: x)
        self.tool_observation_builder = tool_observation_builder
        self.action_emitter = action_emitter

    async def _emit_action(
        self,
        *,
        action: str,
        step_index: int | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.action_emitter is None:
            return
        await self.action_emitter(
            action=action,
            step=step_index,
            message=message,
            payload=payload or {},
        )

    @staticmethod
    def _resolve_tool_display_name(tool_name: str) -> str:
        return TOOL_DISPLAY_NAME_MAP.get(tool_name, tool_name)

    @staticmethod
    def _preview_tool_query(query: Any) -> str:
        if query is None:
            return ""
        return str(query).strip()[:TOOL_QUERY_PREVIEW_LENGTH]

    def _resolve_tool_query_preview(
        self,
        *,
        tool: Any,
        args: dict[str, Any],
        request: AgentRequest,
        tool_request: AgentRequest | None,
    ) -> str:
        if isinstance(tool, AgentTool) and tool_request is not None:
            return self._preview_tool_query(tool_request.query)

        query = args.get("query")
        if query is None or str(query).strip() == "":
            query = tool_request.query if tool_request is not None else request.query
        return self._preview_tool_query(query)

    async def execute_tool(
        self,
        *,
        tool_name: str,
        parid: str,
        args: dict[str, Any],
        request: AgentRequest,
        step_index: int,
        tool_call_id: str | None = None,
        span_attributes: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool wrapped in an OTel TOOL span (zero-cost when off).

        ToolExecutor is the single TOOL-span owner for both the legacy and
        AgentScope engines; callers (e.g. MapToolAdapter) may contribute
        engine-specific attributes via ``span_attributes`` but must not
        create their own TOOL spans.
        """
        check_cancelled = getattr(self.owner, "check_cancelled", None)
        if check_cancelled is not None:
            check_cancelled()
        tracer = otel_trace.get_tracer("map.tool")
        attributes: dict[str, Any] = {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "map.tool.step": step_index,
            "map.agent.name": str(self.owner.name or ""),
        }
        if span_attributes:
            attributes.update(span_attributes)
        span = tracer.start_span(f"tool {tool_name}", attributes=attributes)
        if tool_call_id:
            span.set_attribute("map.tool.call_id", tool_call_id)
        token = otel_context.attach(otel_trace.set_span_in_context(span))
        try:
            result = await self._execute_tool_impl(
                tool_name=tool_name,
                parid=parid,
                args=args,
                request=request,
                step_index=step_index,
                tool_call_id=tool_call_id,
            )
            success, reason = classify_tool_result(result)
            span.set_attribute("map.tool.success", success)
            if not success:
                span.set_status(Status(StatusCode.ERROR, reason))
            return result
        except asyncio.CancelledError:
            # Outer wait_for() timeouts surface here as cancellation (Python
            # 3.11+ CancelledError is a BaseException); the span must still
            # be marked failed before the cancellation propagates.
            span.set_attribute("map.tool.success", False)
            span.set_status(Status(StatusCode.ERROR, "tool cancelled or timed out"))
            raise
        except Exception as exc:  # pragma: no cover - impl catches internally
            span.record_exception(exc)
            span.set_attribute("map.tool.success", False)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            otel_context.detach(token)
            span.end()

    async def _execute_tool_impl(
        self,
        *,
        tool_name: str,
        parid: str,
        args: dict[str, Any],
        request: AgentRequest,
        step_index: int,
        tool_call_id: str | None = None,
    ) -> Any:
        log_tag = self.log_tag_getter()
        # P0-SEC-01: host-execution capabilities are globally disabled until
        # served by OpenSandbox. Fail closed with a stable result so callers
        # never fall through to a host-exec path.
        if is_disabled_capability(tool_name):
            disabled_result = build_capability_disabled_result(tool_name)
            logger.warning(
                f"{log_tag}[Step {step_index}] Tool '{tool_name}' is disabled: CAPABILITY_DISABLED"
            )
            await self._emit_action(
                action="tool_result",
                step_index=step_index,
                message=f"工具“{tool_name}”已被禁用（CAPABILITY_DISABLED）",
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": disabled_result,
                },
            )
            self.owner.record_tool_result(
                tool_name=tool_name,
                result=disabled_result,
                step_index=step_index,
                meta={
                    "tool_id": tool_call_id,
                    "error": "CAPABILITY_DISABLED",
                    "code": disabled_result.get("code"),
                },
            )
            return disabled_result

        tool = self.toolset.resolve(tool_name)
        if tool is None:
            logger.warning(
                f"{log_tag} Tool '{tool_name}' not found at step {step_index}"
            )
            return {"error": f"{log_tag} Tool '{tool_name}' not found"}

        policy_verdict = SkillPolicyChecker.evaluate(
            request=request,
            agent_code=self.owner.name,
            tool_name=tool_name,
            action="execute",
        )
        if not bool(policy_verdict.get("allowed", True)):
            denied_result = SkillPolicyChecker.denied_result(
                tool_name=tool_name,
                agent_code=self.owner.name,
                reason=str(policy_verdict.get("reason") or "tool_forbidden"),
                auth_context=policy_verdict,
            )
            logger.warning(
                f"{log_tag}[Step {step_index}] Tool '{tool_name}' denied by skill policy"
            )
            await self._emit_action(
                action="tool_result",
                step_index=step_index,
                message=f"工具“{tool_name}”无权限执行",
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": denied_result,
                    "policy": policy_verdict,
                },
            )
            self.owner.record_tool_result(
                tool_name=tool_name,
                result=denied_result,
                step_index=step_index,
            )
            return denied_result

        logger.debug(
            f"{log_tag}[Step {step_index}] Calling tool: {tool_name} with args: {args}"
        )
        started = time.time()
        tool_meta: dict[str, Any] = {}
        meta: dict[str, Any] = {"tool_id": tool_call_id}
        agent_instance: BaseAgent | None = None
        tool_request: AgentRequest | None = None

        try:
            if isinstance(tool, AgentTool):
                agent_instance, tool_request, tool_meta = tool.prepare_invocation(
                    args,
                    request,
                    caller_agent_name=self.owner.name,
                )
                meta = {**tool_meta, "tool_id": tool_call_id}
            else:
                tool_request = request.model_copy()
                # Isolate the per-call extra so concurrent tool calls within a
                # step never race on a shared dict.
                tool_request.extra = dict(tool_request.extra)
                if self.owner.name:
                    tool_request.extra["caller_agent_name"] = self.owner.name

            # S4-01: carry the per-tool-call durable identity (step + invocation)
            # so the sandbox tool can fail closed on a complete identity chain.
            tool_request.extra["step_id"] = f"step-{step_index}"
            if tool_call_id:
                tool_request.extra["invocation_id"] = str(tool_call_id)

            tool_display_name = self._resolve_tool_display_name(tool_name)
            tool_query_preview = self._resolve_tool_query_preview(
                tool=tool,
                args=args,
                request=request,
                tool_request=tool_request,
            )
            await self._emit_action(
                action="tool_call",
                step_index=step_index,
                message=(
                    f"正在调用工具“{tool_display_name}”："
                    # f"正在调用工具“{tool_display_name}”（{tool_name}）："
                    f"{tool_query_preview}"
                ),
                payload={
                    "tool_name": tool_name,
                    "tool_display_name": tool_display_name,
                    "tool_call_id": tool_call_id,
                    "query": tool_query_preview,
                    "args": args,
                },
            )
            self.owner.record_tool_call(
                tool_name=tool_name,
                tool_args=args,
                step_index=step_index,
                meta=meta,
            )

            if (
                isinstance(tool, AgentTool)
                and agent_instance is not None
                and tool_request is not None
            ):
                if (
                    isinstance(agent_instance, TraceableAgent)
                    and self.owner.state_store is not None
                    and self.owner.state_id is not None
                ):
                    agent_instance.set_execution_context(
                        self.owner.state_store,
                        self.owner.state_id,
                    )
                with llm_trace_context(
                    state_store=self.owner.state_store,
                    state_id=self.owner.state_id,
                    agent_code=self.owner.name,
                    agent_name=self.owner.agent_display_name,
                    component=tool_name,
                    phase="tool_internal",
                    step=step_index,
                    call_kind="tool_internal_llm",
                ):
                    result = await tool._run_prepared(agent_instance, tool_request, parid)
            else:
                with llm_trace_context(
                    state_store=self.owner.state_store,
                    state_id=self.owner.state_id,
                    agent_code=self.owner.name,
                    agent_name=self.owner.agent_display_name,
                    component=tool_name,
                    phase="tool_internal",
                    step=step_index,
                    call_kind="tool_internal_llm",
                ):
                    result = await tool.run(args, tool_request, parid)

            logger.info(
                f"{log_tag}[Step {step_index}] Tool '{tool_name}' executed successfully"
            )
            logger.debug(
                f"{log_tag}[Step {step_index}] Tool '{tool_name}' result: {self.result_log_preview_builder(result)}"
            )
            result = self._collect_and_strip_tool_extra_result(
                request=request,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step_index=step_index,
                result=result,
            )
            await self._emit_action(
                action="tool_result",
                step_index=step_index,
                message="工具执行完成",
                # message=f"工具“{tool_name}”执行完成",
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": self.result_log_preview_builder(result),
                },
            )
            self.owner.record_tool_result(
                tool_name=tool_name,
                result=result,
                step_index=step_index,
                meta={**meta, "duration_s": time.time() - started},
            )
            return result
        except Exception as exc:
            logger.error(
                f"{log_tag}[Step {step_index}] Tool '{tool_name}' execution failed: {exc}",
                exc_info=True,
            )
            error_result = {"error": str(exc), "tool": tool_name}
            await self._emit_action(
                action="tool_result",
                step_index=step_index,
                message=f"工具“{tool_name}”执行失败",
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": error_result,
                },
            )
            self.owner.record_tool_result(
                tool_name=tool_name,
                result=error_result,
                step_index=step_index,
                meta={
                    **meta,
                    "duration_s": time.time() - started,
                    "error": str(exc),
                },
            )
            return error_result

    async def execute_concurrently(
        self,
        *,
        tool_calls: Sequence[Any],
        request: AgentRequest,
        step: int,
        parid: str,
    ) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
        log_tag = self.log_tag_getter()

        async def _run_tool_call(
            call_obj, args: dict[str, Any]
        ) -> tuple[Any, Any, dict[str, Any]]:
            tool_name = call_obj.function.name
            # Re-enter the step span inside the worker task so the TOOL span
            # becomes a child of the enclosing agent step context.
            with otel_trace.use_span(otel_trace.get_current_span()):
                result = await self.execute_tool(
                    tool_name=tool_name,
                    parid=parid,
                    args=args,
                    request=request,
                    step_index=step,
                    tool_call_id=call_obj.id,
                )
            return call_obj, result, args

        active_calls = [
            (call, self._parse_tool_args(call.function.arguments))
            for call in tool_calls
            if call.function.name != self.terminate_tool_name
        ]
        if not active_calls:
            logger.debug(
                f"{log_tag}[Step {step}] No executable tool calls after filtering"
            )
            return {}, False, []

        tasks: dict[
            asyncio.Task[tuple[Any, Any, dict[str, Any]]], tuple[Any, dict[str, Any]]
        ] = {
            asyncio.create_task(_run_tool_call(call, args)): (call, args)
            for call, args in active_calls
        }

        results: dict[str, Any] = {}
        tool_called = False

        done, pending = await asyncio.wait(tasks.keys(), timeout=self.tools_timeout)
        for task in done:
            try:
                call_obj, result, _args = task.result()
                results[call_obj.id] = result
                tool_called = True
            except Exception as exc:
                logger.error(
                    f"{log_tag}[Step {step}] Tool execution failed: {exc}",
                    exc_info=True,
                )

        if pending:
            logger.warning(
                f"{log_tag}[Step {step}] Tool execution timed out after {self.tools_timeout}s"
            )
            for task in pending:
                call_obj, _args = tasks[task]
                task.cancel()
                timeout_result = {
                    "error": "tool timeout",
                    "tool": call_obj.function.name,
                    "timeout": self.tools_timeout,
                }
                await self._emit_action(
                    action="tool_result",
                    step_index=step,
                    message=f"工具“{call_obj.function.name}”执行超时",
                    payload={
                        "tool_name": call_obj.function.name,
                        "tool_call_id": call_obj.id,
                        "result": timeout_result,
                    },
                )
                self.owner.record_tool_result(
                    tool_name=call_obj.function.name,
                    result=timeout_result,
                    step_index=step,
                    meta={
                        "tool_id": call_obj.id,
                        "duration_s": self.tools_timeout,
                        "error": "tool timeout",
                    },
                )
                results[call_obj.id] = timeout_result

            await asyncio.gather(*pending, return_exceptions=True)

        observations: list[dict[str, Any]] = []
        if self.tool_observation_builder is not None:
            observations = [
                self.tool_observation_builder(
                    step_index=step,
                    tool_name=call.function.name,
                    tool_call_id=call.id,
                    args=args,
                    result=results[call.id],
                )
                for call, args in active_calls
                if call.id in results
            ]

        return results, tool_called, observations

    @staticmethod
    def _parse_tool_args(raw_args: str | dict[str, Any] | None) -> dict[str, Any]:
        if raw_args is None:
            return {}
        if isinstance(raw_args, dict):
            return raw_args
        if not isinstance(raw_args, str):
            return {"value": raw_args}

        import json

        try:
            return json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            return {"raw": raw_args}

    @staticmethod
    def _collect_and_strip_tool_extra_result(
        *,
        request: AgentRequest,
        tool_name: str,
        tool_call_id: str | None,
        step_index: int,
        result: Any,
    ) -> Any:
        raw_extra = None
        item_id = None
        if isinstance(result, ExecutionResult):
            raw_extra = result.extra_result
            if isinstance(result.meta_data, dict):
                item_id = result.meta_data.get("id")
        elif isinstance(result, dict):
            raw_extra = result.get("extra_result")
            item_id = result.get("id")
        else:
            return result

        if not isinstance(raw_extra, dict):
            return result

        request_extra = getattr(request, "extra", None)
        if isinstance(request_extra, dict):
            collector = request_extra.get("tool_extra_result_collector")
            if isinstance(collector, ToolExtraResultCollector):
                collector.add(
                    ToolExtraResultSchema(
                        id=str(item_id or tool_call_id or uuid4().hex),
                        tool=tool_name,
                        tool_call_id=tool_call_id,
                        step=step_index,
                        extra_result=raw_extra,
                    )
                )

        if isinstance(result, ExecutionResult):
            return result.model_copy(update={"extra_result": None})

        sanitized = dict(result)
        sanitized.pop("extra_result", None)
        return sanitized
