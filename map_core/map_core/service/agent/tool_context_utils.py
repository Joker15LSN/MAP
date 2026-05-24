from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import AgentRequest


def _copy_request_extra(request: AgentRequest) -> dict[str, Any]:
    return dict(request.extra or {})


def _resolve_tool_context_container(extra: Mapping[str, Any]) -> Mapping[str, Any] | None:
    tool_context = extra.get("tool_context")
    if isinstance(tool_context, Mapping):
        return tool_context
    return None


def resolve_agent_tool_context_overlay(
    request: AgentRequest,
    *,
    agent_name: str,
    include_top_level_agent_context: bool = True,
    include_caller_nested_agent_context: bool = True,
) -> dict[str, Any]:
    """Resolve agent-specific tool_context overlays from request.extra.

    Merge priority is low -> high, matching the source iteration order:
    1. tool_context.<agent_name>
    2. tool_context.<caller_agent_name>.<agent_name>
    """
    extra = _copy_request_extra(request)
    tool_context = _resolve_tool_context_container(extra)
    caller_agent_name = extra.get("caller_agent_name")
    merged: dict[str, Any] = {}

    if tool_context is None:
        return merged

    if include_top_level_agent_context:
        agent_context = tool_context.get(agent_name)
        if isinstance(agent_context, Mapping):
            merged.update(agent_context)

    if include_caller_nested_agent_context:
        if isinstance(caller_agent_name, str) and caller_agent_name.strip():
            caller_context = tool_context.get(caller_agent_name)
            if isinstance(caller_context, Mapping):
                nested_agent_context = caller_context.get(agent_name)
                if isinstance(nested_agent_context, Mapping):
                    merged.update(nested_agent_context)

    return merged


def merge_extra_with_agent_tool_context_defaults(
    request: AgentRequest,
    *,
    agent_name: str,
    include_top_level_agent_context: bool = False,
    include_caller_nested_agent_context: bool = True,
) -> dict[str, Any]:
    """Return request.extra merged with tool_context defaults.

    Existing request.extra keys always win. Resolved tool_context values only fill
    in missing keys, which matches agents that treat nested tool_context as a
    defaults source rather than a strict overlay source.
    """
    extra = _copy_request_extra(request)
    overlay = resolve_agent_tool_context_overlay(
        request,
        agent_name=agent_name,
        include_top_level_agent_context=include_top_level_agent_context,
        include_caller_nested_agent_context=include_caller_nested_agent_context,
    )
    for key, value in overlay.items():
        extra.setdefault(key, value)
    return extra
