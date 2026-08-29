"""Service-identity transport for pinned runtime snapshots (Step 7 PR-J6).

The BFF exposes ``GET /internal/v1/runtime-config-snapshots/{id}`` for
service principals only. ``ServiceIdentityRuntimeSnapshotTransport`` is the
ONLY caller map_core uses for flow config: it never follows a current
pointer, never retries, and fails closed on every auth/schema/digest
problem.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
from loguru import logger


class RuntimeSnapshotError(Exception):
    """Base fail-closed error for pinned runtime snapshot loading."""


class RuntimeSnapshotIdMissingError(RuntimeSnapshotError):
    """The caller did not provide a pinned snapshot id/digest."""


class RuntimeSnapshotAuthError(RuntimeSnapshotError):
    """Service identity was missing or rejected by the BFF (401/403)."""


class RuntimeSnapshotNotFoundError(RuntimeSnapshotError):
    """The pinned snapshot does not exist (or is not readable)."""


class RuntimeSnapshotDigestMismatchError(RuntimeSnapshotError):
    """Stored/projected digest did not match the pinned or recomputed digest."""


class RuntimeSnapshotSchemaError(RuntimeSnapshotError):
    """The snapshot body does not match the supported projection schema."""


_PROJECTION_DIGEST_KEYS = (
    "schema_version",
    "scene_selection",
    "dispatch_config",
    "flow_policy",
    "scenario_packs",
    "flow_skill_descriptors",
)


def projection_digest(projection: dict[str, Any]) -> str:
    """Recompute the BFF ``projection_digest`` over the projection only.

    Matches ``app/services/runtime_snapshot/digest.py`` exactly:
    sha256(canonical JSON of {schema_version, scene_selection,
    dispatch_config, flow_policy, scenario_packs, flow_skill_descriptors})
    with sorted keys, ensure_ascii=False and compact separators.
    """
    payload = {key: projection.get(key) for key in _PROJECTION_DIGEST_KEYS}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ServiceIdentityRuntimeSnapshotTransport:
    """Read one immutable runtime snapshot from the BFF internal API.

    The transport never falls back to a current pointer and never retries:
    any network/HTTP/auth/schema/digest problem raises a
    :class:`RuntimeSnapshotError` subclass.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        audience: str = "map-bff",
        timeout_s: float = 6.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.audience = audience
        self.timeout_s = timeout_s

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s),
            trust_env=False,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Service-Name": "map-core",
            "X-Service-Audience": self.audience,
        }

    async def get(self, snapshot_id: str) -> dict[str, Any]:
        """Return the snapshot body (already auth/schema/digest verified)."""
        if not isinstance(self.token, str) or not self.token.strip():
            raise RuntimeSnapshotAuthError(
                "MAP_BFF_SERVICE_TOKEN is missing or empty"
            ) from None

        url = f"{self.base_url}/internal/v1/runtime-config-snapshots/{snapshot_id}"
        try:
            async with self._build_client() as client:
                response = await client.get(url, headers=self._headers())
                status = response.status_code
                if status in (401, 403):
                    raise RuntimeSnapshotAuthError(
                        f"runtime snapshot request rejected with HTTP {status}"
                    )
                if status == 404:
                    raise RuntimeSnapshotNotFoundError(
                        f"runtime snapshot {snapshot_id} not found"
                    )
                if status != 200:
                    raise RuntimeSnapshotError(
                        f"runtime snapshot request failed with HTTP {status}"
                    )
                body = response.json()
                header_digest = response.headers.get("X-MAP-Snapshot-Digest")
        except RuntimeSnapshotError:
            raise
        except Exception as exc:
            logger.warning(f"[RuntimeSnapshotTransport] request failed: {exc}")
            raise RuntimeSnapshotError(
                f"runtime snapshot request failed: {exc}"
            ) from exc

        if not isinstance(body, dict):
            raise RuntimeSnapshotSchemaError(
                "runtime snapshot body is not a JSON object"
            )

        projection = body.get("projection")
        if not isinstance(projection, dict):
            raise RuntimeSnapshotSchemaError(
                "runtime snapshot projection is missing"
            )

        if projection.get("schema_version") != 1:
            raise RuntimeSnapshotSchemaError(
                f"unsupported runtime snapshot schema_version: "
                f"{projection.get('schema_version')!r}"
            )

        body_digest = body.get("digest")
        if not isinstance(body_digest, str) or not body_digest:
            raise RuntimeSnapshotSchemaError(
                "runtime snapshot digest is missing"
            )

        if not isinstance(header_digest, str) or not header_digest:
            raise RuntimeSnapshotDigestMismatchError(
                "runtime snapshot response header digest is missing"
            )

        if not hmac.compare_digest(header_digest, body_digest):
            raise RuntimeSnapshotDigestMismatchError(
                "runtime snapshot header digest does not match body digest"
            )

        recomputed_digest = projection_digest(projection)
        if not hmac.compare_digest(recomputed_digest, body_digest):
            raise RuntimeSnapshotDigestMismatchError(
                "runtime snapshot projection digest mismatch"
            )

        return body
