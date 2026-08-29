"""Digest and projection builder unit tests (Step 7 PR-J1)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_runtime_snapshot_test.json")

from app.schemas import AdminState
from app.services.runtime_payloads import (
    build_dispatch_config_payload,
    build_scene_selection_payload,
)
from app.services.runtime_snapshot.digest import (
    canonical_json_hash,
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import build_runtime_projection
from app.store import state_hash


def test_canonical_json_hash_matches_state_hash_algorithm() -> None:
    state = AdminState.default()
    assert canonical_json_hash(state.model_dump()) == state_hash(state)


def test_projection_digest_covers_only_projection_content() -> None:
    state = AdminState.default()
    projection = build_runtime_projection(state)
    digest = projection_digest(projection)
    # Same projection content -> same digest; provenance changes never
    # influence it (the function only receives projection content).
    assert projection_digest(projection.model_copy()) == digest
    # schema_version participates in the digest.
    bumped = projection.model_copy(update={"schema_version": 2})
    assert projection_digest(bumped) != digest


def test_snapshot_id_is_deterministic_uuid5() -> None:
    digest = "a" * 64
    assert snapshot_id_for_digest(digest) == snapshot_id_for_digest(digest)
    assert snapshot_id_for_digest(digest).version == 5


def test_snapshot_projection_strips_llm_api_key(monkeypatch) -> None:
    monkeypatch.setenv("MAP_LLM_API_KEY", "super-secret-key")
    state = AdminState.default()

    secretful_scene = build_scene_selection_payload(state)
    assert secretful_scene["route_llm_config"]["api_key"] == "super-secret-key"
    secretful_dispatch = build_dispatch_config_payload(state)
    assert any(
        "api_key" in cfg.get("llm_config", {})
        for cfg in secretful_dispatch["scene_agent_configs"].values()
    )

    secretless_scene = build_scene_selection_payload(state, include_secrets=False)
    assert secretless_scene["route_llm_config"].get("api_key_ref") == "env:MAP_LLM_API_KEY"
    assert "api_key" not in secretless_scene["route_llm_config"]
    secretless_dispatch = build_dispatch_config_payload(state, include_secrets=False)
    for cfg in secretless_dispatch["scene_agent_configs"].values():
        llm_config = cfg.get("llm_config")
        if llm_config is not None:
            assert llm_config.get("api_key_ref") == "env:MAP_LLM_API_KEY"
            assert "api_key" not in llm_config

    projection = build_runtime_projection(state)
    projection_digest(projection)
    serialized = json.dumps(projection.model_dump(), ensure_ascii=False)
    assert "super-secret-key" not in serialized


def test_default_chat_inline_path_keeps_secrets(monkeypatch) -> None:
    monkeypatch.setenv("MAP_LLM_API_KEY", "inline-secret-key")
    state = AdminState.default()
    scene = build_scene_selection_payload(state)
    assert scene["route_llm_config"]["api_key"] == "inline-secret-key"
