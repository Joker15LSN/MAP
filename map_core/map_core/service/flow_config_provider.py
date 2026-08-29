from __future__ import annotations

import hmac
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from ..schema.flow_domain_schema import (
    FlowConfigSchema,
    ScenarioPackSchema,
    SkillDescriptorSchema,
)
from .runtime_snapshot_transport import (
    RuntimeSnapshotDigestMismatchError,
    RuntimeSnapshotError,
    RuntimeSnapshotIdMissingError,
    RuntimeSnapshotSchemaError,
)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class FlowConfigSnapshot(BaseModel):
    source: Literal["static", "snapshot"] = "snapshot"
    fetched_at: str = Field(default_factory=lambda: _now().isoformat())
    updated_at: str | None = None
    flow_policy: FlowConfigSchema = Field(default_factory=FlowConfigSchema)
    scenario_packs: list[ScenarioPackSchema] = Field(default_factory=list)
    flow_skill_descriptors: list[SkillDescriptorSchema] = Field(default_factory=list)
    stale: bool = False


class FlowConfigProvider:
    """Load flow runtime config from a pinned immutable snapshot.

    The provider has no cache, no current-pointer fetch and no static
    fallback: it delegates to a transport that reads one exact snapshot id
    and fails closed when the pinned id/digest is missing or the returned
    digest does not match.
    """

    def __init__(self, transport) -> None:
        self._transport = transport

    async def get_snapshot(
        self,
        *,
        snapshot_id: str | None,
        expected_digest: str | None,
    ) -> FlowConfigSnapshot:
        if not snapshot_id or not expected_digest:
            raise RuntimeSnapshotIdMissingError(
                "runtime snapshot id and digest are required (fail-closed)"
            )

        payload = await self._transport.get(snapshot_id)
        if not isinstance(payload, dict):
            raise RuntimeSnapshotError(
                "runtime snapshot transport returned a non-object payload"
            )

        body_digest = payload.get("digest")
        if not isinstance(body_digest, str) or not hmac.compare_digest(
            body_digest, expected_digest
        ):
            raise RuntimeSnapshotDigestMismatchError(
                "runtime snapshot digest does not match the pinned digest"
            )

        projection = payload.get("projection") or {}
        flow_policy_raw = projection.get("flow_policy") or {}
        scenario_packs_raw = projection.get("scenario_packs") or []
        skill_descriptors_raw = projection.get("flow_skill_descriptors") or []

        try:
            return FlowConfigSnapshot(
                source="snapshot",
                fetched_at=_now().isoformat(),
                updated_at=None,
                flow_policy=FlowConfigSchema.model_validate(flow_policy_raw),
                scenario_packs=[
                    ScenarioPackSchema.model_validate(item)
                    for item in scenario_packs_raw
                ],
                flow_skill_descriptors=[
                    SkillDescriptorSchema.model_validate(item)
                    for item in skill_descriptors_raw
                ],
                stale=False,
            )
        except ValidationError as exc:
            raise RuntimeSnapshotSchemaError(
                f"runtime snapshot projection validation failed: {exc}"
            ) from exc
