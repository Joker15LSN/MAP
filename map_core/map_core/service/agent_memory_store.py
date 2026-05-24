from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from .. import config as app_config
from ..database.mongodb import MongoClient


class AgentMemoryStore:
    """Mongo-backed per-agent session memory store."""

    _INDEX_NAME = "uniq_session_intention_agent"

    def __init__(
        self,
        *,
        collection_name: str = app_config.MONGODB_AGENT_MEMORY_COLLECTION,
        logger_: Any | None = None,
    ) -> None:
        self._collection_name = collection_name
        self._logger = logger_ or logger
        self._ensured = False

        cfg = getattr(app_config, "MONGODB_CONFIG", None)
        if not cfg or "uri" not in cfg:
            self._logger.warning(
                "MONGODB_CONFIG missing/invalid. AgentMemoryStore disabled."
            )
            self._client = None
            return

        self._client = MongoClient(
            uri=cfg["uri"],
            database=cfg.get("database"),
            **{k: v for k, v in cfg.items() if k not in {"uri", "database"}},
        )

    async def _get_collection(self):
        if self._client is None:
            return None
        client = await self._client.connect()
        db_name = self._client.get_database_name() or getattr(
            app_config, "MONGODB_CONFIG", {}
        ).get("database")
        if not db_name:
            self._logger.warning(
                "MONGODB_CONFIG missing database. AgentMemoryStore disabled."
            )
            return None
        return client.get_database(db_name)[self._collection_name]

    async def ensure_collection(self) -> None:
        """Create the collection implicitly and ensure lookup/upsert indexes exist."""
        if self._ensured:
            return
        collection = await self._get_collection()
        if collection is None:
            return
        await collection.create_index(
            [
                ("session_id", 1),
                ("intention_id", 1),
                ("agent_code", 1),
            ],
            unique=True,
            name=self._INDEX_NAME,
        )
        self._ensured = True

    @staticmethod
    def _normalize_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def _normalize_history(
        cls,
        value: Any,
        *,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        normalized: list[dict[str, Any]] = []
        allowed_roles = {"system", "user", "assistant", "tool"}
        for item in value:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in allowed_roles:
                continue

            message = dict(item)
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                message["content"] = str(content)
            normalized.append(message)

        if max_messages > 0:
            return normalized[-max_messages:]
        return normalized

    async def get_history(
        self,
        *,
        session_id: str | None,
        intention_id: str | None,
        agent_code: str,
        max_messages: int = app_config.AGENT_MEMORY_MAX_MESSAGES,
    ) -> list[dict[str, Any]]:
        normalized_session_id = self._normalize_text(session_id)
        normalized_intention_id = self._normalize_text(intention_id)
        normalized_agent_code = self._normalize_text(agent_code)
        if (
            not normalized_session_id
            or not normalized_intention_id
            or not normalized_agent_code
        ):
            return []

        await self.ensure_collection()
        collection = await self._get_collection()
        if collection is None:
            return []

        document = await collection.find_one(
            {
                "session_id": normalized_session_id,
                "intention_id": normalized_intention_id,
                "agent_code": normalized_agent_code,
            },
            {"history": 1, "_id": 0},
        )
        if not isinstance(document, dict):
            return []
        return self._normalize_history(
            document.get("history"),
            max_messages=max_messages,
        )

    async def upsert_history(
        self,
        *,
        session_id: str,
        intention_id: str,
        agent_code: str,
        history: list[dict[str, Any]],
    ) -> None:
        """Persist one agent's memory document. Currently reserved for writers."""
        normalized_session_id = self._normalize_text(session_id)
        normalized_intention_id = self._normalize_text(intention_id)
        normalized_agent_code = self._normalize_text(agent_code)
        if (
            not normalized_session_id
            or not normalized_intention_id
            or not normalized_agent_code
        ):
            return

        normalized_history = self._normalize_history(
            history,
            max_messages=app_config.AGENT_MEMORY_MAX_MESSAGES,
        )
        await self.ensure_collection()
        collection = await self._get_collection()
        if collection is None:
            return

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        await collection.update_one(
            {
                "session_id": normalized_session_id,
                "intention_id": normalized_intention_id,
                "agent_code": normalized_agent_code,
            },
            {
                "$setOnInsert": {
                    "created_at": now,
                },
                "$set": {
                    "history": normalized_history,
                    "updated_at": now,
                },
            },
            upsert=True,
        )
        self._logger.info(
            "Agent '{}' recorded {} memory messages for session_id={!r}, intention_id={!r}",
            normalized_agent_code,
            len(normalized_history),
            normalized_session_id,
            normalized_intention_id,
        )
