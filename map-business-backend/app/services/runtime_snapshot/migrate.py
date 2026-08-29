"""JSON -> PG runtime snapshot migration command.

Loads a state file as AdminState (fail-closed), materializes the
runtime projection with ``include_secrets=False``, computes the
deterministic snapshot id/digest and reconciles PG:

- ``--apply`` inserts the snapshot (``ON CONFLICT (digest) DO NOTHING``)
  and seeds the singleton current pointer (``ON CONFLICT DO NOTHING``);
- ``--check`` only reconciles and never writes.

Idempotent rerun inserts no duplicate snapshot and does not move the
pointer once seeded. Any existing snapshot whose digest differs from the
file-derived digest is reported as a conflict and nothing is written.

Startup does NOT run this automatically: real data migration is owned by
the operator.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from ...db.models import RuntimeSnapshot, RuntimeSnapshotCurrent
from ...db.session import build_engine
from ...store import AdminStateStore, BadStateFileError
from .digest import projection_digest, snapshot_id_for_digest
from .schemas import build_runtime_projection


@dataclass(frozen=True)
class MigrationReport:
    digest: str
    snapshot_id: str
    matching_count: int
    current_digest: str | None
    conflict_digests: list[str]
    wrote: bool = False

    @property
    def ok(self) -> bool:
        return not self.conflict_digests


def _load_admin_state(state_file: str):
    path = Path(state_file)
    if not path.exists():
        raise FileNotFoundError(f"state file does not exist: {state_file}")
    store = AdminStateStore(state_file)
    try:
        return store.load()
    except BadStateFileError as exc:
        raise SystemExit(f"BAD_STATE_FILE: {exc}") from exc


async def migrate_state_file(
    engine,
    state_file: str,
    *,
    apply: bool,
) -> MigrationReport:
    """Reconcile one state file against the runtime_snapshots tables.

    ``apply=True`` writes (idempotent); ``apply=False`` only checks.
    Raises ``SystemExit`` on a bad state file or a digest conflict.
    """
    state = _load_admin_state(state_file)
    projection = build_runtime_projection(state)
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)

    async with engine.connect() as conn:
        existing_digests = (
            (await conn.execute(select(RuntimeSnapshot.digest))).scalars().all()
        )
        conflicts = sorted(d for d in existing_digests if d != digest)

        if conflicts:
            print(
                f"CONFLICT: state file digest {digest} does not match existing "
                f"snapshot digest(s) {','.join(conflicts)}; not writing"
            )
            raise SystemExit(2)

        if apply:
            await conn.execute(
                _pg_insert_snapshot(projection, digest, snapshot_id)
            )
            await conn.execute(
                _pg_seed_pointer(snapshot_id, digest)
            )
            await conn.commit()
            matching_count = (
                await conn.execute(
                    select(func.count())
                    .select_from(RuntimeSnapshot)
                    .where(RuntimeSnapshot.digest == digest)
                )
            ).scalar_one()
            current_digest = digest
            wrote = True
        else:
            matching_count = sum(1 for d in existing_digests if d == digest)
            current_digest = (
                await conn.execute(
                    select(RuntimeSnapshot.digest)
                    .join(
                        RuntimeSnapshotCurrent,
                        RuntimeSnapshotCurrent.current_snapshot_id == RuntimeSnapshot.id,
                    )
                    .where(RuntimeSnapshotCurrent.id == 1)
                )
            ).scalars().one_or_none()
            wrote = False

    report = MigrationReport(
        digest=digest,
        snapshot_id=str(snapshot_id),
        matching_count=matching_count,
        current_digest=current_digest,
        conflict_digests=conflicts,
        wrote=wrote,
    )
    print(
        f"snapshot_id={report.snapshot_id} digest={digest} "
        f"matching_count={matching_count} current_digest={current_digest or '-'} "
        f"wrote={int(wrote)}"
    )
    return report


def _pg_insert_snapshot(projection, digest: str, snapshot_id):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    table = RuntimeSnapshot.__table__
    return (
        pg_insert(table)
        .values(
            id=snapshot_id,
            schema_version=projection.schema_version,
            projection=projection.model_dump(mode="json"),
            digest=digest,
            status="active",
            parent_id=None,
        )
        .on_conflict_do_nothing(index_elements=["digest"])
    )


def _pg_seed_pointer(snapshot_id, digest: str):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return (
        pg_insert(RuntimeSnapshotCurrent.__table__)
        .values(id=1, current_snapshot_id=snapshot_id, current_digest=digest)
        .on_conflict_do_nothing(index_elements=["id"])
    )


async def _main_async(args: argparse.Namespace) -> int:
    engine = build_engine()
    try:
        report = await migrate_state_file(
            engine, args.state_file, apply=not args.check
        )
        return 0 if report.ok else 2
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, help="AdminState JSON file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="reconcile only; never write",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=True,
        help="apply idempotent migration (default)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
