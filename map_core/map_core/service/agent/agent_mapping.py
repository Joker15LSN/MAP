from __future__ import annotations

from ...schema.scene_agent_config_schema import (
    SceneAgentConfig,
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
            "general_qa_agent",
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


def _build_general_assistant_scene_agent_config() -> SceneAgentConfig:
    return SceneAgentConfig(
        prompt=(
            "你是通用问答助手，优先使用通用问答工具输出可直接阅读的中文结论。"
            "如果用户问题需要事实解释，请按要点分条回答。"
        ),
        additional_user_prompt="",
        tool_names=[
            "general_qa_agent",
            "web_search_agent",
        ],
        max_steps=1,
        description="通用问答场景智能体",
        force_tool_call=True,
    )


SCENE_AGENT_CONFIGS: dict[str, SceneAgentConfig] = {
    agent_code: _build_default_scene_agent_config() for agent_code in SUB_SCENES
}
SCENE_AGENT_CONFIGS["General_Assistant"] = _build_general_assistant_scene_agent_config()
