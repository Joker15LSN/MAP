"""Runtime snapshot module (Step 7 PR-J).

Immutable versioned projection snapshots with a singleton current pointer,
draft -> published -> active -> rolled_back -> retired lifecycle, CAS
activation and crash recovery.
"""

from __future__ import annotations

from .digest import (
    canonical_json_hash,
    projection_digest,
    snapshot_id_for_digest,
    state_hash,
)
from .errors import (
    AdminStateUnavailableError,
    BadAdminStateError,
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
from .service import RuntimeSnapshotService

__all__ = [
    "AdminStateUnavailableError",
    "BadAdminStateError",
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
    "snapshot_id_for_digest",
    "state_hash",
]
