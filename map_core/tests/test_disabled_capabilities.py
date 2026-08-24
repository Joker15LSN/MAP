"""P0-SEC-01 acceptance tests: host-execution capabilities disabled.

Verifies the security stopgap required by the golden taskbook:
- in-process ``python_exec_tool`` / ``bash_tool`` are removed from the
  production registry (no host exec path),
- their names stay *known* so legacy scene configs keep validating,
- any execution attempt resolves to a stable ``CAPABILITY_DISABLED`` result,
- the underlying host-exec implementations are physically gone.
"""

from __future__ import annotations

import asyncio
import importlib.util

from map_core.service.agent.disabled_capabilities import (
    CAPABILITY_DISABLED_CODE,
    CAPABILITY_DISABLED_ERROR,
    build_capability_disabled_result,
    is_disabled_capability,
)
from map_core.service.agent.fallback_agent_configs import (
    build_general_assistant_fallback_config,
)
from map_core.service.agent.tool_executor import ToolExecutor, classify_tool_result
from map_core.service.agent.tool_registry import (
    build_tool_registry,
    find_invalid_tool_names,
    list_registered_tool_names,
)
from map_core.service.agent.tool_runtime import ToolSet
from map_core.service.agentscope2.agent import AgentScopeSceneAgent

DISABLED_TOOLS = [
    "python_exec_tool",
    "bash_tool",
    # review R-02: model-controlled local file IO is closed until it is
    # served by OpenSandbox + the private artifact store.
    "attachment_file_read_tool",
    "attachment_file_write_tool",
]


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeLLMConfig()


def _build_executor() -> tuple[ToolExecutor, AgentScopeSceneAgent]:
    owner = AgentScopeSceneAgent(
        llm=FakeLLM(),
        name="TestAgent",
        system_prompt="test",
        additional_user_prompt="",
        tools=[],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    executor = ToolExecutor(
        owner=owner,
        toolset=ToolSet(),
        tools_timeout=5.0,
        log_tag_getter=lambda: "[TestAgent AGENT]",
    )
    return executor, owner


def test_registry_excludes_host_exec_tools() -> None:
    registry = build_tool_registry(llm=FakeLLM())
    for name in DISABLED_TOOLS:
        assert name not in registry, f"{name} must not be registered"
        assert name not in list_registered_tool_names()


def test_host_exec_implementations_physically_removed() -> None:
    for module_name in (
        "map_core.service.agent.python_exec_tool",
        "map_core.service.agent.bash_tool",
    ):
        spec = importlib.util.find_spec(module_name)
        assert spec is None, f"{module_name} must be physically removed"


def test_disabled_names_stay_known_for_legacy_configs() -> None:
    # Legacy scene configs referencing these names must keep validating;
    # enforcement happens at execution time (stable CAPABILITY_DISABLED).
    assert find_invalid_tool_names(DISABLED_TOOLS) == []
    assert find_invalid_tool_names(["does_not_exist_tool"]) == ["does_not_exist_tool"]


def test_is_disabled_capability_membership() -> None:
    for name in DISABLED_TOOLS:
        assert is_disabled_capability(name)
    assert not is_disabled_capability("web_search_agent")
    assert not is_disabled_capability("")


def test_execution_returns_stable_capability_disabled() -> None:
    executor, _owner = _build_executor()

    async def run(tool_name: str):
        return await executor.execute_tool(
            tool_name=tool_name,
            parid="p-1",
            args={"code": "print('must not run')"},
            request=None,  # type: ignore[arg-type]
            step_index=0,
            tool_call_id="call-1",
        )

    for name in DISABLED_TOOLS:
        result = asyncio.run(run(name))
        assert result == build_capability_disabled_result(name)
        assert result["error"] == CAPABILITY_DISABLED_ERROR
        assert result["code"] == CAPABILITY_DISABLED_CODE


def test_disabled_result_fails_closed_in_span_classifier() -> None:
    success, reason = classify_tool_result(build_capability_disabled_result("bash_tool"))
    assert success is False
    assert reason == CAPABILITY_DISABLED_ERROR


def test_unknown_tool_behavior_unchanged() -> None:
    executor, _owner = _build_executor()

    async def run():
        return await executor.execute_tool(
            tool_name="totally_unknown_tool",
            parid="p-1",
            args={},
            request=None,  # type: ignore[arg-type]
            step_index=0,
        )

    result = asyncio.run(run())
    assert result == {"error": "[TestAgent AGENT] Tool 'totally_unknown_tool' not found"}


def test_fallback_config_does_not_recommend_bash_tool() -> None:
    config = build_general_assistant_fallback_config()
    assert "bash_tool" not in config.tool_names
    assert "bash_tool" not in config.prompt
