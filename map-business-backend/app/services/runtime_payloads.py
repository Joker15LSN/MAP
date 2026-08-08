"""Runtime payload builders.

Pure functions that materialize the runtime request payloads for
``map_core`` from the current admin state. They were extracted verbatim
from ``app.main`` during F-01 (no behavior change); the only difference is
that they now receive the store explicitly instead of closing over a
module-level instance.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from ..repositories.config import ConfigRepository
from ..schemas import (
    AdminState,
    BusinessAgentConfig,
    ChatRequest,
    ModelRecord,
)

SUPPORTED_SCENE_AGENT_CODES: set[str] = {
    "Market_Assistant",
    "Customer_Assistant",
    "Quality",
    "Ecosystem_Partner",
    "IPD_RD",
    "Engineering",
    "Supply_Chain",
    "Procurement",
    "Operations",
    "HR",
    "Company_News",
    "Park_Service",
    "Digitalization",
    "Process_Assist",
    "General_Assistant",
    "Industrial_Assistant",
    "Financial_Assistant",
}

KNOWN_TOOL_NAMES: set[str] = {
    "general_qa_agent",
    "efficiency_pi_agent",
    "annual_performance_agent",
    "ask_database_agent",
    "wenshu_agent",
    "web_search_agent",
    "industry_chat_agent",
    "search_mounted_kb_agent",
    "search_uploaded_file",
    "bash_tool",
    "python_exec_tool",
    "attachment_file_read_tool",
    "attachment_file_write_tool",
    "query_kb_chunk_tool",
    "search_uploaded_file_chunk_tool",
    "search_kb_chunk_tool",
}

TOOL_NAME_ALIASES: dict[str, str] = {
    "通用问答": "general_qa_agent",
    "效率派": "efficiency_pi_agent",
    "问表": "ask_database_agent",
    "数据库数据模型": "ask_database_agent",
    "问数": "wenshu_agent",
    "指标数据模型": "wenshu_agent",
    "互联网搜索": "web_search_agent",
    "工业亿问": "industry_chat_agent",
    "团队知识库": "search_mounted_kb_agent",
    "企业知识库": "search_mounted_kb_agent",
    "上传文件检索": "search_uploaded_file",
}


def slugify(value: str, *, prefix: str = "item") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    if not normalized:
        normalized = uuid.uuid4().hex[:8]
    return f"{prefix}-{normalized}" if not normalized.startswith(f"{prefix}-") else normalized


def resolve_large_model_row(
    state: AdminState,
    model_name: str | None,
) -> ModelRecord | None:
    rows = state.model_center.large_models or []
    normalized_name = (model_name or "").strip()
    if normalized_name:
        for row in rows:
            if row.model_name.strip() == normalized_name:
                return row

    for row in rows:
        if row.is_default:
            return row
    return rows[0] if rows else None


def dedupe_text_items(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def normalize_tool_name(raw_tool_name: str) -> str | None:
    cleaned = str(raw_tool_name).strip()
    if not cleaned:
        return None
    mapped = TOOL_NAME_ALIASES.get(cleaned, cleaned)
    if (
        mapped in KNOWN_TOOL_NAMES
        or mapped.startswith("mcp__")
        or mapped.startswith("skill__")
    ):
        return mapped
    return None


def mcp_tool_runtime_name(server_id: str, tool_name: str) -> str:
    return f"mcp__{slugify(server_id, prefix='server').replace('-', '_')}__{slugify(tool_name, prefix='tool').replace('-', '_')}"


def skill_runtime_tool_name(skill_id: str) -> str:
    return f"skill__{slugify(skill_id, prefix='skill').replace('-', '_')}"


def build_scene_selection_payload(state: AdminState) -> dict[str, Any]:
    enabled_agent_codes: dict[str, dict[str, str]] = {}
    for agent in state.business_agents:
        agent_code = agent.agent_code.strip()
        if not agent.enabled or agent_code not in SUPPORTED_SCENE_AGENT_CODES:
            continue
        if agent_code in enabled_agent_codes:
            continue
        enabled_agent_codes[agent_code] = {
            "agent_name": agent.display_name.strip() or agent_code,
            "agent_description": (
                agent.description.strip()
                or agent.prompt_template.strip()
                or agent.scene_name.strip()
                or agent_code
            ),
        }

    if "General_Assistant" not in enabled_agent_codes:
        enabled_agent_codes["General_Assistant"] = {
            "agent_name": "通用问答智能体",
            "agent_description": "通用知识问答与日常咨询。",
        }

    default_api_key = os.getenv("MAP_LLM_API_KEY", "")
    route_model_row = resolve_large_model_row(state, state.master_agent.route_model)
    summary_model_row = resolve_large_model_row(state, state.master_agent.summary_model)
    route_llm_config = (
        {
            "base_url": route_model_row.model_url.strip(),
            "api_key": default_api_key,
            "model": route_model_row.model_name.strip(),
            "temperature": state.master_agent.temperature,
            "max_tokens": state.master_agent.max_tokens,
        }
        if route_model_row is not None
        else None
    )
    summary_llm_config = (
        {
            "base_url": summary_model_row.model_url.strip(),
            "api_key": default_api_key,
            "model": summary_model_row.model_name.strip(),
            "temperature": state.master_agent.temperature,
            "max_tokens": state.master_agent.max_tokens,
        }
        if summary_model_row is not None
        else None
    )
    return {
        "enabled_agent_codes": enabled_agent_codes,
        "route_prompt": state.master_agent.route_prompt,
        "route_model": state.master_agent.route_model,
        "route_llm_config": route_llm_config,
        "summary_prompt": state.master_agent.summary_prompt,
        "summary_model": state.master_agent.summary_model,
        "summary_llm_config": summary_llm_config,
    }


def derive_agent_tool_names(state: AdminState, agent: BusinessAgentConfig) -> list[str]:
    tool_names: list[str] = [
        normalized
        for normalized in (
            normalize_tool_name(tool_name) for tool_name in (agent.tools or [])
        )
        if normalized is not None
    ]
    mcp_by_id = {server.server_id: server for server in state.mcp_servers}

    for mount in agent.resource_mounts or []:
        if not mount.enabled:
            continue
        if mount.resource_type == "builtin_tool":
            raw_name = mount.builtin_tool_name or mount.resource_id or mount.resource_name
            normalized = normalize_tool_name(raw_name)
            if normalized:
                tool_names.append(normalized)
        elif mount.resource_type == "knowledge_base":
            tool_names.append("search_mounted_kb_agent")
        elif mount.resource_type == "data_model":
            tool_names.append("ask_database_agent")
        elif mount.resource_type == "skill":
            skill_id = mount.skill_id or mount.resource_id
            if skill_id:
                tool_names.append(skill_runtime_tool_name(skill_id))
        elif mount.resource_type in {"mcp_server", "mcp_tool"}:
            server_id = mount.mcp_server_id or mount.resource_id
            server = mcp_by_id.get(server_id)
            if not server or not server.enabled:
                continue
            selected_tool_names = (
                [tool.name for tool in server.tools if tool.enabled]
                if mount.include_all_tools or mount.resource_type == "mcp_server"
                else list(mount.mcp_tool_names or [])
            )
            for tool_name in selected_tool_names:
                if tool_name:
                    tool_names.append(mcp_tool_runtime_name(server.server_id, tool_name))

    return dedupe_text_items(tool_names)


def build_runtime_resource_payload(state: AdminState) -> dict[str, Any]:
    return {
        "mcp_servers": [server.model_dump() for server in state.mcp_servers if server.enabled],
        "skills": [skill.model_dump() for skill in state.skills if skill.status == "active"],
        "flow_skill_descriptors": [
            item.model_dump() for item in state.flow_skill_descriptors if item.status == "active"
        ],
    }


def build_dispatch_config_payload(state: AdminState) -> dict[str, Any]:
    scene_agent_configs: dict[str, dict[str, Any]] = {}
    default_api_key = os.getenv("MAP_LLM_API_KEY", "")
    for agent in state.business_agents:
        agent_code = agent.agent_code.strip()
        if not agent.enabled or agent_code not in SUPPORTED_SCENE_AGENT_CODES:
            continue

        normalized_tools = derive_agent_tool_names(state, agent)
        if not normalized_tools:
            normalized_tools = ["general_qa_agent"]

        prompt = (
            (agent.prompt_config.tool_call_prompt if agent.prompt_config else "").strip()
            or (agent.prompt_config.system_prompt if agent.prompt_config else "").strip()
            or agent.prompt_template.strip()
            or "你是业务智能体，请根据用户问题给出准确、简洁、可执行的回答。"
        )
        additional_user_prompt = (
            (agent.prompt_config.user_prompt if agent.prompt_config else "").strip()
        )
        if additional_user_prompt in {"", "{query}"}:
            additional_user_prompt = ""

        model_row = resolve_large_model_row(
            state,
            (agent.prompt_config.base_model if agent.prompt_config else None)
            or agent.model,
        )
        llm_config = None
        if model_row is not None:
            llm_config = {
                "base_url": model_row.model_url.strip(),
                "api_key": default_api_key,
                "model": model_row.model_name.strip(),
                "temperature": agent.prompt_config.temperature if agent.prompt_config else 0.1,
                "max_tokens": agent.prompt_config.max_tokens if agent.prompt_config else 4096,
            }

        scene_agent_config: dict[str, Any] = {
            "prompt": prompt,
            "additional_user_prompt": additional_user_prompt,
            "tool_names": normalized_tools,
            "max_steps": 1 if agent_code == "General_Assistant" else 2,
            "description": agent.description.strip() or agent.scene_name.strip() or agent.display_name.strip() or agent_code,
            "force_tool_call": True,
            "tool_internal_prompts": [
                item.model_dump()
                for item in (agent.prompt_config.tool_internal_prompts if agent.prompt_config else [])
                if item.enabled
            ],
            "resource_mounts": [item.model_dump() for item in agent.resource_mounts],
        }
        summary_prompt = (agent.prompt_config.summary_prompt if agent.prompt_config else "").strip()
        if summary_prompt:
            scene_agent_config["scene_post_summary"] = {
                "enabled": True,
                "system_prompt": summary_prompt,
            }
        if llm_config is not None:
            scene_agent_config["llm_config"] = llm_config
        scene_agent_configs[agent_code] = scene_agent_config

    if "General_Assistant" not in scene_agent_configs:
        fallback_model = resolve_large_model_row(state, None)
        scene_agent_configs["General_Assistant"] = {
            "prompt": "你是通用问答助手，请调用工具后给出直接答案。",
            "additional_user_prompt": "",
            "tool_names": ["general_qa_agent"],
            "max_steps": 1,
            "description": "通用问答",
            "force_tool_call": True,
            **(
                {
                    "llm_config": {
                        "base_url": fallback_model.model_url.strip(),
                        "api_key": default_api_key,
                        "model": fallback_model.model_name.strip(),
                        "temperature": 0.1,
                        "max_tokens": 2048,
                    }
                }
                if fallback_model is not None
                else {}
            ),
        }

    payload = {"scene_agent_configs": scene_agent_configs}
    payload.update(build_runtime_resource_payload(state))
    if state.agent_engine in ("legacy", "agentscope"):
        payload["engine"] = state.agent_engine
    return payload


def build_runtime_chat_payload(store: ConfigRepository, payload: ChatRequest) -> dict[str, Any]:
    request_payload = payload.model_dump(exclude_none=True)
    state = store.load()
    request_payload.setdefault(
        "scene_selection",
        build_scene_selection_payload(state),
    )
    request_payload.setdefault(
        "dispatch_config",
        build_dispatch_config_payload(state),
    )
    return request_payload
