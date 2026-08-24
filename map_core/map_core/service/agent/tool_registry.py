from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from ...utils.llm_engine import LLMEngine
from .annual_performance_agent import AnnualPerformanceAgent
from .ask_database_agent import AskDatabaseAgent
from .base import BaseAgent
from .disabled_capabilities import (
    DISABLED_HOST_EXEC_CAPABILITIES,
    build_capability_disabled_result,
    is_disabled_capability,
)
from .efficiency_pi_agent import EfficiencyPiAgent
from .general_qa_agent import GeneralQAAgent
from .industry_chat_agent import IndustryChatAgent
from .kb_tools import (
    MountedKBSearchAgent,
    create_query_kb_chunk_tool,
    create_search_kb_chunk_tool,
    create_search_uploaded_file_chunk_tool,
)
from .tool_runtime import AgentTool, Tool
from .traceable_agent import TraceableAgent
from .web_search_agent import WebSearchAgent
from .wenshu_agent import WenshuAgent
from .zhiwen_agent import ZhiwenAgent

# P0-SEC-01 re-exports (kept here for import-compatibility with existing
# callers); the canonical definitions live in ``disabled_capabilities``
# because ``tool_executor`` must import them without an import cycle.
# ``DISABLED_HOST_EXEC_CAPABILITIES`` / ``is_disabled_capability`` /
# ``build_capability_disabled_result`` are available as attributes of this
# module.


@dataclass(frozen=True)
class ToolRegistration:
    spec_provider: Callable[[], dict[str, Any]]
    agent_factory: Callable[[LLMEngine], BaseAgent]


def _build_tool(registration: ToolRegistration, llm: LLMEngine) -> AgentTool:
    spec = registration.spec_provider()
    return AgentTool(
        name=spec["name"],
        description=spec["description"],
        parameters=spec.get("parameters"),
        agent_factory=lambda: registration.agent_factory(llm),
    )


def _tool_registrations() -> list[ToolRegistration]:
    return [
        ToolRegistration(
            spec_provider=EfficiencyPiAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: EfficiencyPiAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=AnnualPerformanceAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: AnnualPerformanceAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=AskDatabaseAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: AskDatabaseAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=WenshuAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: WenshuAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=ZhiwenAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: ZhiwenAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=WebSearchAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: WebSearchAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=IndustryChatAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: IndustryChatAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=GeneralQAAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: GeneralQAAgent(llm=runtime_llm),
        ),
        ToolRegistration(
            spec_provider=MountedKBSearchAgent.get_tool_spec,
            agent_factory=lambda runtime_llm: MountedKBSearchAgent(llm=runtime_llm),
        ),
    ]


def _standalone_tools() -> dict[str, Tool]:
    # P0-SEC-01 (review R-02): attachment_file_read_tool and
    # attachment_file_write_tool are removed from the production registry
    # (host file IO) and stay known-but-disabled via
    # DISABLED_HOST_EXEC_CAPABILITIES; any invocation fails closed in
    # ToolExecutor before touching the filesystem.
    return {
        create_query_kb_chunk_tool().name: create_query_kb_chunk_tool(),  # TODO other strategy to align name and tool
        create_search_uploaded_file_chunk_tool().name: create_search_uploaded_file_chunk_tool(),
        create_search_kb_chunk_tool().name: create_search_kb_chunk_tool(),
    }


def list_registered_tool_agent_names() -> list[str]:
    return [
        registration.spec_provider()["name"] for registration in _tool_registrations()
    ]


def list_registered_tool_names() -> list[str]:
    names = list_registered_tool_agent_names()
    names.extend(_standalone_tools().keys())
    # S3-01: the sandbox execution capability is part of the single
    # capability/tool schema - scenario configs may legally declare it
    # (lazy import avoids a registry <-> sandbox_tools import cycle).
    from ...service.sandbox_tools import SANDBOX_TOOL_NAME

    names.append(SANDBOX_TOOL_NAME)
    return names


def find_invalid_tool_agent_names(tool_names: Sequence[str] | None) -> list[str]:
    if not tool_names:
        return []

    valid_tool_names = set(list_registered_tool_agent_names())
    return sorted(
        {tool_name for tool_name in tool_names if tool_name not in valid_tool_names}
    )


def find_invalid_tool_names(tool_names: Sequence[str] | None) -> list[str]:
    if not tool_names:
        return []

    valid_tool_names = set(list_registered_tool_names()) | DISABLED_HOST_EXEC_CAPABILITIES
    return sorted(
        {
            tool_name
            for tool_name in tool_names
            if tool_name not in valid_tool_names
            and not str(tool_name).startswith(("mcp__", "skill__"))
        }
    )


def validate_tool_names(tool_names: Sequence[str] | None) -> None:
    invalid_tool_names = find_invalid_tool_names(tool_names)
    if not invalid_tool_names:
        return

    valid_tool_names = ", ".join(list_registered_tool_names())
    invalid_values = ", ".join(invalid_tool_names)
    raise ValueError(
        f"Unknown tool_names: {invalid_values}. Allowed values: {valid_tool_names}"
    )


def get_registered_agent_tool(
    tool_registry: Mapping[str, Tool],
    tool_name: str,
) -> AgentTool:
    tool = tool_registry.get(tool_name)
    if tool is None:
        raise ValueError(f"Unknown tool agent: {tool_name}")
    if not isinstance(tool, AgentTool):
        raise ValueError(f"Tool '{tool_name}' is not a traceable tool agent")

    probe_agent = tool._agent_factory() if tool._agent_factory is not None else None
    if not isinstance(probe_agent, TraceableAgent):
        raise ValueError(f"Tool '{tool_name}' is not a traceable tool agent")
    return tool


def build_tool_registry(llm: LLMEngine) -> dict[str, Tool]:
    registrations = _tool_registrations()
    standalone_tools = _standalone_tools()

    registry: dict[str, Tool] = {}
    for registration in registrations:
        tool = _build_tool(registration, llm)
        if tool.name in registry:
            raise ValueError(f"Duplicate tool registration name: {tool.name}")
        registry[tool.name] = tool

    duplicate_names = sorted(set(registry).intersection(standalone_tools))
    if duplicate_names:
        duplicates = ", ".join(duplicate_names)
        raise ValueError(f"Duplicate tool registration name: {duplicates}")

    registry.update(standalone_tools)
    return registry
