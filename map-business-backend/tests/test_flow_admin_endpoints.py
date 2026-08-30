import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def client():
    # TestClient as context manager runs lifespan, which seeds the PG
    # admin state singleton idempotently (J7b: no file store anymore).
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_flow_policy_roundtrip(client) -> None:
    response = client.get("/api/admin/flow-policy")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario_policy"]["mode"] in {"auto", "manual"}

    payload["max_node_budget"] = 16
    update_response = client.put("/api/admin/flow-policy", json=payload)
    assert update_response.status_code == 200
    assert update_response.json()["max_node_budget"] == 16

    verify_response = client.get("/api/admin/flow-policy")
    assert verify_response.status_code == 200
    assert verify_response.json()["max_node_budget"] == 16


def test_scenario_pack_roundtrip(client) -> None:
    scenario_packs = [
        {
            "scenario_id": "cross_domain_serial_analysis",
            "display_name": "跨域串行分析",
            "domain": "general_enterprise",
            "trigger_intents": ["跨业务域", "串行", "心流"],
            "required_agents": ["Operations", "General_Assistant"],
            "optional_agents": ["Marketing"],
            "auth_scopes": ["scenario:cross_domain_serial_analysis:read"],
            "status": "active",
        }
    ]
    response = client.put("/api/admin/scenario-packs", json=scenario_packs)
    assert response.status_code == 200
    assert response.json()[0]["scenario_id"] == "cross_domain_serial_analysis"

    verify_response = client.get("/api/admin/scenario-packs")
    assert verify_response.status_code == 200
    assert len(verify_response.json()) == 1


def test_flow_skill_descriptor_roundtrip(client) -> None:
    skill_descriptors = [
        {
            "skill_id": "ops.ask_database.v1",
            "name": "ask_database",
            "display_name": "经营问表",
            "tool_name": "ask_database_agent",
            "mount_agents": ["Operations"],
            "required_scopes": ["skill:ask_database:execute"],
            "status": "active",
        }
    ]
    response = client.put("/api/admin/flow-skill-descriptors", json=skill_descriptors)
    assert response.status_code == 200
    assert response.json()[0]["tool_name"] == "ask_database_agent"

    verify_response = client.get("/api/admin/flow-skill-descriptors")
    assert verify_response.status_code == 200
    assert verify_response.json()[0]["skill_id"] == "ops.ask_database.v1"


def test_flow_runtime_snapshot_contains_flow_sections(client) -> None:
    response = client.get("/api/admin/flow-runtime-snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert "flow_policy" in payload
    assert "scenario_packs" in payload
    assert "flow_skill_descriptors" in payload
    assert "mcp_servers" in payload
    assert "skills" in payload


def test_master_prompt_publish_diff_and_rollback(client) -> None:
    master_response = client.get("/api/admin/master-agent")
    assert master_response.status_code == 200
    master = master_response.json()
    original_version = master["current_version"]
    release_version = f"pytest-{uuid.uuid4().hex[:8]}"

    master["route_prompt"] = "pytest route prompt"
    master["summary_prompt"] = "pytest summary prompt"
    put_response = client.put("/api/admin/master-agent", json=master)
    assert put_response.status_code == 200

    publish_response = client.post(
        "/api/admin/master-agent/publish",
        json={"operator": "pytest", "note": "test publish", "version": release_version},
    )
    assert publish_response.status_code == 200
    assert publish_response.json()["version"]["version"] == release_version
    assert "pytest route prompt" in publish_response.json()["diff"]

    diff_response = client.get(
        f"/api/admin/master-agent/diff?from={original_version}&to={release_version}"
    )
    assert diff_response.status_code == 200
    assert "route_prompt" in diff_response.json()["diff"]

    rollback_response = client.post(
        "/api/admin/master-agent/rollback",
        json={"version": original_version, "operator": "pytest", "note": "restore"},
    )
    assert rollback_response.status_code == 200
    assert rollback_response.json()["current_version"] == original_version


def test_mcp_skill_upload_and_runtime_snapshot(client) -> None:
    server_id = f"pytest-mcp-{uuid.uuid4().hex[:8]}"
    put_response = client.put(
        "/api/admin/mcp-servers",
        json=[
            {
                "server_id": server_id,
                "display_name": "Pytest MCP",
                "transport": "streamable_http",
                "enabled": True,
                "url": "http://127.0.0.1:9/mcp",
                "tools": [
                    {
                        "name": "echo",
                        "description": "echo tool",
                        "input_schema": {"type": "object"},
                        "enabled": True,
                    }
                ],
            }
        ],
    )
    assert put_response.status_code == 200
    assert put_response.json()[0]["server_id"] == server_id

    upload_response = client.post(
        "/api/admin/skills/upload",
        json={
            "filename": "pytest_skill.md",
            "content": "# Pytest Skill\n\nAnswer with a short test summary.",
            "metadata": {"description": "pytest uploaded skill"},
            "mount_agents": ["Operations"],
        },
    )
    assert upload_response.status_code == 200
    skill = upload_response.json()
    assert skill["source"] == "manual_upload"

    snapshot_response = client.get("/api/admin/flow-runtime-snapshot")
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    assert any(item["server_id"] == server_id for item in snapshot["mcp_servers"])
    assert any(item["skill_id"] == skill["skill_id"] for item in snapshot["skills"])
