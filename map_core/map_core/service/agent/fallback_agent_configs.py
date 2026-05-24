from __future__ import annotations

from ...schema.scene_agent_config_schema import SceneAgentConfig


def build_general_assistant_fallback_config() -> SceneAgentConfig:
    return SceneAgentConfig(
        prompt=(
            "你是一个有用的助手。你应该调用工具(同时调用多次工具)，尽最大努力来检索信息，回答用户提问。"
            "检索用户上传的文件使用search_uploaded_file工具，进行网络搜索使用web_search_agent工具，执行命令行使用bash_tool工具。bash_tool无法访问互联网。"
            "你应该参考剩余的step来规划工具的调用，合理安排工具调用的顺序和次数，直到你认为已经获取到足够的信息来回答用户问题为止。"
            "多次调用工具指南：你必须一次性输出多个tool_calls（数组长度≥2），禁止分多轮串行调。"
        ),
        tool_names=[
            "web_search_agent",
            "search_uploaded_file",
        ],
        max_steps=1,
        description="通用agent助手, 耐心热情地回答用户提问",
        force_tool_call=False,
    )


def get_fallback_scene_agent_configs() -> dict[str, SceneAgentConfig]:
    return {
        "General_Assistant": build_general_assistant_fallback_config(),
    }
