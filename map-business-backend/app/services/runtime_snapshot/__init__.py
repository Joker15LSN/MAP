"""Runtime snapshot module (Step 7 PR-J).

Immutable versioned projection snapshots with a singleton current pointer,
draft -> published -> active -> rolled_back -> retired lifecycle, CAS
activation and crash recovery. J1 provides storage + read path; J2 adds
the lifecycle service.
"""

from __future__ import annotations

from .digest import canonical_json_hash, projection_digest, snapshot_id_for_digest
from .errors import (
    SnapshotAuditWriteError,
    SnapshotConcurrentModificationError,
    SnapshotDigestMismatchError,
    SnapshotError,
    SnapshotNotFoundError,
    SnapshotStateConflictError,
)
from .repository import RuntimeSnapshotRepository
from .schemas import (
    MutationContext,
    RuntimeProjection,
    RuntimeSnapshotRead,
    RuntimeSnapshotRecord,
    build_runtime_projection,
)

__all__ = [
    "MutationContext",
    "RuntimeProjection",
    "RuntimeSnapshotRead",
    "RuntimeSnapshotRecord",
    "RuntimeSnapshotRepository",
    "SnapshotAuditWriteError",
    "SnapshotConcurrentModificationError",
    "SnapshotDigestMismatchError",
    "SnapshotError",
    "SnapshotNotFoundError",
    "SnapshotStateConflictError",
    "build_runtime_projection",
    "canonical_json_hash",
    "projection_digest",
    "snapshot_id_for_digest",
]
