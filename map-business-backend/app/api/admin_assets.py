"""Business Agent / MCP server / uploaded Skill endpoints.

Extracted verbatim from ``app.main`` during F-01. URLs, request/response
shapes and operation names are unchanged.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..core.identity import RequestPrincipal
from ..core_client import MapCoreClient
from ..db.session import DbSession
from ..repositories.config import ConfigRepository
from ..schemas import (
    AdminState,
    BusinessAgentConfig,
    BusinessAgentTestChatRequest,
    FlowSkillDescriptor,
    McpServerConfig,
    McpToolConfig,
    SkillUploadRequest,
    UploadedSkill,
)
from ..services.audit import admin_write_guard
from ..services.runtime_payloads import (
    build_dispatch_config_payload,
    build_scene_selection_payload,
    skill_runtime_tool_name,
    slugify,
)
from ..services.runtime_snapshot.schemas import MutationContext
from .deps import get_core_client, get_principal, get_runtime_snapshots, get_store

router = APIRouter()


async def _audited_update(
    request: Request,
    session: DbSession,
    resource_type: str,
    resource_id: str,
    action: str,
    updater,
):
    """Route all AdminState writes through RuntimeSnapshotService
    (no router-level store.update may bypass the audit chain)."""
    context = MutationContext(
        principal=get_principal(request),
        request=request,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
    )
    return await get_runtime_snapshots(request, session).apply_change(
        session, context, updater
    )


def _now_iso() -> str:
    return datetime.now().isoformat()


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
    if getattr(request.state, "request_id", None):
        headers["X-Request-ID"] = request.state.request_id
    if getattr(request.state, "session_id", None):
        headers["X-Session-ID"] = request.state.session_id
    if getattr(request.state, "workspace_id", None):
        headers["X-Workspace-ID"] = request.state.workspace_id
    for propagation_header in ("traceparent", "tracestate", "baggage"):
        value = request.headers.get(propagation_header)
        if value:
            headers[propagation_header] = value
    return headers


@router.get("/api/admin/business-agents")
async def get_business_agents(
    store: ConfigRepository = Depends(get_store),
) -> list[BusinessAgentConfig]:
    state = await store.load()
    return state.business_agents


@router.post("/api/admin/business-agents")
async def post_business_agent(
    payload: BusinessAgentConfig,
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> BusinessAgentConfig:
    now = datetime.now().isoformat()

    def _append(draft: Any) -> BusinessAgentConfig:
        exists = any(item.agent_code == payload.agent_code for item in draft.business_agents)
        if exists:
            raise HTTPException(
                status_code=409, detail=f"agent {payload.agent_code} already exists"
            )
        created = payload.model_copy(update={"last_updated": now})
        draft.business_agents.append(created)
        return created

    _, created = await _audited_update(
        request, session, "business_agent", payload.agent_code, "create", _append
    )
    return created


@router.put("/api/admin/business-agents/{agent_code}")
async def put_business_agent(
    agent_code: str,
    payload: BusinessAgentConfig,
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> BusinessAgentConfig:
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

    _, updated = await _audited_update(
        request, session, "business_agent", agent_code, "update", _update
    )
    return updated


@router.post("/api/admin/business-agents/{agent_code}/test-chat")
async def test_business_agent(
    agent_code: str,
    payload: BusinessAgentTestChatRequest,
    request: Request,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    store: ConfigRepository = Depends(get_store),
    core_client: MapCoreClient = Depends(get_core_client),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> dict[str, Any]:
    state = await store.load()
    agent = payload.agent
    if agent is None:
        agent = next(
            (item for item in state.business_agents if item.agent_code == agent_code), None
        )
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_code} not found")
    if agent.agent_code != agent_code:
        raise HTTPException(status_code=400, detail="agent_code in path and body must match")

    temp_state = state.model_copy(update={"business_agents": [agent]})
    dispatch_payload = build_dispatch_config_payload(temp_state)
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
        "scene_selection": build_scene_selection_payload(temp_state),
    }
    try:
        return await core_client.chat_by_path(
            "/global_domain/debug/scene_agent/run",
            debug_payload,
            headers=headers,
        )
    except Exception as exc:  # noqa: BLE001
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


@router.get("/api/admin/mcp-servers")
async def get_mcp_servers(store: ConfigRepository = Depends(get_store)) -> list[McpServerConfig]:
    return (await store.load()).mcp_servers


@router.put("/api/admin/mcp-servers")
async def put_mcp_servers(
    payload: list[McpServerConfig],
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[McpServerConfig]:
    state, _ = await _audited_update(
        request,
        session,
        "mcp_server",
        "collection",
        "update",
        lambda draft: setattr(draft, "mcp_servers", payload),
    )
    return state.mcp_servers


@router.post("/api/admin/mcp-servers")
async def post_mcp_server(
    payload: McpServerConfig,
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> McpServerConfig:
    def _upsert(draft: AdminState) -> McpServerConfig:
        for idx, item in enumerate(draft.mcp_servers):
            if item.server_id == payload.server_id:
                draft.mcp_servers[idx] = payload
                return payload
        draft.mcp_servers.insert(0, payload)
        return payload

    _, server = await _audited_update(
        request, session, "mcp_server", payload.server_id, "upsert", _upsert
    )
    return server


async def _probe_mcp_tools(server: McpServerConfig) -> tuple[list[McpToolConfig], str]:
    """Best-effort MCP tool discovery without storing credentials."""
    now = _now_iso()
    if server.transport == "stdio":
        # Backend config should not launch arbitrary local commands just to render admin UI.
        return (
            [tool.model_copy(update={"last_seen_at": now}) for tool in server.tools],
            "stdio_configured",
        )

    if not server.url.strip():
        return (server.tools, "missing_url")

    payloads = [
        {"jsonrpc": "2.0", "id": "map-tools-list", "method": "tools/list", "params": {}},
        {"method": "tools/list", "params": {}},
    ]
    headers = {
        key: value for key, value in server.headers.items() if isinstance(value, str) and value
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
                            if isinstance(item, dict) and str(item.get("name") or "").strip()
                        ],
                        "ok",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
            return (server.tools, f"refresh_failed: {last_error or 'invalid tools/list response'}")
    except Exception as exc:  # noqa: BLE001
        return (server.tools, f"refresh_failed: {exc}")


@router.post("/api/admin/mcp-servers/{server_id}/refresh-tools")
async def refresh_mcp_server_tools(
    server_id: str,
    request: Request,
    session: DbSession,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(admin_write_guard),
) -> McpServerConfig:
    state = await store.load()
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

    _, updated = await _audited_update(
        request, session, "mcp_server", server_id, "refresh_tools", _update
    )
    return updated


@router.get("/api/admin/skills")
async def get_uploaded_skills(store: ConfigRepository = Depends(get_store)) -> list[UploadedSkill]:
    return (await store.load()).skills


@router.put("/api/admin/skills")
async def put_uploaded_skills(
    payload: list[UploadedSkill],
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> list[UploadedSkill]:
    def _replace(draft: AdminState) -> list[UploadedSkill]:
        draft.skills = payload
        _sync_uploaded_skills_to_skillhub(draft)
        return draft.skills

    _, skills = await _audited_update(
        request, session, "skill", "collection", "update", _replace
    )
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
                    raise HTTPException(
                        status_code=400, detail=f"invalid skill.json: {exc}"
                    ) from exc
            return skill_content, metadata

    return raw_content.decode("utf-8"), dict(payload.metadata)


def _skill_from_upload(payload: SkillUploadRequest) -> UploadedSkill:
    content, metadata = _decode_skill_upload(payload)
    raw_name = str(metadata.get("name") or payload.filename.rsplit(".", 1)[0]).strip()
    skill_id = str(metadata.get("skill_id") or slugify(raw_name, prefix="skill"))
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
            tool_name=skill_runtime_tool_name(skill.skill_id),
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


@router.post("/api/admin/skills/upload")
async def upload_skill(
    payload: SkillUploadRequest,
    request: Request,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
) -> UploadedSkill:
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

    _, skill = await _audited_update(
        request, session, "skill", uploaded.skill_id, "upload", _upsert
    )
    return skill
