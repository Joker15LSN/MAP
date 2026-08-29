"""Canonical digest helpers for runtime snapshots.

``canonical_json_hash`` is the same canonical JSON algorithm as
``app.store.state_hash`` (sorted keys, ensure_ascii=False, compact
separators) so snapshot digests and admin state hashes come from one
algorithm family; a unit test locks the equivalence.

``projection_digest`` covers ONLY ``schema_version`` + projection content
— provenance fields (id, parent_id, status, created_at) never influence
it.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from .schemas import RuntimeProjection

# Fixed namespace for deterministic snapshot ids (uuid5).
SNAPSHOT_ID_NAMESPACE = uuid.UUID("7d9d4f2a-1e5b-4e6c-8a2f-3b0c9d1f5a6e")


def canonical_json_hash(obj: object) -> str:
    """SHA-256 over canonical JSON (sorted keys, compact separators)."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def projection_digest(projection: RuntimeProjection | dict) -> str:
    """Digest of schema_version + projection content (no provenance)."""
    payload = (
        projection.model_dump(mode="json")
        if isinstance(projection, RuntimeProjection)
        else projection
    )
    return canonical_json_hash(payload)


def snapshot_id_for_digest(digest: str) -> uuid.UUID:
    """Deterministic snapshot id derived from the projection digest."""
    return uuid.uuid5(SNAPSHOT_ID_NAMESPACE, digest)
