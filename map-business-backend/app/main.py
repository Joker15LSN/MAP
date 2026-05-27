from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .core_client import MapCoreClient
from .schemas import (
    AdminState,
    AddressConfigItem,
    BasicSettingItem,
    BusinessAgentConfig,
    ChatRequest,
    DashboardCardConfig,
    DataAccessItem,
    DataAssetItem,
    GlossaryTermItem,
    HomeRecommendationItem,
    KnowledgeBinding,
    FlowPolicyConfig,
    FlowSkillDescriptor,
    MasterAgentConfig,
    ModelRecord,
    ModelCenterConfig,
    PermissionRule,
    ScenarioPackConfig,
    ReleaseRecord,
    RolePolicy,
    SecurityPolicyItem,
    SessionPolicyItem,
    SkillPolicy,
    UserAccount,
)
from .store import AdminStateStore

MAP_CORE_API_ORIGIN = os.getenv("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000")
STATE_FILE = os.getenv(
    "MAP_BFF_STATE_FILE",
    "/app/data/admin_state.json",
)

app = FastAPI(
    title="MAP Business Backend",
    description="MAP business management BFF. Frontend talks to this service only.",
    version="0.2.0",
)

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
    if mapped in KNOWN_TOOL_NAMES:
        return mapped
    return None


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

    return {"enabled_agent_codes": enabled_agent_codes}


def _build_dispatch_config_payload(state: AdminState) -> dict[str, Any]:
    scene_agent_configs: dict[str, dict[str, Any]] = {}
    default_api_key = os.getenv("MAP_LLM_API_KEY", "")
    for agent in state.business_agents:
        agent_code = agent.agent_code.strip()
        if not agent.enabled or agent_code not in SUPPORTED_SCENE_AGENT_CODES:
            continue

        normalized_tools = _dedupe_text_items(
            [
                normalized
                for normalized in (
                    _normalize_tool_name(tool_name) for tool_name in (agent.tools or [])
                )
                if normalized is not None
            ]
        )
        if not normalized_tools:
            normalized_tools = ["general_qa_agent"]

        prompt = (
            (agent.prompt_config.system_prompt if agent.prompt_config else "").strip()
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

    return {"scene_agent_configs": scene_agent_configs}


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
        "master_enabled": state.master_agent.enabled,
        "business_agent_count": len(state.business_agents),
        "business_agent_enabled_count": len(enabled_business_agents),
        "permission_rule_count": len(state.permission_rules),
        "knowledge_binding_count": len(state.knowledge_bindings),
        "skill_enabled_count": len(enabled_skills),
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
