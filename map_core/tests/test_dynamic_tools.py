from __future__ import annotations

import asyncio

from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_registry import find_invalid_tool_names
from map_core.service.dynamic_tools import (
    build_mcp_tools,
    build_prompt_skill_tools,
    mcp_tool_runtime_name,
    skill_runtime_tool_name,
)
from map_core.utils.model_invocation import (
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelUsage,
)


class FakeLLM:
    async def invoke(self, req: ModelInvocationRequest) -> ModelInvocationOutcome:
        system_prompt = ""
        user_prompt = ""
        for message in req.messages:
            role = getattr(message, "role", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            content = getattr(message, "content", None) or (
                message.get("content") if isinstance(message, dict) else None
            )
            if role == "system":
                system_prompt = str(content or "")
            elif role == "user":
                user_prompt = str(content or "")
        return ModelInvocationOutcome(
            status="succeeded",
            content=f"{system_prompt[:10]}::{user_prompt[:10]}",
            usage=ModelUsage(total_tokens=3),
            attempts=1,
            latency_ms=0.0,
        )


def test_dynamic_tool_names_are_policy_valid() -> None:
    assert mcp_tool_runtime_name("finance-api", "quote") == "mcp__server_finance_api__tool_quote"
    assert skill_runtime_tool_name("ops.skill.v1") == "skill__skill_ops.skill.v1"
    assert find_invalid_tool_names(["mcp__server_finance_api__tool_quote", "skill__skill_ops"]) == []


def test_prompt_skill_tool_executes_with_skill_content() -> None:
    tools = build_prompt_skill_tools(
        skills=[
            {
                "skill_id": "ops.summary",
                "tool_name": "skill__ops_summary",
                "display_name": "Ops Summary",
                "content": "请按运营口径总结。",
                "status": "active",
            }
        ],
        descriptors=[],
        llm=FakeLLM(),
    )
    result = asyncio.run(
        tools["skill__ops_summary"].run(
            {"query": "为什么调用变慢？"},
            AgentRequest(query="fallback", staff_code="pytest"),
            "-",
        )
    )

    assert result.name == "skill__ops_summary"
    assert result.data_source["source"] == "prompt_skill"
    assert result.data_source["usage"]["total_tokens"] == 3


def test_mcp_tool_builder_respects_enabled_flags() -> None:
    tools = build_mcp_tools(
        [
            {
                "server_id": "demo",
                "enabled": True,
                "transport": "streamable_http",
                "url": "http://127.0.0.1:9/mcp",
                "tools": [
                    {"name": "enabled_tool", "enabled": True},
                    {"name": "disabled_tool", "enabled": False},
                ],
            },
            {
                "server_id": "disabled",
                "enabled": False,
                "tools": [{"name": "hidden", "enabled": True}],
            },
        ]
    )

    assert list(tools) == ["mcp__server_demo__tool_enabled_tool"]
