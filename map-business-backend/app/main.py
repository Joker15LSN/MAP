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
    MasterAgentConfig,
    ModelCenterConfig,
    PermissionRule,
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
    try:
        return await core_client.chat(
            payload.model_dump(exclude_none=True),
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

    async def stream() -> Any:
        try:
            async for chunk in core_client.stream_chat(
                payload.model_dump(exclude_none=True),
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
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/admin/full-config")
async def full_config() -> dict[str, Any]:
    return store.load().model_dump()


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
