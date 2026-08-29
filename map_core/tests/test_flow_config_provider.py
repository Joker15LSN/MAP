import asyncio
import uuid

import httpx
import pytest

from map_core.schema.flow_domain_schema import (
    FlowConfigSchema,
    ScenarioPackSchema,
    SkillDescriptorSchema,
)
from map_core.service.flow_config_provider import FlowConfigProvider
from map_core.service.runtime_snapshot_transport import (
    RuntimeSnapshotAuthError,
    RuntimeSnapshotDigestMismatchError,
    RuntimeSnapshotIdMissingError,
    RuntimeSnapshotNotFoundError,
    RuntimeSnapshotSchemaError,
    ServiceIdentityRuntimeSnapshotTransport,
    projection_digest,
)

SNAPSHOT_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _projection(**overrides) -> dict:
    projection = {
        "schema_version": 1,
        "scene_selection": {},
        "dispatch_config": {},
        "flow_policy": {},
        "scenario_packs": [],
        "flow_skill_descriptors": [],
    }
    projection.update(overrides)
    return projection


def _snapshot_body(
    *,
    projection: dict | None = None,
    digest: str | None = None,
    schema_version: int | None = None,
) -> dict:
    proj = projection or _projection()
    if schema_version is not None:
        proj = {**proj, "schema_version": schema_version}
    return {
        "id": SNAPSHOT_ID,
        "schema_version": proj.get("schema_version"),
        "digest": digest or projection_digest(proj),
        "parent_id": None,
        "created_at": "2025-01-01T00:00:00Z",
        "projection": proj,
    }


def _transport_with(handler, *, token: str = "secret") -> ServiceIdentityRuntimeSnapshotTransport:
    transport = ServiceIdentityRuntimeSnapshotTransport(
        base_url="http://bff.test/",
        token=token,
        audience="map-bff",
    )
    transport._build_client = lambda: httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        timeout=6.0,
    )
    return transport


def _ok_handler(request: httpx.Request) -> httpx.Response:
    body = _snapshot_body()
    return httpx.Response(
        200,
        json=body,
        headers={"X-MAP-Snapshot-Digest": body["digest"]},
    )


def test_transport_get_returns_snapshot_body_on_success() -> None:
    transport = _transport_with(_ok_handler)

    body = asyncio.run(transport.get(SNAPSHOT_ID))

    assert body["id"] == SNAPSHOT_ID
    assert body["digest"] == projection_digest(_projection())
    assert body["projection"]["schema_version"] == 1


@pytest.mark.parametrize("status", [401, 403])
def test_transport_auth_statuses_fail_closed(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    transport = _transport_with(handler)

    with pytest.raises(RuntimeSnapshotAuthError):
        asyncio.run(transport.get(SNAPSHOT_ID))


def test_transport_404_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = _transport_with(handler)

    with pytest.raises(RuntimeSnapshotNotFoundError):
        asyncio.run(transport.get(SNAPSHOT_ID))


def test_transport_header_body_digest_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_snapshot_body(),
            headers={"X-MAP-Snapshot-Digest": OTHER_DIGEST},
        )

    transport = _transport_with(handler)

    with pytest.raises(RuntimeSnapshotDigestMismatchError):
        asyncio.run(transport.get(SNAPSHOT_ID))


def test_transport_local_projection_digest_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _snapshot_body(
            projection=_projection(scene_selection={"x": 1}),
            digest=DIGEST,
        )
        return httpx.Response(
            200,
            json=body,
            headers={"X-MAP-Snapshot-Digest": body["digest"]},
        )

    transport = _transport_with(handler)

    with pytest.raises(RuntimeSnapshotDigestMismatchError):
        asyncio.run(transport.get(SNAPSHOT_ID))


def test_transport_schema_version_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_snapshot_body(schema_version=2),
            headers={"X-MAP-Snapshot-Digest": DIGEST},
        )

    transport = _transport_with(handler)

    with pytest.raises(RuntimeSnapshotSchemaError):
        asyncio.run(transport.get(SNAPSHOT_ID))


def test_transport_missing_token_fails_closed_without_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    transport = _transport_with(handler, token="")

    with pytest.raises(RuntimeSnapshotAuthError):
        asyncio.run(transport.get(SNAPSHOT_ID))

    assert called is False


def _provider_with_fake_transport(payload) -> FlowConfigProvider:
    class FakeTransport:
        def __init__(self) -> None:
            self.called_with: str | None = None

        async def get(self, snapshot_id: str) -> dict:
            self.called_with = snapshot_id
            return payload

    return FlowConfigProvider(FakeTransport())


def test_provider_requires_snapshot_id_and_digest() -> None:
    provider = _provider_with_fake_transport(_snapshot_body())

    with pytest.raises(RuntimeSnapshotIdMissingError):
        asyncio.run(provider.get_snapshot(snapshot_id=None, expected_digest=DIGEST))
    with pytest.raises(RuntimeSnapshotIdMissingError):
        asyncio.run(provider.get_snapshot(snapshot_id=SNAPSHOT_ID, expected_digest=None))


def test_provider_digest_mismatch_raises() -> None:
    provider = _provider_with_fake_transport(_snapshot_body())

    with pytest.raises(RuntimeSnapshotDigestMismatchError):
        asyncio.run(
            provider.get_snapshot(
                snapshot_id=SNAPSHOT_ID,
                expected_digest=OTHER_DIGEST,
            )
        )


def test_provider_success_returns_pinned_snapshot() -> None:
    payload = _snapshot_body(
        projection=_projection(
            flow_policy=FlowConfigSchema(max_node_budget=21).model_dump(),
            scenario_packs=[
                ScenarioPackSchema(
                    scenario_id="s1",
                    display_name="S1",
                    domain="d1",
                ).model_dump()
            ],
            flow_skill_descriptors=[
                SkillDescriptorSchema(
                    skill_id="k1",
                    name="k1",
                    display_name="k1",
                    tool_name="ask_database_agent",
                ).model_dump()
            ],
        )
    )
    provider = _provider_with_fake_transport(payload)

    snapshot = asyncio.run(
        provider.get_snapshot(
            snapshot_id=SNAPSHOT_ID,
            expected_digest=payload["digest"],
        )
    )

    assert snapshot.source == "snapshot"
    assert snapshot.stale is False
    assert snapshot.updated_at is None
    assert snapshot.flow_policy.max_node_budget == 21
    assert snapshot.scenario_packs[0].scenario_id == "s1"
    assert snapshot.flow_skill_descriptors[0].skill_id == "k1"
