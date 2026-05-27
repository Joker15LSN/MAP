from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from ..schema.flow_domain_schema import (
    FlowConfigSchema,
    ScenarioPackSchema,
    SkillDescriptorSchema,
)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class FlowConfigSnapshot(BaseModel):
    source: Literal["remote", "cache", "static"] = "static"
    fetched_at: str = Field(default_factory=lambda: _now().isoformat())
    updated_at: str | None = None
    flow_policy: FlowConfigSchema = Field(default_factory=FlowConfigSchema)
    scenario_packs: list[ScenarioPackSchema] = Field(default_factory=list)
    flow_skill_descriptors: list[SkillDescriptorSchema] = Field(default_factory=list)
    stale: bool = False


class FlowConfigProvider:
    """Load flow runtime config from BFF admin snapshot API with local cache."""

    _instance: "FlowConfigProvider | None" = None

    def __init__(self) -> None:
        bff_origin = os.getenv("MAP_BFF_API_ORIGIN", "http://backend-service:18080")
        self.snapshot_url = os.getenv(
            "MAP_FLOW_CONFIG_SNAPSHOT_URL",
            f"{bff_origin.rstrip('/')}/api/admin/flow-runtime-snapshot",
        )
        ttl_s = os.getenv("MAP_FLOW_CONFIG_CACHE_TTL_S", "10").strip()
        self.cache_ttl_s = int(ttl_s) if ttl_s.isdigit() else 10
        enabled_raw = os.getenv("MAP_FLOW_CONFIG_FETCH_ENABLED", "true").strip().lower()
        self.fetch_enabled = enabled_raw not in {"0", "false", "off", "no"}
        self._cache: FlowConfigSnapshot | None = None
        self._cache_expire_at: datetime | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> "FlowConfigProvider":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def get_snapshot(self) -> FlowConfigSnapshot:
        async with self._lock:
            if self._is_cache_fresh():
                assert self._cache is not None
                return self._cache.model_copy(
                    update={
                        "source": "cache",
                        "fetched_at": _now().isoformat(),
                        "stale": False,
                    }
                )

            if not self.fetch_enabled:
                return self._build_static_snapshot()

            remote = await self._try_fetch_remote()
            if remote is not None:
                self._cache = remote
                self._cache_expire_at = _now() + timedelta(seconds=self.cache_ttl_s)
                return remote

            if self._cache is not None:
                return self._cache.model_copy(
                    update={
                        "source": "cache",
                        "fetched_at": _now().isoformat(),
                        "stale": True,
                    }
                )
            return self._build_static_snapshot()

    def _is_cache_fresh(self) -> bool:
        if self._cache is None or self._cache_expire_at is None:
            return False
        return _now() < self._cache_expire_at

    async def _try_fetch_remote(self) -> FlowConfigSnapshot | None:
        timeout = httpx.Timeout(timeout=6.0, connect=2.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self.snapshot_url)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning(
                f"[FlowConfigProvider] failed to load remote snapshot from {self.snapshot_url}: {exc}"
            )
            return None

        try:
            flow_policy_raw = payload.get("flow_policy") or {}
            scenario_packs_raw = payload.get("scenario_packs") or []
            skill_descriptors_raw = payload.get("flow_skill_descriptors") or []

            return FlowConfigSnapshot(
                source="remote",
                fetched_at=_now().isoformat(),
                updated_at=payload.get("updated_at"),
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
        except Exception as exc:
            logger.warning(
                f"[FlowConfigProvider] invalid remote snapshot payload: {exc}"
            )
            return None

    @staticmethod
    def _build_static_snapshot() -> FlowConfigSnapshot:
        # Production-safe fallback: no implicit built-in scenario/skill packs.
        # Runtime should be driven by admin config; when unavailable, flow falls back to global.
        return FlowConfigSnapshot(
            source="static",
            fetched_at=_now().isoformat(),
            flow_policy=FlowConfigSchema(),
            scenario_packs=[],
            flow_skill_descriptors=[],
            stale=False,
        )
