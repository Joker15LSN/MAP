from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable, TypeVar

from pydantic import ValidationError

from .schemas import AdminState

T = TypeVar("T")


class AdminStateStore:
    def __init__(self, state_file: str) -> None:
        self._path = Path(state_file)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write_state(AdminState.default())

    def load(self) -> AdminState:
        with self._lock:
            return self._read_state()

    def update(self, updater: Callable[[AdminState], T]) -> tuple[AdminState, T]:
        with self._lock:
            state = self._read_state()
            result = updater(state)
            state.updated_at = datetime.now().isoformat()
            self._write_state(state)
            return state, result

    def _read_state(self) -> AdminState:
        raw = self._path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        try:
            return AdminState.model_validate(payload)
        except ValidationError:
            fallback = AdminState.default()
            self._write_state(fallback)
            return fallback

    def _write_state(self, state: AdminState) -> None:
        self._path.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
