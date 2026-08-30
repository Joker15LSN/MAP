"""JSON file -> PG admin state + runtime snapshot migration command.

Loads a state file as AdminState (fail-closed), seeds the singleton PG
``admin_state`` row (``INSERT ON CONFLICT DO NOTHING``) and reconciles the
runtime snapshot tables:

- ``--apply`` inserts the snapshot (``ON CONFLICT (digest) DO NOTHING``)
  and seeds the singleton current pointer (``ON CONFLICT DO NOTHING``);
- ``--check`` only reconciles and never writes.

Idempotent rerun inserts no duplicate rows and does not move the pointer
once seeded. Any existing snapshot whose digest differs from the
file-derived digest is reported as a conflict and nothing is written.

Startup does NOT run this automatically: real data migration is owned by
the operator (run it after ``alembic upgrade head`` and before starting
the new BFF image when an old ``admin_state.json`` must be imported).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...db.models import RuntimeSnapshot, RuntimeSnapshotCurrent
from ...db.session import build_engine
from ...store import AdminStateStore, BadStateFileError
from .adapters.admin_state_pg import PgAdminStateRepository
from .digest import projection_digest, snapshot_id_for_digest, state_hash
from .schemas import build_runtime_projection


@dataclass(frozen=True)
class MigrationReport:
    admin_count: int
    admin_hash: str | None
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
    import_admin_state: bool = True,
) -> MigrationReport:
    """Reconcile one state file against PG admin_state + snapshot tables.

    ``apply=True`` writes (idempotent); ``apply=False`` only checks.
    Raises ``SystemExit`` on a bad state file or a digest conflict.
    """
    file_state = _load_admin_state(state_file)
    projection = build_runtime_projection(file_state)
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    file_admin_hash = state_hash(file_state)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        admin_repo = PgAdminStateRepository(session)

        if apply and import_admin_state:
            await admin_repo.seed_if_empty(file_state)

        admin_state = await _load_pg_admin_state(admin_repo)
        if admin_state is None:
            admin_count = 0
            admin_hash = None
        else:
            admin_count = 1
            admin_hash = state_hash(admin_state)

        existing_digests = (
            (await session.execute(select(RuntimeSnapshot.digest))).scalars().all()
        )
        conflicts = sorted(d for d in existing_digests if d != digest)

        if apply:
            if conflicts:
                print(
                    f"CONFLICT: state file digest {digest} does not match existing "
                    f"snapshot digest(s) {','.join(conflicts)}; not writing"
                )
                await session.rollback()
                raise SystemExit(2)

            await session.execute(
                _pg_insert_snapshot(projection, digest, snapshot_id)
            )
            await session.execute(_pg_seed_pointer(snapshot_id, digest))
            await session.commit()

            async with factory() as verify_session:
                matching_count = (
                    await verify_session.execute(
                        select(func.count())
                        .select_from(RuntimeSnapshot)
                        .where(RuntimeSnapshot.digest == digest)
                    )
                ).scalar_one()
                current_digest = (
                    await verify_session.execute(
                        select(RuntimeSnapshot.digest)
                        .join(
                            RuntimeSnapshotCurrent,
                            RuntimeSnapshotCurrent.current_snapshot_id
                            == RuntimeSnapshot.id,
                        )
                        .where(RuntimeSnapshotCurrent.id == 1)
                    )
                ).scalars().one_or_none()
            wrote = True
        else:
            matching_count = sum(1 for d in existing_digests if d == digest)
            current_digest = (
                await session.execute(
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
        admin_count=admin_count,
        admin_hash=admin_hash,
        digest=digest,
        snapshot_id=str(snapshot_id),
        matching_count=matching_count,
        current_digest=current_digest,
        conflict_digests=conflicts,
        wrote=wrote,
    )
    print(
        f"admin_count={admin_count} admin_hash={admin_hash or '-'} "
        f"file_admin_hash={file_admin_hash} "
        f"snapshot_id={report.snapshot_id} digest={digest} "
        f"matching_count={matching_count} current_digest={current_digest or '-'} "
        f"wrote={int(wrote)}"
    )
    return report


async def _load_pg_admin_state(admin_repo: PgAdminStateRepository):
    """Return the current PG admin state, or None when the row is missing."""
    from ...db.models import AdminStateRow
    from ...schemas import AdminState

    row = (
        (
            await admin_repo._session.execute(
                select(AdminStateRow).where(AdminStateRow.id == 1)
            )
        )
        .scalars()
        .one_or_none()
    )
    if row is None:
        return None
    return AdminState.model_validate(row.state_json)


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
            engine,
            args.state_file,
            apply=not args.check,
            import_admin_state=args.import_admin_state,
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
    parser.add_argument(
        "--import-admin-state",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="seed PG admin_state from the file when empty (default: enabled)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
