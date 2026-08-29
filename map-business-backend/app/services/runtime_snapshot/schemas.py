"""Pydantic schemas for runtime snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ...core.identity import RequestPrincipal
from ...schemas import AdminState
from ..runtime_payloads import build_dispatch_config_payload, build_scene_selection_payload


class RuntimeProjection(BaseModel):
    """The runtime-relevant projection of an AdminState (secrets stripped).

    ``schema_version`` is the projection contract version; unknown major
    versions must be rejected by readers (fail-closed).
    """

    schema_version: Literal[1] = 1
    scene_selection: dict[str, Any]
    dispatch_config: dict[str, Any]
    flow_policy: dict[str, Any]
    scenario_packs: list[dict[str, Any]]
    flow_skill_descriptors: list[dict[str, Any]]


class RuntimeSnapshotRecord(BaseModel):
    """A stored snapshot row with provenance."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schema_version: int
    digest: str
    parent_id: uuid.UUID | None
    status: str
    created_at: datetime
    projection: RuntimeProjection


class RuntimeSnapshotRead(BaseModel):
    """Public read projection for the internal read route."""

    id: uuid.UUID
    schema_version: int
    digest: str
    parent_id: uuid.UUID | None
    created_at: datetime
    projection: RuntimeProjection


class MutationContext(BaseModel):
    """The original request context carried by a runtime snapshot mutation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    principal: RequestPrincipal
    request: Any
    resource_type: str
    resource_id: str
    action: str


def build_runtime_projection(state: AdminState) -> RuntimeProjection:
    """Materialize the runtime projection with secrets stripped.

    Uses the existing payload builders with ``include_secrets=False`` so
    ``MAP_LLM_API_KEY`` becomes ``api_key_ref`` and never enters the
    digest. Flow policy / scenario packs / flow skill descriptors mirror
    the runtime resource payload semantics.
    """
    return RuntimeProjection(
        schema_version=1,
        scene_selection=build_scene_selection_payload(state, include_secrets=False),
        dispatch_config=build_dispatch_config_payload(state, include_secrets=False),
        flow_policy=state.flow_policy.model_dump(),
        scenario_packs=[item.model_dump() for item in state.scenario_packs],
        flow_skill_descriptors=[
            item.model_dump() for item in state.flow_skill_descriptors if item.status == "active"
        ],
    )
