"""Admin configuration endpoints: section get/put, summary, snapshots.

Extracted verbatim from ``app.main`` during F-01. URLs, request/response
shapes and operation names are unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from ..core.identity import RequestPrincipal
from ..db.session import DbSession
from ..repositories.config import ConfigRepository
from ..schemas import (
    AddressConfigItem,
    BasicSettingItem,
    DashboardCardConfig,
    DataAccessItem,
    DataAssetItem,
    FlowPolicyConfig,
    FlowSkillDescriptor,
    GlossaryTermItem,
    HomeRecommendationItem,
    KnowledgeBinding,
    ModelCenterConfig,
    PermissionRule,
    ReleaseRecord,
    RolePolicy,
    ScenarioPackConfig,
    SecurityPolicyItem,
    SessionPolicyItem,
    SkillPolicy,
    UserAccount,
)
from ..services.audit import admin_write_guard
from .deps import get_store

router = APIRouter()


@router.get("/api/admin/full-config")
async def full_config(store: ConfigRepository = Depends(get_store)) -> dict[str, Any]:
    return store.load().model_dump()


@router.get("/api/admin/flow-runtime-snapshot")
async def flow_runtime_snapshot(store: ConfigRepository = Depends(get_store)) -> dict[str, Any]:
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


@router.get("/api/admin/summary")
async def admin_summary(store: ConfigRepository = Depends(get_store)) -> dict[str, Any]:
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


@router.get("/api/admin/model-center")
async def get_model_center(store: ConfigRepository = Depends(get_store)) -> ModelCenterConfig:
    return store.load().model_center


@router.put("/api/admin/model-center")
async def put_model_center(
    payload: ModelCenterConfig,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> ModelCenterConfig:
    state, _ = store.update(lambda draft: setattr(draft, "model_center", payload))
    return state.model_center


@router.get("/api/admin/basic-settings")
async def get_basic_settings(store: ConfigRepository = Depends(get_store)) -> list[BasicSettingItem]:
    return store.load().basic_settings


@router.put("/api/admin/basic-settings")
async def put_basic_settings(
    payload: list[BasicSettingItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[BasicSettingItem]:
    state, _ = store.update(lambda draft: setattr(draft, "basic_settings", payload))
    return state.basic_settings


@router.get("/api/admin/address-configs")
async def get_address_configs(store: ConfigRepository = Depends(get_store)) -> list[AddressConfigItem]:
    return store.load().address_configs


@router.put("/api/admin/address-configs")
async def put_address_configs(
    payload: list[AddressConfigItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[AddressConfigItem]:
    state, _ = store.update(lambda draft: setattr(draft, "address_configs", payload))
    return state.address_configs


@router.get("/api/admin/data-connectors")
async def get_data_connectors(store: ConfigRepository = Depends(get_store)) -> list[DataAccessItem]:
    return store.load().data_access_items


@router.put("/api/admin/data-connectors")
async def put_data_connectors(
    payload: list[DataAccessItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[DataAccessItem]:
    state, _ = store.update(lambda draft: setattr(draft, "data_access_items", payload))
    return state.data_access_items


@router.get("/api/admin/data-assets")
async def get_data_assets(store: ConfigRepository = Depends(get_store)) -> list[DataAssetItem]:
    return store.load().data_assets


@router.put("/api/admin/data-assets")
async def put_data_assets(
    payload: list[DataAssetItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[DataAssetItem]:
    state, _ = store.update(lambda draft: setattr(draft, "data_assets", payload))
    return state.data_assets


@router.get("/api/admin/session-policies")
async def get_session_policies(store: ConfigRepository = Depends(get_store)) -> list[SessionPolicyItem]:
    return store.load().session_policies


@router.put("/api/admin/session-policies")
async def put_session_policies(
    payload: list[SessionPolicyItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[SessionPolicyItem]:
    state, _ = store.update(lambda draft: setattr(draft, "session_policies", payload))
    return state.session_policies


@router.get("/api/admin/dashboard-cards")
async def get_dashboard_cards(store: ConfigRepository = Depends(get_store)) -> list[DashboardCardConfig]:
    return store.load().dashboard_cards


@router.put("/api/admin/dashboard-cards")
async def put_dashboard_cards(
    payload: list[DashboardCardConfig],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[DashboardCardConfig]:
    state, _ = store.update(lambda draft: setattr(draft, "dashboard_cards", payload))
    return state.dashboard_cards


@router.get("/api/admin/security-policies")
async def get_security_policies(store: ConfigRepository = Depends(get_store)) -> list[SecurityPolicyItem]:
    return store.load().security_policies


@router.put("/api/admin/security-policies")
async def put_security_policies(
    payload: list[SecurityPolicyItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[SecurityPolicyItem]:
    state, _ = store.update(lambda draft: setattr(draft, "security_policies", payload))
    return state.security_policies


@router.get("/api/admin/glossary-terms")
async def get_glossary_terms(store: ConfigRepository = Depends(get_store)) -> list[GlossaryTermItem]:
    return store.load().glossary_terms


@router.put("/api/admin/glossary-terms")
async def put_glossary_terms(
    payload: list[GlossaryTermItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[GlossaryTermItem]:
    state, _ = store.update(lambda draft: setattr(draft, "glossary_terms", payload))
    return state.glossary_terms


@router.get("/api/admin/homepage-recommendations")
async def get_homepage_recommendations(store: ConfigRepository = Depends(get_store)) -> list[HomeRecommendationItem]:
    return store.load().homepage_recommendations


@router.put("/api/admin/homepage-recommendations")
async def put_homepage_recommendations(
    payload: list[HomeRecommendationItem],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[HomeRecommendationItem]:
    state, _ = store.update(lambda draft: setattr(draft, "homepage_recommendations", payload))
    return state.homepage_recommendations


@router.get("/api/admin/permission-rules")
async def get_permission_rules(store: ConfigRepository = Depends(get_store)) -> list[PermissionRule]:
    state = store.load()
    return state.permission_rules


@router.put("/api/admin/permission-rules")
async def put_permission_rules(
    payload: list[PermissionRule],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[PermissionRule]:
    state, _ = store.update(lambda draft: setattr(draft, "permission_rules", payload))
    return state.permission_rules


@router.get("/api/admin/role-policies")
async def get_role_policies(store: ConfigRepository = Depends(get_store)) -> list[RolePolicy]:
    return store.load().role_policies


@router.put("/api/admin/role-policies")
async def put_role_policies(
    payload: list[RolePolicy],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[RolePolicy]:
    state, _ = store.update(lambda draft: setattr(draft, "role_policies", payload))
    return state.role_policies


@router.get("/api/admin/user-accounts")
async def get_user_accounts(store: ConfigRepository = Depends(get_store)) -> list[UserAccount]:
    return store.load().user_accounts


@router.put("/api/admin/user-accounts")
async def put_user_accounts(
    payload: list[UserAccount],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[UserAccount]:
    state, _ = store.update(lambda draft: setattr(draft, "user_accounts", payload))
    return state.user_accounts


@router.get("/api/admin/knowledge-bindings")
async def get_knowledge_bindings(store: ConfigRepository = Depends(get_store)) -> list[KnowledgeBinding]:
    state = store.load()
    return state.knowledge_bindings


@router.put("/api/admin/knowledge-bindings")
async def put_knowledge_bindings(
    payload: list[KnowledgeBinding],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[KnowledgeBinding]:
    state, _ = store.update(lambda draft: setattr(draft, "knowledge_bindings", payload))
    return state.knowledge_bindings


@router.get("/api/admin/skill-policies")
async def get_skill_policies(store: ConfigRepository = Depends(get_store)) -> list[SkillPolicy]:
    state = store.load()
    return state.skill_policies


@router.put("/api/admin/skill-policies")
async def put_skill_policies(
    payload: list[SkillPolicy],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[SkillPolicy]:
    state, _ = store.update(lambda draft: setattr(draft, "skill_policies", payload))
    return state.skill_policies


@router.get("/api/admin/flow-policy")
async def get_flow_policy(store: ConfigRepository = Depends(get_store)) -> FlowPolicyConfig:
    return store.load().flow_policy


@router.put("/api/admin/flow-policy")
async def put_flow_policy(
    payload: FlowPolicyConfig,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> FlowPolicyConfig:
    state, _ = store.update(lambda draft: setattr(draft, "flow_policy", payload))
    return state.flow_policy


@router.get("/api/admin/scenario-packs")
async def get_scenario_packs(store: ConfigRepository = Depends(get_store)) -> list[ScenarioPackConfig]:
    return store.load().scenario_packs


@router.put("/api/admin/scenario-packs")
async def put_scenario_packs(
    payload: list[ScenarioPackConfig],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[ScenarioPackConfig]:
    state, _ = store.update(lambda draft: setattr(draft, "scenario_packs", payload))
    return state.scenario_packs


@router.get("/api/admin/flow-skill-descriptors")
async def get_flow_skill_descriptors(store: ConfigRepository = Depends(get_store)) -> list[FlowSkillDescriptor]:
    return store.load().flow_skill_descriptors


@router.put("/api/admin/flow-skill-descriptors")
async def put_flow_skill_descriptors(
    payload: list[FlowSkillDescriptor],
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[FlowSkillDescriptor]:
    state, _ = store.update(lambda draft: setattr(draft, "flow_skill_descriptors", payload))
    return state.flow_skill_descriptors


@router.get("/api/admin/release-history")
async def get_release_history(store: ConfigRepository = Depends(get_store)) -> list[ReleaseRecord]:
    state = store.load()
    return state.release_history


@router.post("/api/admin/release-history")
async def append_release_history(
    note: str,
    operator: str = "admin",
    version: str = "v1",
    risk_level: str = "low",
    affected_agents: str = "Master,Operations,Marketing,CustomerSuccess",
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
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


@router.get("/api/admin/audit-logs")
async def get_audit_logs(
    session: DbSession,
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> dict[str, Any]:
    from sqlalchemy import func, select

    from ..db.models import AuditLog

    conditions = []
    if actor:
        conditions.append(AuditLog.actor_user_id == actor)
    if action:
        conditions.append(AuditLog.action == action)
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)

    total = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
    ).scalar_one()
    result = await session.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc())
        .limit(min(limit, 200))
        .offset(max(offset, 0))
    )
    rows = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": str(row.id),
                "workspace_id": str(row.workspace_id),
                "actor_user_id": row.actor_user_id,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "request_id": row.request_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }
