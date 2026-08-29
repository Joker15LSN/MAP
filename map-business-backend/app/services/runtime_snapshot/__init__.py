"""Runtime snapshot module (Step 7 PR-J).

Immutable versioned projection snapshots with a singleton current pointer,
draft -> published -> active -> rolled_back -> retired lifecycle, CAS
activation and crash recovery.
"""

from __future__ import annotations

from .digest import canonical_json_hash, projection_digest, snapshot_id_for_digest
from .errors import (
    RuntimeSnapshotUnavailableError,
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
from .service import (
    AdminStateMutationStore,
    RuntimeSnapshotService,
    reconcile_runtime_snapshot_mutations,
)

__all__ = [
    "AdminStateMutationStore",
    "MutationContext",
    "RuntimeProjection",
    "RuntimeSnapshotRead",
    "RuntimeSnapshotRecord",
    "RuntimeSnapshotRepository",
    "RuntimeSnapshotService",
    "RuntimeSnapshotUnavailableError",
    "SnapshotAuditWriteError",
    "SnapshotConcurrentModificationError",
    "SnapshotDigestMismatchError",
    "SnapshotError",
    "SnapshotNotFoundError",
    "SnapshotStateConflictError",
    "build_runtime_projection",
    "canonical_json_hash",
    "projection_digest",
    "reconcile_runtime_snapshot_mutations",
    "snapshot_id_for_digest",
]
