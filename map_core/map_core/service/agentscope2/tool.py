from __future__ import annotations

import asyncio
import json
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

from ...utils.serialization import safe_serialize
from ..agent.base import ExecutionResult
from ..agent.tool_executor import classify_tool_result
from ..agent.tool_runtime import Tool


def extract_result_for_llm_context(result: Any) -> Any:
    if isinstance(result, ExecutionResult):
        payload: Any = result.model_dump(exclude_none=True)
    elif isinstance(result, dict):
        payload = result
    elif isinstance(result, str):
        raw = result.strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return result
    else:
        return safe_serialize(result)

    if not isinstance(payload, dict) or not any(
        key in payload for key in ("content", "data_source", "notices")
    ):
        return safe_serialize(payload)
    content = payload.get("content")
    notices = payload.get("notices")
    if isinstance(notices, list) and notices:
        value: dict[str, Any] = {"notices": safe_serialize(notices)}
        if isinstance(content, str) and content.strip():
            value["content"] = content.strip()
        return value
    if isinstance(content, str) and content.strip():
        return content.strip()
    if "data_source" in payload:
        return safe_serialize(payload.get("data_source"))
    return safe_serialize(payload)


class MapToolAdapter(ToolBase):
    """Run an existing request-scoped MAP Tool as an AgentScope ToolBase.

    All governance (SkillPolicyChecker) and extra-result collection live in
    ``ToolExecutor.execute_tool``; routing calls through it keeps the new
    engine behaviour identical to the legacy path.
    """

    is_concurrency_safe = True
    is_read_only = False
    is_external_tool = False
    is_state_injected = False

    def __init__(
        self,
        *,
        tool: Tool,
        function_schema: dict[str, Any],
        owner: Any,
        request: Any,
    ) -> None:
        super().__init__()
        function = function_schema.get("function") or {}
        self.name = str(function.get("name") or tool.name)
        self.description = str(function.get("description") or tool.description)
        parameters = function.get("parameters")
        self.input_schema = (
            parameters
            if isinstance(parameters, dict)
            else {"type": "object", "properties": {}}
        )
        self.map_tool = tool
        self.owner = owner
        self.request = request

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Allowed by MAP tool governance.",
            decision_reason="The MAP ToolExecutor owns authorization (SkillPolicyChecker).",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        # ToolExecutor.execute_tool is the single TOOL-span owner; the
        # adapter only contributes AgentScope-specific span attributes.
        return await self._call_impl(**kwargs)

    def _span_attributes(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "map.engine": "agentscope",
            "map.agent.code": str(getattr(self.owner, "name", "unknown")),
            "map.tool.argument_count": len(kwargs),
        }
        request_extra = getattr(self.request, "extra", {})
        if isinstance(request_extra, dict):
            for source_key, attribute_key in (
                ("request_id", "map.request.id"),
                ("session_id", "session.id"),
            ):
                value = request_extra.get(source_key)
                if value not in (None, ""):
                    attributes[attribute_key] = str(value)
        return attributes

    async def _call_impl(self, **kwargs: Any) -> ToolChunk:
        step = self.owner.current_step
        call_id = self.owner.claim_tool_call_id(self.name, step)
        state = ToolResultState.SUCCESS
        try:
            result = await asyncio.wait_for(
                self.owner.tool_executor.execute_tool(
                    tool_name=self.map_tool.name,
                    parid=self.owner.agent_id,
                    args=dict(kwargs),
                    request=self.request,
                    step_index=step,
                    tool_call_id=call_id,
                    span_attributes=self._span_attributes(kwargs),
                ),
                timeout=self.owner.tools_timeout,
            )
            success, _reason = classify_tool_result(result)
            if not success:
                state = ToolResultState.ERROR
        except TimeoutError:
            state = ToolResultState.ERROR
            result = {
                "error": "tool timeout",
                "tool": self.map_tool.name,
                "timeout": self.owner.tools_timeout,
            }
            await self.owner.record_tool_timeout(
                tool_name=self.map_tool.name,
                tool_call_id=call_id,
                step=step,
                result=result,
            )

        self.owner.register_tool_result(
            step=step,
            tool_name=self.name,
            tool_call_id=call_id,
            args=dict(kwargs),
            result=result,
        )
        serialized = (
            safe_serialize(result.model_dump(exclude_none=True))
            if isinstance(result, ExecutionResult)
            else safe_serialize(result)
        )
        output = (
            serialized
            if isinstance(serialized, str)
            else json.dumps(serialized, ensure_ascii=False)
        )
        return ToolChunk(
            content=[TextBlock(text=output)],
            state=state,
            metadata={"map_tool_name": self.map_tool.name},
        )
