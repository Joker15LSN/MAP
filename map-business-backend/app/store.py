"""File-backed AdminState store (F-01 / FIX-P1-AUDIT-01).

- :meth:`load` fails closed: a corrupt state file raises
  :class:`BadStateFileError` and is NEVER overwritten by defaults;
- :meth:`update_with_hash` performs an optimistic concurrency check
  (expected hash) and writes atomically: temp file -> fsync -> rename,
  so no half-written file can ever be observed;
- legacy :meth:`update` is kept for read-only compatibility callers.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TypeVar

from pydantic import ValidationError

from .schemas import AdminState

T = TypeVar("T")


def state_hash(state: AdminState) -> str:
    """Canonical hash of the state (sorted keys, stable across runs)."""
    canonical = json.dumps(
        state.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BadStateFileError(Exception):
    """The state file exists but cannot be parsed; it must not be overwritten."""


class ConcurrentModificationError(Exception):
    """The state changed since the caller read it (expected hash mismatch)."""


class StoreWriteError(Exception):
    """The atomic write failed; the previous file stays intact."""


class AdminStateStore:
    def __init__(self, state_file: str) -> None:
        self._path = Path(state_file)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write_atomic(AdminState.default())

    def load(self) -> AdminState:
        with self._lock:
            return self._read_state()

    def update(self, updater: Callable[[AdminState], T]) -> tuple[AdminState, T]:
        """Legacy unconditional update (kept for compatibility callers)."""
        with self._lock:
            state = self._read_state()
            result = updater(state)
            state.updated_at = datetime.now().isoformat()
            self._write_atomic(state)
            return state, result

    def update_with_hash(
        self, expected_hash: str, updater: Callable[[AdminState], T]
    ) -> tuple[AdminState, T]:
        """Optimistic update: fails with ConcurrentModificationError when the
        state changed since ``expected_hash`` was read."""
        with self._lock:
            state = self._read_state()
            if state_hash(state) != expected_hash:
                raise ConcurrentModificationError(
                    "admin state changed since the request was read"
                )
            result = updater(state)
            state.updated_at = datetime.now().isoformat()
            self._write_atomic(state)
            return state, result

    def _read_state(self) -> AdminState:
        raw = self._path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BadStateFileError(f"state file is not valid JSON: {exc}") from exc
        payload = self._migrate_payload(payload)
        try:
            return AdminState.model_validate(payload)
        except ValidationError as exc:
            # Never fall back to defaults and overwrite the corrupt file.
            raise BadStateFileError(
                f"state file failed validation (kept untouched): {exc}"
            ) from exc

    @staticmethod
    def _migrate_payload(payload: dict) -> dict:
        """Normalize persisted admin JSON across schema revisions."""
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

    def _write_atomic(self, state: AdminState) -> None:
        """Temp file + fsync + atomic rename; failures never corrupt the old file."""
        directory = self._path.parent
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(state.model_dump(), ensure_ascii=False, indent=2)
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except Exception as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise StoreWriteError(f"atomic state write failed: {exc}") from exc
