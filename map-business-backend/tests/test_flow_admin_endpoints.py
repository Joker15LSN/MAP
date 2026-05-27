import os

from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_test_state.json")

from app.main import app


client = TestClient(app)


def test_flow_policy_roundtrip() -> None:
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


def test_scenario_pack_roundtrip() -> None:
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


def test_flow_skill_descriptor_roundtrip() -> None:
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


def test_flow_runtime_snapshot_contains_flow_sections() -> None:
    response = client.get("/api/admin/flow-runtime-snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert "flow_policy" in payload
    assert "scenario_packs" in payload
    assert "flow_skill_descriptors" in payload
