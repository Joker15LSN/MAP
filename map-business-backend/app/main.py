from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .core_client import MapCoreClient
from .schemas import (
    AddressConfigItem,
    AdminState,
    BasicSettingItem,
    BusinessAgentConfig,
    BusinessAgentTestChatRequest,
    ChatRequest,
    DashboardCardConfig,
    DataAccessItem,
    DataAssetItem,
    FlowPolicyConfig,
    FlowSkillDescriptor,
    GlossaryTermItem,
    HomeRecommendationItem,
    KnowledgeBinding,
    MasterAgentConfig,
    MasterPromptVersion,
    MasterPublishRequest,
    MasterRollbackRequest,
    McpServerConfig,
    McpToolConfig,
    ModelCenterConfig,
    ModelRecord,
    PermissionRule,
    ReleaseRecord,
    RolePolicy,
    ScenarioPackConfig,
    SecurityPolicyItem,
    SessionPolicyItem,
    SkillPolicy,
    SkillUploadRequest,
    UploadedSkill,
    UserAccount,
)
from .store import AdminStateStore
from .telemetry import configure_bff_telemetry, shutdown_bff_telemetry

MAP_CORE_API_ORIGIN = os.getenv("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000")
STATE_FILE = os.getenv(
    "MAP_BFF_STATE_FILE",
    "/app/data/admin_state.json",
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Telemetry is configured once at import time above (idempotent); the
    # lifespan only owns the process-exit shutdown. OTel's global
    # TracerProvider and the FastAPI/httpx instrumentation cannot be
    # reinstalled in-process, so shutdown is a terminal state.
    yield
    shutdown_bff_telemetry()


app = FastAPI(
    title="MAP Business Backend",
    description="MAP business management BFF. Frontend talks to this service only.",
    version="0.2.0",
    lifespan=_lifespan,
)

# SERVER/CLIENT spans + dynamic traceparent injection (no-op unless
# MAP_OTEL_ENABLED is truthy). Must run before requests are served.
configure_bff_telemetry(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = AdminStateStore(STATE_FILE)
core_client = MapCoreClient(MAP_CORE_API_ORIGIN)

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


def _now_iso() -> str:
    return datetime.now().isoformat()


def _slugify(value: str, *, prefix: str = "item") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    if not normalized:
        normalized = uuid.uuid4().hex[:8]
    return f"{prefix}-{normalized}" if not normalized.startswith(f"{prefix}-") else normalized


def _resolve_large_model_row(
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


def _dedupe_text_items(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _normalize_tool_name(raw_tool_name: str) -> str | None:
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


def _mcp_tool_runtime_name(server_id: str, tool_name: str) -> str:
    return f"mcp__{_slugify(server_id, prefix='server').replace('-', '_')}__{_slugify(tool_name, prefix='tool').replace('-', '_')}"


def _skill_runtime_tool_name(skill_id: str) -> str:
    return f"skill__{_slugify(skill_id, prefix='skill').replace('-', '_')}"


def _master_version_payload(
    master: MasterAgentConfig,
    *,
    version: str,
    operator: str,
    note: str,
) -> MasterPromptVersion:
    return MasterPromptVersion(
        version=version,
        created_at=_now_iso(),
        operator=operator,
        note=note,
        route_prompt=master.route_prompt,
        summary_prompt=master.summary_prompt,
        route_model=master.route_model,
        summary_model=master.summary_model,
        model=master.model,
        temperature=master.temperature,
        max_tokens=master.max_tokens,
    )


def _master_version_to_config(
    master: MasterAgentConfig,
    version: MasterPromptVersion,
) -> MasterAgentConfig:
    return master.model_copy(
        update={
            "route_prompt": version.route_prompt,
            "summary_prompt": version.summary_prompt,
            "route_model": version.route_model,
            "summary_model": version.summary_model,
            "model": version.model,
            "temperature": version.temperature,
            "max_tokens": version.max_tokens,
            "current_version": version.version,
            "draft_version": f"{version.version}-draft",
        }
    )


def _master_prompt_snapshot(master: MasterAgentConfig | MasterPromptVersion) -> str:
    payload = {
        "route_model": master.route_model,
        "summary_model": master.summary_model,
        "model": master.model,
        "temperature": master.temperature,
        "max_tokens": master.max_tokens,
        "route_prompt": master.route_prompt,
        "summary_prompt": master.summary_prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _unified_diff(from_label: str, from_text: str, to_label: str, to_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            from_text.splitlines(keepends=True),
            to_text.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
        )
    )


def _build_scene_selection_payload(state: AdminState) -> dict[str, Any]:
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
    route_model_row = _resolve_large_model_row(state, state.master_agent.route_model)
    summary_model_row = _resolve_large_model_row(state, state.master_agent.summary_model)
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


def _derive_agent_tool_names(state: AdminState, agent: BusinessAgentConfig) -> list[str]:
    tool_names: list[str] = [
        normalized
        for normalized in (
            _normalize_tool_name(tool_name) for tool_name in (agent.tools or [])
        )
        if normalized is not None
    ]
    mcp_by_id = {server.server_id: server for server in state.mcp_servers}

    for mount in agent.resource_mounts or []:
        if not mount.enabled:
            continue
        if mount.resource_type == "builtin_tool":
            raw_name = mount.builtin_tool_name or mount.resource_id or mount.resource_name
            normalized = _normalize_tool_name(raw_name)
            if normalized:
                tool_names.append(normalized)
        elif mount.resource_type == "knowledge_base":
            tool_names.append("search_mounted_kb_agent")
        elif mount.resource_type == "data_model":
            tool_names.append("ask_database_agent")
        elif mount.resource_type == "skill":
            skill_id = mount.skill_id or mount.resource_id
            if skill_id:
                tool_names.append(_skill_runtime_tool_name(skill_id))
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
                    tool_names.append(_mcp_tool_runtime_name(server.server_id, tool_name))

    return _dedupe_text_items(tool_names)


def _build_runtime_resource_payload(state: AdminState) -> dict[str, Any]:
    return {
        "mcp_servers": [server.model_dump() for server in state.mcp_servers if server.enabled],
        "skills": [skill.model_dump() for skill in state.skills if skill.status == "active"],
        "flow_skill_descriptors": [
            item.model_dump() for item in state.flow_skill_descriptors if item.status == "active"
        ],
    }


def _build_dispatch_config_payload(state: AdminState) -> dict[str, Any]:
    scene_agent_configs: dict[str, dict[str, Any]] = {}
    default_api_key = os.getenv("MAP_LLM_API_KEY", "")
    for agent in state.business_agents:
        agent_code = agent.agent_code.strip()
        if not agent.enabled or agent_code not in SUPPORTED_SCENE_AGENT_CODES:
            continue

        normalized_tools = _derive_agent_tool_names(state, agent)
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

        model_row = _resolve_large_model_row(
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
        fallback_model = _resolve_large_model_row(state, None)
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
    payload.update(_build_runtime_resource_payload(state))
    if state.agent_engine in ("legacy", "agentscope"):
        payload["engine"] = state.agent_engine
    return payload


def _build_runtime_chat_payload(payload: ChatRequest) -> dict[str, Any]:
    request_payload = payload.model_dump(exclude_none=True)
    state = store.load()
    request_payload.setdefault(
        "scene_selection",
        _build_scene_selection_payload(state),
    )
    request_payload.setdefault(
        "dispatch_config",
        _build_dispatch_config_payload(state),
    )
    return request_payload


def _forward_headers(
    request_token: str | None,
    request: Request,
) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if request_token:
        headers["X-request-token"] = request_token
    if request.headers.get("X-UserId"):
        headers["X-UserId"] = request.headers["X-UserId"]
    if request.headers.get("X-UserName"):
        headers["X-UserName"] = request.headers["X-UserName"]
    # Forward inbound W3C propagation headers so an existing upstream trace
    # continues even when OTel is disabled. With OTel enabled the httpx
    # instrumentation additionally injects a dynamic traceparent referencing
    # the BFF CLIENT span (overwriting these values at send time), so
    # map_core always joins the trace owned by the BFF SERVER span.
    for propagation_header in ("traceparent", "tracestate", "baggage"):
        value = request.headers.get(propagation_header)
        if value:
            headers[propagation_header] = value
    return headers


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "map-business-backend"}


@app.post("/api/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> dict[str, Any]:
    headers = _forward_headers(request_token, request)
    request_payload = _build_runtime_chat_payload(payload)
    try:
        return await core_client.chat(
            request_payload,
            headers=headers,
        )
    except Exception as exc:
        return {
            "content": (
                "MAP 算法服务当前不可用，业务后端已捕获该异常。"
                "你仍可继续查看前后台页面和管理配置。"
            ),
            "meta": {
                "fallback": True,
                "upstream_error": str(exc),
            },
        }


@app.post("/api/chat/stream/v2")
async def chat_stream_v2(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> StreamingResponse:
    headers = _forward_headers(request_token, request)
    request_payload = _build_runtime_chat_payload(payload)

    async def stream() -> Any:
        try:
            async for chunk in core_client.stream_chat(
                request_payload,
                headers=headers,
            ):
                yield chunk
        except Exception as exc:
            error_data = json.dumps(
                {
                    "error": f"MAP 算法服务不可用: {exc}",
                    "fallback": True,
                },
                ensure_ascii=False,
            )
            done_data = json.dumps(
                {
                    "content": (
                        "MAP 算法服务当前不可用，已自动回退到业务后端兜底响应。"
                    ),
                    "meta": {"fallback": True},
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode("utf-8")
            yield f"event: done\ndata: {done_data}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/flow/v1")
async def chat_flow_v1(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> dict[str, Any]:
    headers = _forward_headers(request_token, request)
    request_payload = _build_runtime_chat_payload(payload)
    try:
        return await core_client.chat_by_path(
            "/flow_domain/chat/v1",
            request_payload,
            headers=headers,
        )
    except Exception as exc:
        return {
            "content": (
                "MAP 心流算法服务当前不可用，业务后端已捕获该异常。"
                "你仍可继续使用全域模式。"
            ),
            "meta": {
                "fallback": True,
                "upstream_error": str(exc),
                "mode": "flow",
            },
        }


@app.post("/api/chat/stream/flow/v1")
async def chat_stream_flow_v1(
    payload: ChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> StreamingResponse:
    headers = _forward_headers(request_token, request)
    request_payload = _build_runtime_chat_payload(payload)

    async def stream() -> Any:
        try:
            async for chunk in core_client.stream_chat_by_path(
                "/flow_domain/chat/stream/v1",
                request_payload,
                headers=headers,
            ):
                yield chunk
        except Exception as exc:
            error_data = json.dumps(
                {
                    "error": f"MAP 心流算法服务不可用: {exc}",
                    "fallback": True,
                    "mode": "flow",
                },
                ensure_ascii=False,
            )
            done_data = json.dumps(
                {
                    "content": (
                        "MAP 心流算法服务当前不可用，已自动回退到业务后端兜底响应。"
                    ),
                    "meta": {"fallback": True, "mode": "flow"},
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode("utf-8")
            yield f"event: done\ndata: {done_data}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/admin/full-config")
async def full_config() -> dict[str, Any]:
    return store.load().model_dump()


@app.get("/api/admin/flow-runtime-snapshot")
async def flow_runtime_snapshot() -> dict[str, Any]:
    state = store.load()
    return {
        "updated_at": state.updated_at,
        "flow_policy": state.flow_policy.model_dump(),
        "scenario_packs": [item.model_dump() for item in state.scenario_packs],
        "flow_skill_descriptors": [
            item.model_dump() for item in state.flow_skill_descriptors
        ],
        "mcp_servers": [item.model_dump() for item in state.mcp_servers],
        "skills": [item.model_dump() for item in state.skills],
        "business_agents": [
            {
                "agent_code": agent.agent_code,
                "display_name": agent.display_name,
                "enabled": agent.enabled,
                "resource_mounts": [item.model_dump() for item in agent.resource_mounts],
                "prompt_config": agent.prompt_config.model_dump(),
            }
            for agent in state.business_agents
        ],
        "master_agent": {
            "route_prompt": state.master_agent.route_prompt,
            "summary_prompt": state.master_agent.summary_prompt,
            "route_model": state.master_agent.route_model,
            "summary_model": state.master_agent.summary_model,
            "current_version": state.master_agent.current_version,
        },
    }


@app.get("/api/admin/summary")
async def admin_summary() -> dict[str, Any]:
    state = store.load()
    enabled_business_agents = [agent for agent in state.business_agents if agent.enabled]
    enabled_skills = [item for item in state.skill_policies if item.enabled]
    enabled_users = [item for item in state.user_accounts if item.status == "enabled"]
    model_total = (
        len(state.model_center.large_models)
        + len(state.model_center.asr_models)
        + len(state.model_center.tts_models)
        + len(state.model_center.embedding_models)
        + len(state.model_center.rerank_models)
    )
    return {
        "updated_at": state.updated_at,
        "master_version": state.master_agent.current_version,
        "business_agent_count": len(state.business_agents),
        "business_agent_enabled_count": len(enabled_business_agents),
        "permission_rule_count": len(state.permission_rules),
        "knowledge_binding_count": len(state.knowledge_bindings),
        "skill_enabled_count": len(enabled_skills),
        "mcp_server_count": len(state.mcp_servers),
        "skill_count": len(state.skills),
        "release_count": len(state.release_history),
        "model_count": model_total,
        "user_count": len(state.user_accounts),
        "user_enabled_count": len(enabled_users),
    }


@app.get("/api/admin/model-center")
async def get_model_center() -> ModelCenterConfig:
    return store.load().model_center


@app.put("/api/admin/model-center")
async def put_model_center(payload: ModelCenterConfig) -> ModelCenterConfig:
    state, _ = store.update(lambda draft: setattr(draft, "model_center", payload))
    return state.model_center


@app.get("/api/admin/basic-settings")
async def get_basic_settings() -> list[BasicSettingItem]:
    return store.load().basic_settings


@app.put("/api/admin/basic-settings")
async def put_basic_settings(payload: list[BasicSettingItem]) -> list[BasicSettingItem]:
    state, _ = store.update(lambda draft: setattr(draft, "basic_settings", payload))
    return state.basic_settings


@app.get("/api/admin/address-configs")
async def get_address_configs() -> list[AddressConfigItem]:
    return store.load().address_configs


@app.put("/api/admin/address-configs")
async def put_address_configs(payload: list[AddressConfigItem]) -> list[AddressConfigItem]:
    state, _ = store.update(lambda draft: setattr(draft, "address_configs", payload))
    return state.address_configs


@app.get("/api/admin/data-connectors")
async def get_data_connectors() -> list[DataAccessItem]:
    return store.load().data_access_items


@app.put("/api/admin/data-connectors")
async def put_data_connectors(payload: list[DataAccessItem]) -> list[DataAccessItem]:
    state, _ = store.update(lambda draft: setattr(draft, "data_access_items", payload))
    return state.data_access_items


@app.get("/api/admin/data-assets")
async def get_data_assets() -> list[DataAssetItem]:
    return store.load().data_assets


@app.put("/api/admin/data-assets")
async def put_data_assets(payload: list[DataAssetItem]) -> list[DataAssetItem]:
    state, _ = store.update(lambda draft: setattr(draft, "data_assets", payload))
    return state.data_assets


@app.get("/api/admin/master-agent")
async def get_master_agent() -> MasterAgentConfig:
    state = store.load()
    return state.master_agent


@app.put("/api/admin/master-agent")
async def put_master_agent(payload: MasterAgentConfig) -> MasterAgentConfig:
    state, _ = store.update(lambda draft: setattr(draft, "master_agent", payload))
    return state.master_agent


@app.post("/api/admin/master-agent/publish")
async def publish_master_agent(payload: MasterPublishRequest) -> dict[str, Any]:
    def _publish(draft: AdminState) -> dict[str, Any]:
        master = draft.master_agent
        previous = next(
            (
                item
                for item in master.prompt_versions
                if item.version == master.current_version
            ),
            None,
        )
        version = (payload.version or "").strip()
        if not version:
            existing_numbers = []
            for item in master.prompt_versions:
                if item.version.startswith("v") and item.version[1:].isdigit():
                    existing_numbers.append(int(item.version[1:]))
            version = f"v{(max(existing_numbers) if existing_numbers else 0) + 1}"
        if any(item.version == version for item in master.prompt_versions):
            raise HTTPException(status_code=409, detail=f"version {version} already exists")

        created = _master_version_payload(
            master,
            version=version,
            operator=payload.operator,
            note=payload.note.strip() or "Master 提示词发布",
        )
        master.prompt_versions.insert(0, created)
        draft.master_agent = master.model_copy(
            update={"current_version": version, "draft_version": f"{version}-draft"}
        )
        record = ReleaseRecord(
            id=f"rel-{uuid.uuid4().hex[:8]}",
            version=version,
            operator=payload.operator,
            note=payload.note.strip() or "Master 提示词发布",
            affected_agents=["Master"],
            risk_level="medium",
            created_at=_now_iso(),
        )
        draft.release_history.insert(0, record)
        previous_snapshot = (
            _master_prompt_snapshot(previous)
            if previous is not None
            else ""
        )
        return {
            "version": created.model_dump(),
            "release": record.model_dump(),
            "diff": _unified_diff(
                previous.version if previous is not None else "empty",
                previous_snapshot,
                version,
                _master_prompt_snapshot(created),
            ),
        }

    _, result = store.update(_publish)
    return result


@app.get("/api/admin/master-agent/versions")
async def list_master_versions() -> list[MasterPromptVersion]:
    return store.load().master_agent.prompt_versions


@app.get("/api/admin/master-agent/versions/{version}")
async def get_master_version(version: str) -> MasterPromptVersion:
    for item in store.load().master_agent.prompt_versions:
        if item.version == version:
            return item
    raise HTTPException(status_code=404, detail=f"version {version} not found")


@app.get("/api/admin/master-agent/diff")
async def diff_master_versions(
    from_version: str | None = None,
    to_version: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    state = store.load()
    master = state.master_agent
    from_key = (from_version or from_ or "").strip()
    to_key = (to_version or to or "current").strip()
    versions = {item.version: item for item in master.prompt_versions}

    def _snapshot_for(key: str) -> tuple[str, str]:
        if key in {"", "current"}:
            return "current", _master_prompt_snapshot(master)
        version = versions.get(key)
        if version is None:
            raise HTTPException(status_code=404, detail=f"version {key} not found")
        return version.version, _master_prompt_snapshot(version)

    from_label, from_text = _snapshot_for(from_key or master.current_version)
    to_label, to_text = _snapshot_for(to_key)
    return {
        "from": from_label,
        "to": to_label,
        "diff": _unified_diff(from_label, from_text, to_label, to_text),
    }


@app.post("/api/admin/master-agent/rollback")
async def rollback_master_agent(payload: MasterRollbackRequest) -> MasterAgentConfig:
    def _rollback(draft: AdminState) -> MasterAgentConfig:
        target = next(
            (
                item
                for item in draft.master_agent.prompt_versions
                if item.version == payload.version
            ),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"version {payload.version} not found")
        draft.master_agent = _master_version_to_config(draft.master_agent, target)
        draft.release_history.insert(
            0,
            ReleaseRecord(
                id=f"rel-{uuid.uuid4().hex[:8]}",
                version=payload.version,
                operator=payload.operator,
                note=payload.note.strip() or f"回滚 Master 到 {payload.version}",
                affected_agents=["Master"],
                risk_level="medium",
                created_at=_now_iso(),
            ),
        )
        return draft.master_agent

    _, updated = store.update(_rollback)
    return updated


@app.get("/api/admin/business-agents")
async def get_business_agents() -> list[BusinessAgentConfig]:
    state = store.load()
    return state.business_agents


@app.post("/api/admin/business-agents")
async def post_business_agent(payload: BusinessAgentConfig) -> BusinessAgentConfig:
    now = datetime.now().isoformat()

    def _append(draft: Any) -> BusinessAgentConfig:
        exists = any(item.agent_code == payload.agent_code for item in draft.business_agents)
        if exists:
            raise HTTPException(status_code=409, detail=f"agent {payload.agent_code} already exists")
        created = payload.model_copy(update={"last_updated": now})
        draft.business_agents.append(created)
        return created

    _, created = store.update(_append)
    return created


@app.put("/api/admin/business-agents/{agent_code}")
async def put_business_agent(agent_code: str, payload: BusinessAgentConfig) -> BusinessAgentConfig:
    if payload.agent_code != agent_code:
        raise HTTPException(status_code=400, detail="agent_code in path and body must match")

    now = datetime.now().isoformat()

    def _update(draft: Any) -> BusinessAgentConfig:
        for idx, item in enumerate(draft.business_agents):
            if item.agent_code == agent_code:
                updated = payload.model_copy(update={"last_updated": now})
                draft.business_agents[idx] = updated
                return updated
        raise HTTPException(status_code=404, detail=f"agent {agent_code} not found")

    _, updated = store.update(_update)
    return updated


@app.post("/api/admin/business-agents/{agent_code}/test-chat")
async def test_business_agent(
    agent_code: str,
    payload: BusinessAgentTestChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
) -> dict[str, Any]:
    state = store.load()
    agent = payload.agent
    if agent is None:
        agent = next((item for item in state.business_agents if item.agent_code == agent_code), None)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_code} not found")
    if agent.agent_code != agent_code:
        raise HTTPException(status_code=400, detail="agent_code in path and body must match")

    temp_state = state.model_copy(update={"business_agents": [agent]})
    dispatch_payload = _build_dispatch_config_payload(temp_state)
    scene_agent_config = (dispatch_payload.get("scene_agent_configs") or {}).get(agent_code)
    if scene_agent_config is None:
        raise HTTPException(status_code=400, detail=f"agent {agent_code} cannot be materialized")

    headers = _forward_headers(request_token, request)
    debug_payload = {
        "query": payload.query,
        "history": payload.history,
        "agent_code": agent_code,
        "scene_agent_config": scene_agent_config,
        "dispatch_config": dispatch_payload,
        "scene_selection": _build_scene_selection_payload(temp_state),
    }
    try:
        return await core_client.chat_by_path(
            "/global_domain/debug/scene_agent/run",
            debug_payload,
            headers=headers,
        )
    except Exception as exc:
        return {
            "request_id": "bff-fallback",
            "state_id": "bff-fallback",
            "agent_code": agent_code,
            "result": {
                "success": False,
                "name": agent_code,
                "content": "",
                "error": f"MAP 算法服务不可用或测试执行失败: {exc}",
            },
        }


@app.get("/api/admin/session-policies")
async def get_session_policies() -> list[SessionPolicyItem]:
    return store.load().session_policies


@app.put("/api/admin/session-policies")
async def put_session_policies(payload: list[SessionPolicyItem]) -> list[SessionPolicyItem]:
    state, _ = store.update(lambda draft: setattr(draft, "session_policies", payload))
    return state.session_policies


@app.get("/api/admin/dashboard-cards")
async def get_dashboard_cards() -> list[DashboardCardConfig]:
    return store.load().dashboard_cards


@app.put("/api/admin/dashboard-cards")
async def put_dashboard_cards(payload: list[DashboardCardConfig]) -> list[DashboardCardConfig]:
    state, _ = store.update(lambda draft: setattr(draft, "dashboard_cards", payload))
    return state.dashboard_cards


@app.get("/api/admin/security-policies")
async def get_security_policies() -> list[SecurityPolicyItem]:
    return store.load().security_policies


@app.put("/api/admin/security-policies")
async def put_security_policies(payload: list[SecurityPolicyItem]) -> list[SecurityPolicyItem]:
    state, _ = store.update(lambda draft: setattr(draft, "security_policies", payload))
    return state.security_policies


@app.get("/api/admin/glossary-terms")
async def get_glossary_terms() -> list[GlossaryTermItem]:
    return store.load().glossary_terms


@app.put("/api/admin/glossary-terms")
async def put_glossary_terms(payload: list[GlossaryTermItem]) -> list[GlossaryTermItem]:
    state, _ = store.update(lambda draft: setattr(draft, "glossary_terms", payload))
    return state.glossary_terms


@app.get("/api/admin/homepage-recommendations")
async def get_homepage_recommendations() -> list[HomeRecommendationItem]:
    return store.load().homepage_recommendations


@app.put("/api/admin/homepage-recommendations")
async def put_homepage_recommendations(payload: list[HomeRecommendationItem]) -> list[HomeRecommendationItem]:
    state, _ = store.update(lambda draft: setattr(draft, "homepage_recommendations", payload))
    return state.homepage_recommendations


@app.get("/api/admin/permission-rules")
async def get_permission_rules() -> list[PermissionRule]:
    state = store.load()
    return state.permission_rules


@app.put("/api/admin/permission-rules")
async def put_permission_rules(payload: list[PermissionRule]) -> list[PermissionRule]:
    state, _ = store.update(lambda draft: setattr(draft, "permission_rules", payload))
    return state.permission_rules


@app.get("/api/admin/role-policies")
async def get_role_policies() -> list[RolePolicy]:
    return store.load().role_policies


@app.put("/api/admin/role-policies")
async def put_role_policies(payload: list[RolePolicy]) -> list[RolePolicy]:
    state, _ = store.update(lambda draft: setattr(draft, "role_policies", payload))
    return state.role_policies


@app.get("/api/admin/user-accounts")
async def get_user_accounts() -> list[UserAccount]:
    return store.load().user_accounts


@app.put("/api/admin/user-accounts")
async def put_user_accounts(payload: list[UserAccount]) -> list[UserAccount]:
    state, _ = store.update(lambda draft: setattr(draft, "user_accounts", payload))
    return state.user_accounts


@app.get("/api/admin/knowledge-bindings")
async def get_knowledge_bindings() -> list[KnowledgeBinding]:
    state = store.load()
    return state.knowledge_bindings


@app.put("/api/admin/knowledge-bindings")
async def put_knowledge_bindings(payload: list[KnowledgeBinding]) -> list[KnowledgeBinding]:
    state, _ = store.update(lambda draft: setattr(draft, "knowledge_bindings", payload))
    return state.knowledge_bindings


@app.get("/api/admin/skill-policies")
async def get_skill_policies() -> list[SkillPolicy]:
    state = store.load()
    return state.skill_policies


@app.put("/api/admin/skill-policies")
async def put_skill_policies(payload: list[SkillPolicy]) -> list[SkillPolicy]:
    state, _ = store.update(lambda draft: setattr(draft, "skill_policies", payload))
    return state.skill_policies


@app.get("/api/admin/mcp-servers")
async def get_mcp_servers() -> list[McpServerConfig]:
    return store.load().mcp_servers


@app.put("/api/admin/mcp-servers")
async def put_mcp_servers(payload: list[McpServerConfig]) -> list[McpServerConfig]:
    state, _ = store.update(lambda draft: setattr(draft, "mcp_servers", payload))
    return state.mcp_servers


@app.post("/api/admin/mcp-servers")
async def post_mcp_server(payload: McpServerConfig) -> McpServerConfig:
    def _upsert(draft: AdminState) -> McpServerConfig:
        for idx, item in enumerate(draft.mcp_servers):
            if item.server_id == payload.server_id:
                draft.mcp_servers[idx] = payload
                return payload
        draft.mcp_servers.insert(0, payload)
        return payload

    _, server = store.update(_upsert)
    return server


async def _probe_mcp_tools(server: McpServerConfig) -> tuple[list[McpToolConfig], str]:
    """Best-effort MCP tool discovery without storing credentials."""
    now = _now_iso()
    if server.transport == "stdio":
        # Backend config should not launch arbitrary local commands just to render admin UI.
        return (
            [
                tool.model_copy(update={"last_seen_at": now})
                for tool in server.tools
            ],
            "stdio_configured",
        )

    if not server.url.strip():
        return (server.tools, "missing_url")

    payloads = [
        {"jsonrpc": "2.0", "id": "map-tools-list", "method": "tools/list", "params": {}},
        {"method": "tools/list", "params": {}},
    ]
    headers = {
        key: value
        for key, value in server.headers.items()
        if isinstance(value, str) and value
    }
    timeout = httpx.Timeout(timeout=max(5, server.timeout_s), connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            last_error = ""
            for payload in payloads:
                try:
                    response = await client.post(server.url, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    raw_tools = (
                        data.get("result", {}).get("tools")
                        if isinstance(data.get("result"), dict)
                        else data.get("tools")
                    )
                    if not isinstance(raw_tools, list):
                        continue
                    return (
                        [
                            McpToolConfig(
                                name=str(item.get("name") or "").strip(),
                                description=str(item.get("description") or ""),
                                input_schema=item.get("inputSchema")
                                if isinstance(item.get("inputSchema"), dict)
                                else item.get("input_schema")
                                if isinstance(item.get("input_schema"), dict)
                                else {},
                                enabled=True,
                                last_seen_at=now,
                            )
                            for item in raw_tools
                            if isinstance(item, dict)
                            and str(item.get("name") or "").strip()
                        ],
                        "ok",
                    )
                except Exception as exc:
                    last_error = str(exc)
            return (server.tools, f"refresh_failed: {last_error or 'invalid tools/list response'}")
    except Exception as exc:
        return (server.tools, f"refresh_failed: {exc}")


@app.post("/api/admin/mcp-servers/{server_id}/refresh-tools")
async def refresh_mcp_server_tools(server_id: str) -> McpServerConfig:
    state = store.load()
    server = next((item for item in state.mcp_servers if item.server_id == server_id), None)
    if server is None:
        raise HTTPException(status_code=404, detail=f"MCP server {server_id} not found")
    tools, status = await _probe_mcp_tools(server)

    def _update(draft: AdminState) -> McpServerConfig:
        for idx, item in enumerate(draft.mcp_servers):
            if item.server_id == server_id:
                updated = item.model_copy(
                    update={
                        "tools": tools,
                        "status": status,
                        "last_refreshed_at": _now_iso(),
                    }
                )
                draft.mcp_servers[idx] = updated
                return updated
        raise HTTPException(status_code=404, detail=f"MCP server {server_id} not found")

    _, updated = store.update(_update)
    return updated


@app.get("/api/admin/skills")
async def get_uploaded_skills() -> list[UploadedSkill]:
    return store.load().skills


@app.put("/api/admin/skills")
async def put_uploaded_skills(payload: list[UploadedSkill]) -> list[UploadedSkill]:
    def _replace(draft: AdminState) -> list[UploadedSkill]:
        draft.skills = payload
        _sync_uploaded_skills_to_skillhub(draft)
        return draft.skills

    state, skills = store.update(_replace)
    return skills


def _decode_skill_upload(payload: SkillUploadRequest) -> tuple[str, dict[str, Any]]:
    raw_content = payload.content.encode("utf-8")
    if payload.encoding == "base64":
        raw_content = base64.b64decode(payload.content)

    filename = payload.filename.strip()
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
            names = archive.namelist()
            skill_name = next((name for name in names if name.endswith("SKILL.md")), None)
            if skill_name is None:
                raise HTTPException(status_code=400, detail="zip must contain SKILL.md")
            skill_content = archive.read(skill_name).decode("utf-8")
            metadata = dict(payload.metadata)
            manifest_name = next((name for name in names if name.endswith("skill.json")), None)
            if manifest_name:
                try:
                    manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
                    if isinstance(manifest, dict):
                        metadata.update(manifest)
                except json.JSONDecodeError as exc:
                    raise HTTPException(status_code=400, detail=f"invalid skill.json: {exc}") from exc
            return skill_content, metadata

    return raw_content.decode("utf-8"), dict(payload.metadata)


def _skill_from_upload(payload: SkillUploadRequest) -> UploadedSkill:
    content, metadata = _decode_skill_upload(payload)
    raw_name = str(metadata.get("name") or payload.filename.rsplit(".", 1)[0]).strip()
    skill_id = str(metadata.get("skill_id") or _slugify(raw_name, prefix="skill"))
    raw_mount_agents = metadata.get("mount_agents")
    mount_agents = (
        payload.mount_agents
        if payload.mount_agents
        else [str(item) for item in raw_mount_agents]
        if isinstance(raw_mount_agents, list)
        else []
    )
    now = _now_iso()
    return UploadedSkill(
        skill_id=skill_id,
        name=raw_name or skill_id,
        display_name=str(metadata.get("display_name") or raw_name or skill_id),
        version=str(metadata.get("version") or "1.0.0"),
        description=str(metadata.get("description") or ""),
        content=content,
        metadata=metadata,
        mount_agents=mount_agents,
        status=str(metadata.get("status") or "active"),
        uploaded_at=now,
        updated_at=now,
    )


def _sync_uploaded_skills_to_skillhub(draft: AdminState) -> None:
    non_uploaded = [
        item
        for item in draft.flow_skill_descriptors
        if item.metadata.get("source") != "manual_upload"
    ]
    uploaded_descriptors = [
        FlowSkillDescriptor(
            skill_id=skill.skill_id,
            name=skill.name,
            display_name=skill.display_name,
            version=skill.version,
            description=skill.description,
            tool_name=_skill_runtime_tool_name(skill.skill_id),
            executor_type="prompt_skill",
            content=skill.content,
            metadata={**skill.metadata, "source": "manual_upload"},
            mount_agents=list(skill.mount_agents),
            required_scopes=[],
            audit_tags=["manual_upload", "prompt_skill"],
            status="active" if skill.status == "active" else "inactive",
        )
        for skill in draft.skills
    ]
    draft.flow_skill_descriptors = [*uploaded_descriptors, *non_uploaded]


@app.post("/api/admin/skills/upload")
async def upload_skill(payload: SkillUploadRequest) -> UploadedSkill:
    uploaded = _skill_from_upload(payload)

    def _upsert(draft: AdminState) -> UploadedSkill:
        for idx, item in enumerate(draft.skills):
            if item.skill_id == uploaded.skill_id:
                draft.skills[idx] = uploaded
                _sync_uploaded_skills_to_skillhub(draft)
                return uploaded
        draft.skills.insert(0, uploaded)
        _sync_uploaded_skills_to_skillhub(draft)
        return uploaded

    _, skill = store.update(_upsert)
    return skill


@app.get("/api/admin/flow-policy")
async def get_flow_policy() -> FlowPolicyConfig:
    return store.load().flow_policy


@app.put("/api/admin/flow-policy")
async def put_flow_policy(payload: FlowPolicyConfig) -> FlowPolicyConfig:
    state, _ = store.update(lambda draft: setattr(draft, "flow_policy", payload))
    return state.flow_policy


@app.get("/api/admin/scenario-packs")
async def get_scenario_packs() -> list[ScenarioPackConfig]:
    return store.load().scenario_packs


@app.put("/api/admin/scenario-packs")
async def put_scenario_packs(payload: list[ScenarioPackConfig]) -> list[ScenarioPackConfig]:
    state, _ = store.update(lambda draft: setattr(draft, "scenario_packs", payload))
    return state.scenario_packs


@app.get("/api/admin/flow-skill-descriptors")
async def get_flow_skill_descriptors() -> list[FlowSkillDescriptor]:
    return store.load().flow_skill_descriptors


@app.put("/api/admin/flow-skill-descriptors")
async def put_flow_skill_descriptors(payload: list[FlowSkillDescriptor]) -> list[FlowSkillDescriptor]:
    state, _ = store.update(lambda draft: setattr(draft, "flow_skill_descriptors", payload))
    return state.flow_skill_descriptors


@app.get("/api/admin/release-history")
async def get_release_history() -> list[ReleaseRecord]:
    state = store.load()
    return state.release_history


@app.post("/api/admin/release-history")
async def append_release_history(
    note: str,
    operator: str = "admin",
    version: str = "v1",
    risk_level: str = "low",
    affected_agents: str = "Master,Operations,Marketing,CustomerSuccess",
) -> ReleaseRecord:
    now = datetime.now().isoformat()
    record = ReleaseRecord(
        id=f"rel-{uuid.uuid4().hex[:8]}",
        version=version,
        operator=operator,
        note=note.strip() or "配置发布",
        affected_agents=[item.strip() for item in affected_agents.split(",") if item.strip()],
        risk_level=risk_level,
        created_at=now,
    )

    def _append(draft: Any) -> ReleaseRecord:
        draft.release_history.insert(0, record)
        return record

    _, created = store.update(_append)
    return created
