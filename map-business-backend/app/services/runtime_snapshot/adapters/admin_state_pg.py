"""PostgreSQL single-row AdminState repository (Step 7 PR-J7a).

The repository never commits: it joins the caller's transaction.
``load`` locks the singleton row (``SELECT ... FOR UPDATE``) so a
read-modify-write cycle is serialized on the row; the same transaction can
then write snapshots/audit/outbox and commit atomically. A missing row or
a ``state_hash`` mismatch fails closed — defaults are NEVER written by
``load``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import AdminStateRow
from ....schemas import AdminState
from ..digest import state_hash
from ..errors import AdminStateUnavailableError, BadAdminStateError


class PgAdminStateRepository:
    """Durable AdminState row access (single row, id = 1)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self) -> AdminState:
        """Lock and validate the singleton admin state row.

        Fails closed with :class:`AdminStateUnavailableError` when the row
        is missing and with :class:`BadAdminStateError` when the document
        is invalid or its canonical hash does not match ``state_hash``.
        """
        row = (
            (
                await self._session.execute(
                    select(AdminStateRow)
                    .where(AdminStateRow.id == 1)
                    .with_for_update()
                )
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise AdminStateUnavailableError(
                "admin state row is missing: refusing to build a default"
            )
        try:
            state = AdminState.model_validate(row.state_json)
        except ValidationError as exc:
            raise BadAdminStateError(
                f"admin state row failed validation (kept untouched): {exc}"
            ) from exc
        if state_hash(state) != row.state_hash:
            raise BadAdminStateError(
                "admin state row hash mismatch (kept untouched)"
            )
        return state

    async def save(self, state: AdminState) -> None:
        """Atomically update ``state_json`` + ``state_hash`` (flush only).

        The caller owns the transaction; this method never commits.
        """
        await self._session.execute(
            update(AdminStateRow)
            .where(AdminStateRow.id == 1)
            .values(
                state_json=state.model_dump(),
                state_hash=state_hash(state),
                updated_at=datetime.now(UTC),
            )
        )

    async def seed_if_empty(self, state: AdminState) -> bool:
        """Insert the singleton row if it does not exist.

        Returns ``True`` when this call inserted the row, ``False`` when a
        row already existed (and was left untouched).
        """
        result = await self._session.execute(
            pg_insert(AdminStateRow.__table__)
            .values(
                id=1,
                state_json=state.model_dump(),
                state_hash=state_hash(state),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        return result.rowcount > 0
