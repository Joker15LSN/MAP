"""Master Agent publish/version/diff/rollback endpoints.

Extracted verbatim from ``app.main`` during F-01. URLs, request/response
shapes and operation names are unchanged.
"""

from __future__ import annotations

import difflib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.identity import RequestPrincipal
from ..repositories.config import ConfigRepository
from ..schemas import (
    AdminState,
    MasterAgentConfig,
    MasterPromptVersion,
    MasterPublishRequest,
    MasterRollbackRequest,
    ReleaseRecord,
)
from .deps import get_store, require_admin

router = APIRouter()


def _now_iso() -> str:
    return datetime.now().isoformat()


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


@router.get("/api/admin/master-agent")
async def get_master_agent(store: ConfigRepository = Depends(get_store)) -> MasterAgentConfig:
    state = store.load()
    return state.master_agent


@router.put("/api/admin/master-agent")
async def put_master_agent(
    payload: MasterAgentConfig,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(require_admin),
) -> MasterAgentConfig:
    state, _ = store.update(lambda draft: setattr(draft, "master_agent", payload))
    return state.master_agent


@router.post("/api/admin/master-agent/publish")
async def publish_master_agent(
    payload: MasterPublishRequest,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(require_admin),
) -> dict[str, Any]:
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


@router.get("/api/admin/master-agent/versions")
async def list_master_versions(store: ConfigRepository = Depends(get_store)) -> list[MasterPromptVersion]:
    return store.load().master_agent.prompt_versions


@router.get("/api/admin/master-agent/versions/{version}")
async def get_master_version(
    version: str,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(require_admin),
) -> MasterPromptVersion:
    for item in store.load().master_agent.prompt_versions:
        if item.version == version:
            return item
    raise HTTPException(status_code=404, detail=f"version {version} not found")


@router.get("/api/admin/master-agent/diff")
async def diff_master_versions(
    from_version: str | None = None,
    to_version: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(require_admin),
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


@router.post("/api/admin/master-agent/rollback")
async def rollback_master_agent(
    payload: MasterRollbackRequest,
    store: ConfigRepository = Depends(get_store),
    _: RequestPrincipal = Depends(require_admin),
) -> MasterAgentConfig:
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
