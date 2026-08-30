"""Typed errors for the runtime snapshot module."""

from __future__ import annotations


class SnapshotError(Exception):
    """Base class for runtime snapshot errors."""


class SnapshotNotFoundError(SnapshotError):
    """The requested snapshot id does not exist (or is not readable)."""


class SnapshotStateConflictError(SnapshotError):
    """The snapshot status does not allow the requested transition."""


class SnapshotConcurrentModificationError(SnapshotError):
    """The current pointer changed since the caller read it (CAS failure)."""


class SnapshotDigestMismatchError(SnapshotError):
    """The stored projection does not hash to the stored digest."""


class SnapshotAuditWriteError(SnapshotError):
    """Audit append failed; the product write may have succeeded already."""


class AdminStateUnavailableError(SnapshotError):
    """The singleton PG admin state row is missing.

    Callers must fail closed (never write defaults over a missing row).
    """


class BadAdminStateError(SnapshotError):
    """The PG admin state row exists but failed validation or hash check.

    The row must never be overwritten by defaults.
    """


class RuntimeSnapshotUnavailableError(SnapshotError):
    """Run creation needs a current snapshot but none is configured.

    Fail-closed: callers must map this to 503 + ``RUNTIME_SNAPSHOT_UNAVAILABLE``.
    """

    def __init__(self) -> None:
        self.code = "RUNTIME_SNAPSHOT_UNAVAILABLE"
        self.message = (
            "runtime snapshot unavailable: no active runtime snapshot is configured"
        )
        super().__init__(self.message)
