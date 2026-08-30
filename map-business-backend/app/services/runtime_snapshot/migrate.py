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
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...db.models import RuntimeSnapshot, RuntimeSnapshotCurrent
from ...db.session import build_engine
from ...schemas import AdminState
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


def _load_admin_state(state_file: str) -> AdminState:
    """Read a legacy admin state JSON file (fail-closed).

    The old file-backed store was deleted in Step 7 PR-J7b; this reader
    preserves the same validation and legacy-payload normalization so an
    operator can still import an old ``admin_state.json`` exactly once.
    """
    path = Path(state_file)
    if not path.exists():
        raise FileNotFoundError(f"state file does not exist: {state_file}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"BAD_STATE_FILE: state file is not valid JSON: {exc}") from exc
    payload = _migrate_payload(payload)
    try:
        return AdminState.model_validate(payload)
    except ValidationError as exc:
        raise SystemExit(
            f"BAD_STATE_FILE: state file failed validation (kept untouched): {exc}"
        ) from exc


def _migrate_payload(payload: dict) -> dict:
    """Normalize persisted admin JSON across schema revisions.

    Kept byte-for-byte compatible with the deleted file-backed store so
    legacy state files import exactly as the old ``AdminStateStore.load``
    would have read them.
    """
    if not isinstance(payload, dict):
        return payload

    master = payload.get("master_agent")
    if isinstance(master, dict):
        for legacy_key in (
            "enabled",
            "fallback_enabled",
            "query_rewrite_enabled",
            "content_review_enabled",
        ):
            master.pop(legacy_key, None)

        model = str(
            master.get("model") or master.get("scene_selector_model") or "deepseek-v4-flash"
        )
        master.setdefault("route_model", master.get("scene_selector_model") or model)
        master.setdefault("summary_model", model)
        master.setdefault(
            "route_prompt",
            "你是 MAP Master 路由智能体。请根据用户问题、历史上下文和可用业务智能体，"
            "直接判断应调用哪些 sub-agent，输出候选 agent_code、confidence 与 reason。",
        )
        master.setdefault(
            "summary_prompt",
            "请整合各业务智能体结果，优先给出结论、证据来源和下一步建议。",
        )
        master.setdefault("current_version", "v1")
        master.setdefault("draft_version", f"{master['current_version']}-draft")
        if not isinstance(master.get("prompt_versions"), list) or not master["prompt_versions"]:
            now = datetime.now().isoformat()
            master["prompt_versions"] = [
                {
                    "version": master["current_version"],
                    "created_at": now,
                    "operator": "migration",
                    "note": "旧配置迁移生成",
                    "route_prompt": master["route_prompt"],
                    "summary_prompt": master["summary_prompt"],
                    "route_model": master["route_model"],
                    "summary_model": master["summary_model"],
                    "model": model,
                    "temperature": master.get("temperature", 0.2),
                    "max_tokens": master.get("max_tokens", 4096),
                }
            ]

    for agent in payload.get("business_agents") or []:
        if not isinstance(agent, dict):
            continue
        prompt_config = agent.get("prompt_config")
        if isinstance(prompt_config, dict):
            prompt_config.setdefault(
                "tool_call_prompt",
                prompt_config.get("system_prompt", ""),
            )
            if "tool_internal_prompts" not in prompt_config:
                prompt_config["tool_internal_prompts"] = [
                    {
                        "tool_name": item.get("tool_name", ""),
                        "prompt": item.get("system_prompt") or item.get("user_prompt") or "",
                        "enabled": True,
                    }
                    for item in prompt_config.get("tool_prompts") or []
                    if isinstance(item, dict)
                ]
        agent.setdefault("resource_mounts", [])

    payload.setdefault("mcp_servers", [])
    payload.setdefault("skills", [])
    payload.setdefault("flow_skill_descriptors", [])
    return payload


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
