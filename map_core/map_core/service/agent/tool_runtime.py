from __future__ import annotations

import inspect
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, override

from ...utils.global_context import agent_log_context
from ...utils.serialization import safe_serialize
from .base import AgentRequest, AgentResult, BaseAgent, ExecutionResult, ToolResult
from .traceable_agent import TraceableAgent


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: (
        Callable[[dict[str, Any], AgentRequest, str], Awaitable[Any] | Any] | None
    ) = None

    async def run(self, args: dict[str, Any], request: AgentRequest, parid: str) -> Any:
        if self.handler is None:
            raise RuntimeError(f"Tool '{self.name}' has no handler")
        result = self.handler(args, request, parid)
        if inspect.isawaitable(result):
            result = await result
        return self.normalize_result(result)

    def normalize_result(self, result: Any) -> Any:
        if isinstance(result, ExecutionResult):
            if isinstance(result, AgentResult):
                return result
            if isinstance(result, ToolResult) and result.name:
                return result
            if isinstance(result, ToolResult):
                return result.model_copy(update={"name": self.name})
            return result

        if hasattr(result, "model_dump") and callable(result.model_dump):
            payload = safe_serialize(result.model_dump())
            if isinstance(payload, Mapping) and (
                "content" in payload or "data_source" in payload
            ):
                return ToolResult(name=self.name, **payload)
            return ToolResult(name=self.name, data_source={"data": payload})

        if isinstance(result, str):
            return ToolResult(name=self.name, content=result)

        payload = safe_serialize(result)
        if isinstance(payload, Mapping):
            if "content" in payload or "data_source" in payload:
                return ToolResult(name=self.name, **dict(payload))

            extra_result = payload.get("extra_result")
            data_source = dict(payload)
            if isinstance(extra_result, Mapping):
                data_source.pop("extra_result", None)
            else:
                extra_result = None
            return ToolResult(
                name=self.name,
                data_source=data_source,
                extra_result=safe_serialize(extra_result)
                if isinstance(extra_result, Mapping)
                else None,
            )
        return ToolResult(name=self.name, data_source={"data": payload})

    @staticmethod
    def _resolve_caller_agent_name(
        agent_request: AgentRequest | None, owner_agent_name: str | None
    ) -> str | None:
        if isinstance(owner_agent_name, str) and owner_agent_name.strip():
            return owner_agent_name.strip()
        if agent_request is None:
            return None
        extra = getattr(agent_request, "extra", None)
        if not isinstance(extra, Mapping):
            return None
        legacy_name = extra.get("caller_agent_name")
        if isinstance(legacy_name, str) and legacy_name.strip():
            return legacy_name.strip()
        return None

    @staticmethod
    def _normalize_dynamic_description(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            lines = [
                f"- {str(key).strip()}: {str(item).strip()}"
                for key, item in value.items()
                if str(key).strip() and str(item).strip()
            ]
            return "\n".join(lines).strip()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = [str(item).strip() for item in value if str(item).strip()]
            return "\n".join(f"- {item}" for item in items).strip()
        return str(value).strip() if value is not None else ""

    def _resolve_runtime_description(
        self,
        agent_request: AgentRequest | None,
        *,
        base_description: str,
        owner_agent_name: str | None = None,
    ) -> str:
        if agent_request is None:
            return base_description
        extra = getattr(agent_request, "extra", None)
        if not isinstance(extra, Mapping):
            return base_description
        tool_context = extra.get("tool_context")
        if not isinstance(tool_context, Mapping):
            return base_description

        resolved_owner_agent_name = self._resolve_caller_agent_name(
            agent_request,
            owner_agent_name,
        )
        dynamic_description: Any = None

        top_level_tool_context = tool_context.get(self.name)
        if isinstance(top_level_tool_context, Mapping):
            dynamic_description = top_level_tool_context.get("description")

        if resolved_owner_agent_name:
            owner_context = tool_context.get(resolved_owner_agent_name)
            if isinstance(owner_context, Mapping):
                nested_tool_context = owner_context.get(self.name)
                if isinstance(nested_tool_context, Mapping):
                    nested_description = nested_tool_context.get("description")
                    if nested_description is not None:
                        dynamic_description = nested_description

        normalized_description = self._normalize_dynamic_description(dynamic_description)
        if not normalized_description:
            return base_description

        # return f"{base_description}\n\n挂载数据包括：\n{normalized_description[:2000]}"
        return f"{base_description}"

    def to_openai_tool(
        self,
        agent_request: AgentRequest | None,
        owner_agent_name: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._resolve_runtime_description(
                    agent_request=agent_request,
                    base_description=self.description,
                    owner_agent_name=owner_agent_name,
                ),
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class RuntimeSchemaTool(Tool):
    @abstractmethod
    def build_function_name(self, agent_request: AgentRequest | None) -> str: ...

    @abstractmethod
    def build_function_description(self, agent_request: AgentRequest | None) -> str: ...

    @abstractmethod
    def build_function_parameters(
        self, agent_request: AgentRequest | None
    ) -> dict[str, Any]: ...

    @override
    def to_openai_tool(
        self,
        agent_request: AgentRequest | None,
        owner_agent_name: str | None = None,
    ) -> dict[str, Any]:
        base_description = self.build_function_description(agent_request=agent_request)
        return {
            "type": "function",
            "function": {
                "name": self.build_function_name(agent_request=agent_request),
                "description": self._resolve_runtime_description(
                    agent_request=agent_request,
                    base_description=base_description,
                    owner_agent_name=owner_agent_name,
                ),
                "parameters": self.build_function_parameters(
                    agent_request=agent_request
                )
                or {"type": "object", "properties": {}},
            },
        }


class ToolSet:
    def __init__(
        self,
        tools: Sequence[Tool] | None = None,
        *,
        include_terminate: bool = False,
    ) -> None:
        self._tools: dict[str, Tool] = {tool.name: tool for tool in (tools or [])}
        if include_terminate and "terminate" not in self._tools:
            self._tools["terminate"] = Tool(
                name="terminate",
                description="Stop tool interaction and return final answer.",
                parameters={"type": "object", "properties": {}},
                handler=lambda _args, _request, _parid: "terminated",
            )

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def resolve(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def enabled_tools(self, request: AgentRequest | None = None) -> list[Tool]:
        if request is None:
            return self.list_tools()

        enabled = None
        if request.extra:
            enabled = request.extra.get("enabled_tools")
        if enabled is None:
            return self.list_tools()
        enabled_set = {str(name) for name in enabled}
        if "terminate" in self._tools:
            enabled_set.add("terminate")
        return [tool for tool in self._tools.values() if tool.name in enabled_set]

    def to_openai_tools(
        self,
        request: AgentRequest | None = None,
        *,
        owner_agent_name: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            tool.to_openai_tool(request, owner_agent_name=owner_agent_name)
            for tool in self.enabled_tools(request)
        ]


class AgentTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        agent_factory: Callable[[], BaseAgent] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
        )
        self._agent_factory = agent_factory

    def _prepare_invocation(
        self,
        args: dict[str, Any],
        request: AgentRequest,
        caller_agent_name: str | None = None,
    ) -> tuple[BaseAgent, AgentRequest, dict[str, Any]]:
        if self._agent_factory is None:
            raise RuntimeError(f"Tool '{self.name}' has no agent factory")
        agent = self._agent_factory()

        query = args.get("query", request.query)
        staff_code = args.get("staff_code", request.staff_code)
        extra = dict(request.extra or {})
        summarize = args.get("summarize", request.summarize)
        original_query = request.original_query or request.query
        for key, value in args.items():
            if key not in {"query", "staff_code", "summarize"}:
                extra[key] = value
        if caller_agent_name:
            extra["caller_agent_name"] = caller_agent_name
        extra.setdefault("original_query", original_query)

        tool_request = AgentRequest(
            query=query,
            original_query=original_query,
            staff_code=staff_code,
            history=request.history,
            scene_result=request.scene_result,
            summarize=summarize,
            extra=extra,
        )
        meta = {"tool_id": agent.agent_id, "tool_name": agent.name}
        return agent, tool_request, meta

    def prepare_invocation(
        self,
        args: dict[str, Any],
        request: AgentRequest,
        caller_agent_name: str | None = None,
    ) -> tuple[BaseAgent, AgentRequest, dict[str, Any]]:
        return self._prepare_invocation(
            args=args,
            request=request,
            caller_agent_name=caller_agent_name,
        )

    def prepare_traceable_invocation(
        self,
        args: dict[str, Any],
        request: AgentRequest,
        caller_agent_name: str | None = None,
    ) -> tuple[TraceableAgent, AgentRequest, dict[str, Any]]:
        agent, tool_request, meta = self._prepare_invocation(
            args=args,
            request=request,
            caller_agent_name=caller_agent_name,
        )
        if not isinstance(agent, TraceableAgent):
            raise ValueError(f"Tool '{self.name}' is not a traceable tool agent")
        return agent, tool_request, meta

    async def _run_prepared(
        self, agent: BaseAgent, tool_request: AgentRequest, parid: str
    ) -> Any:
        with agent_log_context(agent.agent_id, parent_id=parid):
            result = await agent.execute(tool_request, parid=parid)
            if isinstance(result, AsyncGenerator):
                chunks: list[str] = []
                async for chunk in result:
                    chunks.append(str(chunk))
                return self.normalize_result("".join(chunks))
            if isinstance(result, ExecutionResult):
                return result
            if hasattr(result, "model_dump") and callable(result.model_dump):
                return self.normalize_result(result)
            return self.normalize_result(result)

    async def run(self, args: dict[str, Any], request: AgentRequest, parid: str) -> Any:
        agent, tool_request, _meta = self._prepare_invocation(args, request)
        return await self._run_prepared(agent, tool_request, parid)

    async def run_with_meta(
        self, args: dict[str, Any], request: AgentRequest, parid: str
    ) -> tuple[Any, dict[str, Any]]:
        agent, tool_request, meta = self._prepare_invocation(args, request)
        result = await self._run_prepared(agent, tool_request, parid)
        return result, meta
