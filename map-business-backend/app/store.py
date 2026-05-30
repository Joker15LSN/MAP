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
        payload = self._migrate_payload(payload)
        try:
            state = AdminState.model_validate(payload)
            self._write_state(state)
            return state
        except ValidationError:
            fallback = AdminState.default()
            self._write_state(fallback)
            return fallback

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

            model = str(master.get("model") or master.get("scene_selector_model") or "deepseek-v4-flash")
            master.setdefault("route_model", master.get("scene_selector_model") or model)
            master.setdefault("summary_model", model)
            master.setdefault(
                "route_prompt",
                "你是 MAP Master 路由智能体。请根据用户问题、历史上下文和可用业务智能体，直接判断应调用哪些 sub-agent，输出候选 agent_code、confidence 与 reason。",
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

    def _write_state(self, state: AdminState) -> None:
        self._path.write_text(
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
