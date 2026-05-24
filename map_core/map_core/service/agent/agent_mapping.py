from __future__ import annotations

from ...schema.scene_agent_config_schema import (
    SceneAgentConfig,
    ScenePostSummaryConfig,
)
from ...schema.scene_registry import SUB_SCENES


def _build_default_scene_agent_config() -> SceneAgentConfig:
    return SceneAgentConfig(
        prompt=(
            "你是场景智能助手。你必须调用工具来回答用户问题，"
            "并在需要时结合工具结果给出简洁结论。你需要拆分原始问题，"
            "分别调用工具，并最终整合工具结果。"
        ),
        additional_user_prompt="",
        tool_names=[
            "efficiency_pi_agent",
            "annual_performance_agent",
            "ask_database_agent",
            "wenshu_agent",
            "web_search_agent",
            "industry_chat_agent",
        ],
        max_steps=2,
        description="默认场景智能体",
        force_tool_call=True,
    )


SCENE_AGENT_CONFIGS: dict[str, SceneAgentConfig] = {
    agent_code: _build_default_scene_agent_config() for agent_code in SUB_SCENES
}
