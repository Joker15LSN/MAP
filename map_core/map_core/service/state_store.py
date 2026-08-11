from __future__ import annotations

import asyncio
import contextvars
import inspect
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from typing import Any, Awaitable, Callable, Coroutine, Literal, TypeVar, cast
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from pymongo.errors import OperationFailure

from .. import config as app_config
from ..database.mongodb import MongoClient
from ..observability import current_trace_context
from ..schema.state_event_schema import AgentEventSchema
from ..schema.state_store_schema import (
    AgentExecutionDocument,
    LLMCallRecordDocument,
    RequestRecordDocument,
    ToolCallRecordDocument,
)


class BaseAgentStateHandler(ABC):
    """Abstract base class for state handlers."""

    @abstractmethod
    async def handle_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        """Process the event asynchronously."""
        pass

    async def ensure_state(self, state_id: str, base_state: dict[str, Any]) -> None:
        """Optional hook for initializing state."""
        pass

    async def close(self) -> None:
        """Optional hook for releasing resources."""
        pass


class MongoAgentStateHandler(BaseAgentStateHandler):
    """
    Persists agent events to MongoDB, routing to three dedicated collections:

    - agent_executions:   one document per event (scene selector, agent dispatcher, per-agent
                          lifecycle, token usage, etc.). Every document is a flat record carrying
                          all session context fields (state_id, request_id, session_id, …) plus
                          a monotonic ``seq`` field that preserves event order within a state.
    - tool_call_records:  individual tool call invocations and their results.
    - request_records:    one document per HTTP request capturing query, timing, scene summary,
                          agents invoked, and aggregated token usage.

    Routing is driven by event_type:
      "tool_call" | "tool_result"     → tool_call_records
      "request.start" | "request.end" → request_records
      everything else                 → agent_executions
    """

    _TOOL_CALL_EVENT_TYPES: frozenset[str] = frozenset({"tool_call", "tool_result"})
    _LLM_CALL_EVENT_TYPES: frozenset[str] = frozenset({"llm_call"})
    _REQUEST_EVENT_TYPES: frozenset[str] = frozenset({"request.start", "request.end"})
    _REQUEST_AGENT_INDEX_KEYS: list[tuple[str, int]] = [
        ("request_id", 1),
        ("agent_code", 1),
    ]
    _REQUEST_AGENT_INDEX_NAME = "idx_request_id_agent_code"

    def __init__(
        self,
        agent_executions_collection: str = app_config.MONGODB_AGENT_EXECUTIONS_COLLECTION,
        tool_call_collection: str = app_config.MONGODB_TOOL_CALL_COLLECTION,
        request_collection: str = app_config.MONGODB_REQUEST_COLLECTION,
        llm_call_collection: str = app_config.MONGODB_LLM_CALL_COLLECTION,
    ) -> None:
        self._agent_executions_col_name = agent_executions_collection
        self._tool_call_col_name = tool_call_collection
        self._request_col_name = request_collection
        self._llm_call_col_name = llm_call_collection

        cfg = getattr(app_config, "MONGODB_CONFIG", None)
        if not cfg or "uri" not in cfg:
            logger.warning("MONGODB_CONFIG missing/invalid. MongoHandler disabled.")
            self._client = None
            return

        self._client = MongoClient(
            uri=cfg["uri"],
            database=cfg.get("database"),
            **{k: v for k, v in cfg.items() if k not in {"uri", "database"}},
        )

        # Caches {state_id: context_fields} so every event document can carry the full
        # session context (request_id, session_id, staff_code, …) even when base_state
        # is not re-supplied on every call.  Populated from the first base_state seen.
        self._state_context: dict[str, dict[str, Any]] = {}

        # Per-state monotonic counter for event ordering.  Protected by a single asyncio
        # lock; contention is negligible because increments are O(1) in-memory ops.
        self._seq: dict[str, int] = defaultdict(int)
        self._seq_lock = asyncio.Lock()
        self._indexes_ensured: set[str] = set()
        self._index_lock = asyncio.Lock()

    # Internal helpers
    async def _get_collection(self, collection_name: str):
        if not self._client:
            return None
        client = await self._client.connect()
        db_name = self._client.get_database_name() or getattr(
            app_config, "MONGODB_CONFIG", {}
        ).get("database")
        collection = client.get_database(db_name)[collection_name]
        await self._ensure_collection_indexes(collection_name, collection)
        return collection

    async def _ensure_collection_indexes(
        self,
        collection_name: str,
        collection: Any,
    ) -> None:
        if collection_name in self._indexes_ensured:
            return

        async with self._index_lock:
            if collection_name in self._indexes_ensured:
                return

            if collection_name in {
                self._agent_executions_col_name,
                self._tool_call_col_name,
                self._llm_call_col_name,
            }:
                if not await self._collection_has_index(
                    collection,
                    self._REQUEST_AGENT_INDEX_KEYS,
                ):
                    try:
                        await collection.create_index(
                            self._REQUEST_AGENT_INDEX_KEYS,
                            name=self._REQUEST_AGENT_INDEX_NAME,
                        )
                    except OperationFailure as exc:
                        if not self._is_different_name_index_conflict(exc):
                            raise
                        logger.debug(
                            "Mongo index already exists with different name for "
                            f"{collection_name}: {exc}"
                        )

            self._indexes_ensured.add(collection_name)

    @staticmethod
    async def _collection_has_index(
        collection: Any,
        expected_keys: list[tuple[str, int]],
    ) -> bool:
        async for index_info in collection.list_indexes():
            keys = index_info.get("key")
            if hasattr(keys, "items") and list(keys.items()) == expected_keys:
                return True
        return False

    @staticmethod
    def _is_different_name_index_conflict(exc: OperationFailure) -> bool:
        return (
            exc.code == 85
            and "Index already exists with a different name" in str(exc)
        )

    def _cache_state_context(self, state_id: str, base_state: dict[str, Any]) -> None:
        """Cache context fields from base_state for use in standalone event documents.

        ``_id`` is excluded because MongoDB manages that field itself.
        Only the first base_state seen for a given state_id is kept (it is authoritative).
        """
        if state_id not in self._state_context:
            self._state_context[state_id] = {
                k: v for k, v in base_state.items() if k != "_id"
            }

    def _get_state_context(self, state_id: str) -> dict[str, Any]:
        """Return cached context fields for state_id, or an empty dict if unknown."""
        return self._state_context.get(state_id, {})

    @staticmethod
    def _resolve_agent_identity(
        payload: dict[str, Any],
        ctx: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        payload_data = payload.get("data")
        nested_data = payload_data if isinstance(payload_data, dict) else {}
        context = ctx or {}

        agent_code = (
            payload.get("agent_code")
            or nested_data.get("agent_code")
            or context.get("agent_code")
        )
        agent_name = (
            payload.get("agent_name")
            or nested_data.get("agent_name")
            or context.get("agent_name")
        )
        return agent_code, agent_name

    async def _next_seq(self, state_id: str) -> int:
        """Return and increment the per-state event sequence counter (thread-safe)."""
        async with self._seq_lock:
            seq = self._seq[state_id]
            self._seq[state_id] += 1
            return seq

    async def ensure_state(self, state_id: str, base_state: dict[str, Any]) -> None:
        # Each event is now a self-contained document, so there is no parent document
        # to pre-create.  We only populate the context cache here.
        self._cache_state_context(state_id, base_state)

    async def handle_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        # Keep the context cache up-to-date whenever base_state is supplied.
        if base_state:
            self._cache_state_context(state_id, base_state)

        if event_type in self._TOOL_CALL_EVENT_TYPES:
            await self._handle_tool_call_event(state_id, event_type, payload)
        elif event_type in self._LLM_CALL_EVENT_TYPES:
            await self._handle_llm_call_event(state_id, payload)
        elif event_type in self._REQUEST_EVENT_TYPES:
            await self._handle_request_event(state_id, event_type, payload)
        else:
            await self._handle_agentic_event(state_id, event_type, payload, base_state)

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def _handle_agentic_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None,
    ) -> None:
        """Insert a standalone document for each agentic event into agent_executions.

        Every document is a flat record that combines:
          - session context fields (state_id, request_id, session_id, staff_code, …)
          - a monotonic ``seq`` counter to preserve event order within a state
          - the normalized event fields (event_type, component, stage, status, payload, ts)
        """
        if base_state:
            self._cache_state_context(state_id, base_state)

        ctx = self._get_state_context(state_id)
        seq = await self._next_seq(state_id)
        agent_code, agent_name = self._resolve_agent_identity(payload, ctx)
        trace_ctx = payload.get("_trace") or {}
        payload_data = payload.get("data")
        resolved_payload = payload_data if isinstance(payload_data, dict) else payload
        if "_trace" in resolved_payload:
            # ``_trace`` is an internal envelope field; never persist it inside
            # the document's payload field.
            resolved_payload = {
                k: v for k, v in resolved_payload.items() if k != "_trace"
            }
        payload_ts = payload.get("timestamp")
        resolved_ts = (
            payload_ts
            if isinstance(payload_ts, datetime)
            else datetime.now(ZoneInfo("Asia/Shanghai"))
        )
        component = payload.get("component")
        if not isinstance(component, str) or not component.strip():
            component = agent_code or agent_name
        stage = payload.get("stage")
        status = payload.get("status")
        raw_meta = ctx.get("meta")
        meta: dict[str, Any] = (
            cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
        )

        document = AgentExecutionDocument(
            state_id=state_id,
            request_id=ctx.get("request_id"),
            session_id=ctx.get("session_id"),
            staff_code=ctx.get("staff_code"),
            meta=meta,
            agent_code=agent_code,
            agent_name=agent_name,
            seq=seq,
            event_type=event_type,
            component=component,
            stage=stage if isinstance(stage, str) else None,
            status=status if isinstance(status, str) else None,
            payload=resolved_payload,
            ts=resolved_ts,
            trace_id=trace_ctx.get("trace_id"),
            span_id=trace_ctx.get("span_id"),
        )

        try:
            collection = await self._get_collection(self._agent_executions_col_name)
            if collection is None:
                return
            await collection.insert_one(asdict(document))
        except Exception as exc:
            logger.error(
                f"[MongoHandler] Agentic event write failed for {state_id}: {exc}"
            )

    async def _handle_tool_call_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Insert a standalone document for each tool_call / tool_result event.

        Each event becomes its own document in tool_call_records. The shared fields
        (state_id, agent_id, step, tool, tool_id) can be used to correlate the two
        sides of a call in queries without requiring any upsert pairing logic.
        """
        ctx = self._get_state_context(state_id)
        ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        agent_code, agent_name = self._resolve_agent_identity(payload, ctx)
        trace_ctx = payload.get("_trace") or {}
        document = ToolCallRecordDocument(
            event_type=event_type,
            state_id=state_id,
            request_id=ctx.get("request_id"),
            session_id=ctx.get("session_id"),
            ts=ts,
            agent_code=agent_code,
            agent_name=agent_name,
            agent_id=payload.get("agent_id"),
            tool=payload.get("tool"),
            tool_id=payload.get("tool_id"),
            step=payload.get("step"),
            trace_id=trace_ctx.get("trace_id"),
            span_id=trace_ctx.get("span_id"),
        )

        if event_type == "tool_call":
            document.args = payload.get("args")
        else:  # tool_result
            output = payload.get("output")
            document.output = output
            document.status = (
                "error"
                if isinstance(output, dict) and output.get("success") is False
                else "success"
            )
            if payload.get("duration_s") is not None:
                document.duration_s = payload.get("duration_s")
            if payload.get("error") is not None:
                document.error = payload.get("error")
            elif isinstance(output, dict) and output.get("error") is not None:
                document.error = output.get("error")

        try:
            collection = await self._get_collection(self._tool_call_col_name)
            if collection is None:
                return
            await collection.insert_one(asdict(document))
        except Exception as exc:
            logger.error(
                f"[MongoHandler] Tool call event write failed for {state_id}: {exc}"
            )

    async def _handle_llm_call_event(
        self,
        state_id: str,
        payload: dict[str, Any],
    ) -> None:
        ctx = self._get_state_context(state_id)
        seq = await self._next_seq(state_id)
        agent_code, agent_name = self._resolve_agent_identity(payload, ctx)
        trace_ctx = payload.get("_trace") or {}
        raw_meta = ctx.get("meta")
        meta: dict[str, Any] = (
            cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
        )

        document = LLMCallRecordDocument(
            state_id=state_id,
            request_id=payload.get("request_id") or ctx.get("request_id"),
            session_id=payload.get("session_id") or ctx.get("session_id"),
            staff_code=payload.get("staff_code") or ctx.get("staff_code"),
            meta=meta,
            seq=seq,
            agent_code=agent_code,
            agent_name=agent_name,
            component=payload.get("component"),
            phase=payload.get("phase"),
            step=payload.get("step"),
            call_kind=payload.get("call_kind"),
            model=payload.get("model"),
            provider_request_id=payload.get("provider_request_id"),
            start_ts=payload.get("start_ts"),
            end_ts=payload.get("end_ts"),
            duration_s=payload.get("duration_s"),
            status=payload.get("status"),
            usage=payload.get("usage"),
            error=payload.get("error"),
            finish_reason=payload.get("finish_reason"),
            prompt_summary=payload.get("prompt_summary"),
            tool_names=payload.get("tool_names"),
            trace_id=payload.get("trace_id") or trace_ctx.get("trace_id"),
            span_id=payload.get("span_id") or trace_ctx.get("span_id"),
        )
        try:
            collection = await self._get_collection(self._llm_call_col_name)
            if collection is None:
                return
            await collection.insert_one(asdict(document))
        except Exception as exc:
            logger.error(
                f"[MongoHandler] LLM call event write failed for {state_id}: {exc}"
            )

    async def _handle_request_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Upsert the request_records document for this request_id.

        request.start creates the document with initial metadata.
        request.end updates it with final timing and aggregated results.
        """
        request_id: str | None = payload.get("request_id")
        if not request_id:
            logger.warning(
                f"[MongoHandler] {event_type} event missing request_id, skipping"
            )
            return

        ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        trace_ctx = payload.get("_trace") or {}
        try:
            collection = await self._get_collection(self._request_col_name)
            if collection is None:
                return

            if event_type == "request.start":
                document = RequestRecordDocument(
                    state_id=state_id,
                    request_id=request_id,
                    session_id=payload.get("session_id"),
                    workspace_id=payload.get("workspace_id"),
                    staff_code=payload.get("staff_code"),
                    query=payload.get("query"),
                    start_ts=payload.get("start_ts", ts),
                    status="running",
                    trace_id=trace_ctx.get("trace_id"),
                    span_id=trace_ctx.get("span_id"),
                )
                update_op: dict[str, Any] = {
                    "$setOnInsert": asdict(document),
                }
            else:  # request.end
                document = RequestRecordDocument(
                    state_id=state_id,
                    request_id=request_id,
                    end_ts=ts,
                    status=payload.get("status", "success"),
                    duration_s=payload.get("duration_s"),
                    scene_result=payload.get("scene_result"),
                    agents_called=payload.get("agents_called"),
                    token_usage_total=payload.get("token_usage_total"),
                    error=payload.get("error"),
                )
                update_op = {
                    "$setOnInsert": {
                        "state_id": document.state_id,
                        "request_id": document.request_id,
                        "session_id": document.session_id,
                        "workspace_id": document.workspace_id,
                    },
                    "$set": {
                        "end_ts": document.end_ts,
                        "status": document.status,
                        "duration_s": document.duration_s,
                        "scene_result": document.scene_result,
                        "agents_called": document.agents_called,
                        "token_usage_total": document.token_usage_total,
                        "error": document.error,
                        **(
                            {
                                "trace_id": trace_ctx.get("trace_id"),
                                "span_id": trace_ctx.get("span_id"),
                            }
                            if trace_ctx
                            else {}
                        ),
                    },
                }

            await collection.update_one(
                {"request_id": request_id}, update_op, upsert=True
            )
        except Exception as exc:
            logger.error(
                f"[MongoHandler] Request event write failed for {request_id}: {exc}"
            )


class WebHookAgentStateHandler(BaseAgentStateHandler):
    """Handler that pushes events to an external API."""

    def __init__(self, webhook_url: str, auth_token: str | None = None):
        self.webhook_url = webhook_url
        self.headers = {"Content-Type": "application/json"}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"

        self.client = httpx.AsyncClient(timeout=3.0)

    async def handle_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        # Strip the internal ``_trace`` envelope field: the external webhook
        # contract must not change shape when OTel is enabled.
        event_payload = {
            key: value for key, value in payload.items() if key != "_trace"
        }
        data = {
            "state_id": state_id,
            "event_type": event_type,
            "event": event_payload,
            "timestamp": str(datetime.now(ZoneInfo("Asia/Shanghai"))),
        }

        try:
            resp = await self.client.post(
                self.webhook_url, json=data, headers=self.headers
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(f"[WebHookHandler] Push failed: {exc}")
        except Exception as exc:
            logger.error(f"[WebHookHandler] Unexpected error: {exc}")

    async def close(self):
        await self.client.aclose()


class EventDispatcher:
    """Bounded async event queue with a fixed worker pool, providing back-pressure for fire_and_forget.

    Each submitted coroutine is paired with a ``contextvars.Context``
    snapshot taken at submit time (inside the request task). Workers execute
    the coroutine inside that snapshot so that OTel trace context captured by
    ``record_event`` survives the queue hop — coroutine objects alone do not
    carry ``contextvars``.
    """

    def __init__(self, maxsize: int = 500, n_workers: int = 3) -> None:
        self._queue: asyncio.Queue[
            tuple[Coroutine[Any, Any, Any], contextvars.Context] | None
        ] = asyncio.Queue(maxsize=maxsize)
        self._n_workers = n_workers
        self._worker_tasks: list[asyncio.Task] = []

    def start(self) -> None:
        """Start consumer workers. Must be called after the asyncio event loop is running (inside lifespan)."""
        for _ in range(self._n_workers):
            task = asyncio.create_task(self._consume())
            self._worker_tasks.append(task)

    async def stop(self) -> None:
        """Shutdown: send one sentinel per worker and wait for all to finish."""
        for _ in self._worker_tasks:
            await self._queue.put(None)
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:  # sentinel: exit signal
                self._queue.task_done()
                break
            coro, ctx = item
            try:
                await asyncio.create_task(coro, context=ctx)
            except Exception as exc:
                logger.error(f"[EventDispatcher] handler error: {exc}")
            finally:
                self._queue.task_done()

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        context: contextvars.Context | None = None,
    ) -> None:
        """Non-blocking submit. Drops the coroutine and logs a warning when the queue is full.

        The request context is snapshotted here (synchronously, inside the
        caller task) unless an explicit context is provided.
        """
        ctx = context or contextvars.copy_context()
        try:
            self._queue.put_nowait((coro, ctx))
        except asyncio.QueueFull:
            logger.warning(
                f"[EventDispatcher] queue full (maxsize={self._queue.maxsize}), dropping event"
            )
            coro.close()

    @property
    def started(self) -> bool:
        return bool(self._worker_tasks)


class GlobalAgentStateStore:
    """Singleton Dispatcher for agent events."""

    _instance: "GlobalAgentStateStore | None" = None

    def __init__(self) -> None:
        self.handlers: list[BaseAgentStateHandler] = []
        self._closed = False
        self._dispatcher = EventDispatcher()

        self.handlers.append(MongoAgentStateHandler())

        webhook_url = getattr(app_config, "AGENT_EVENT_WEBHOOK_URL", None)
        # webhook_url = "http://localhost:8000/api/event_callback"
        if webhook_url:
            self.handlers.append(WebHookAgentStateHandler(webhook_url=webhook_url))

    @classmethod
    def instance(cls) -> "GlobalAgentStateStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def maybe_instance(cls) -> "GlobalAgentStateStore | None":
        return cls._instance

    def start(self) -> None:
        """Start the event dispatcher. Must be called inside lifespan after the event loop is running."""
        self._dispatcher.start()

    async def ensure_state(self, state_id: str, base_state: dict[str, Any]) -> None:
        """Notify all handlers to initialize state."""
        tasks = [h.ensure_state(state_id, base_state) for h in self.handlers]
        await asyncio.gather(*tasks, return_exceptions=True)

    def _snapshot_trace_payload(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture the current OTel trace context synchronously.

        Must run in the caller's task (inside the request context) so that
        async dispatcher workers can persist trace correlation without a live
        span context — coroutine objects do not carry ``contextvars``.

        ``_trace`` is an internal envelope field: handlers must strip it
        before exposing the payload externally (webhook push, Mongo
        ``payload`` field fallback) so the external event contract stays
        unchanged.
        """
        if "_trace" in payload:
            return payload
        trace_ctx = current_trace_context()
        if not trace_ctx:
            return payload
        return {**payload, "_trace": trace_ctx}

    def record_event_nowait(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        """Snapshot trace context now, then dispatch via fire-and-forget."""
        snapshot = self._snapshot_trace_payload(payload)
        fire_and_forget(self.record_event(state_id, event_type, snapshot, base_state))

    async def record_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        """
        Dispatches the event to ALL handlers concurrently.
        Wait for all to finish (or fail) without blocking each other.

        When awaited directly (dispatcher not running), the OTel trace context
        is captured here as a fallback. When dispatched through
        ``record_event_nowait``, ``payload["_trace"]`` is already snapshotted
        in the caller's task and preserved as-is.
        """
        payload = self._snapshot_trace_payload(payload)
        tasks = [
            h.handle_event(state_id, event_type, payload, base_state)
            for h in self.handlers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                logger.error(f"One of the state handlers failed: {res}")

    async def close(self) -> None:
        if self._closed:
            return

        await self._dispatcher.stop()

        tasks = [h.close() for h in self.handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                logger.error(f"One of the state handler shutdowns failed: {res}")

        self._closed = True
        type(self)._instance = None


_R = TypeVar("_R")

DEFAULT_STATE_STORE_TIMEOUT_S = 2.0


async def _safe_state_call(
    coro: Awaitable[Any],
    *,
    action: str,
    timeout_s: float = DEFAULT_STATE_STORE_TIMEOUT_S,
) -> None:
    """deprecated!"""
    try:
        await asyncio.wait_for(coro, timeout=timeout_s)
    except Exception as exc:
        logger.error(f"State store {action} failed: {exc}")


def safe_serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, datetime)):
        return value
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return safe_serialize(value.model_dump())
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple)):
        return [safe_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(k): safe_serialize(v) for k, v in value.items()}
    return str(value)


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Submit an event coroutine to the bounded dispatcher queue.

    The caller's ``contextvars.Context`` (including the OTel span) is
    snapshotted synchronously here and restored around coroutine execution,
    so trace correlation survives the dispatcher hop.

    Falls back to asyncio.create_task when the dispatcher is not yet running
    (e.g. in tests or before application startup).
    """
    ctx = contextvars.copy_context()
    store = GlobalAgentStateStore.maybe_instance()
    if store is not None and store._dispatcher.started:
        store._dispatcher.submit(coro, context=ctx)
    else:
        task = asyncio.create_task(coro, context=ctx)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def record_agent_call(
    component: str,
    category: Literal[
        "lifecycle", "system", "workflow", "agent", "tool", "error"
    ] = "lifecycle",
    meta_extractor: Callable[[Any], dict[str, Any]] | None = None,
):
    """Decorator for async methods to record input/output into agent state store."""

    def decorator(func: Callable[..., Awaitable[_R]]):
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _R:
            self_obj = args[0] if args else None

            state_store = getattr(self_obj, "state_store", None)
            state_id = getattr(self_obj, "state_id", None)
            base_state = getattr(self_obj, "base_state", None)

            if not state_store or not state_id:
                return await func(*args, **kwargs)

            try:
                func_sig = inspect.signature(func)
                bound_args = func_sig.bind(*args, **kwargs)
                bound_args.apply_defaults()

                ignored_keys = {"self", "cls", "ctx", "state_store", "state_id"}
                clean_input = {
                    k: v
                    for k, v in bound_args.arguments.items()
                    if k not in ignored_keys
                }
                serialized_input = safe_serialize(clean_input)
            except Exception as e:
                logger.warning(f"Failed to bind arguments for {component}: {e}")
                serialized_input = {"error": "arg_binding_failed"}

            start_ts = datetime.now(ZoneInfo("Asia/Shanghai"))

            start_event = AgentEventSchema(
                timestamp=start_ts,
                category=category,
                component=component,
                stage="start",
                data={"input": serialized_input},
            )

            fire_and_forget(
                state_store.record_event(
                    state_id=state_id,
                    event_type=component,
                    payload=start_event.model_dump(),
                    base_state=base_state,
                )
            )

            result = None
            try:
                result = await func(*args, **kwargs)

                end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
                duration = (end_ts - start_ts).total_seconds()

                if inspect.isasyncgen(result) or inspect.isgenerator(result):
                    serialized_output = {
                        "type": "stream",
                        "status": "initialized",
                        "description": "Generator returned, streaming content...",
                    }
                else:
                    serialized_output = safe_serialize(result)

                meta: dict[str, Any] = {"duration_s": duration}
                if meta_extractor is not None:
                    try:
                        extra = meta_extractor(result)
                        if extra:
                            meta.update(extra)
                    except Exception:
                        pass
                end_event = AgentEventSchema(
                    timestamp=end_ts,
                    category=category,
                    component=component,
                    stage="end",
                    status="success",
                    data={
                        "output": serialized_output,
                        "meta": meta,
                    },
                )

                fire_and_forget(
                    state_store.record_event(
                        state_id=state_id,
                        event_type=component,
                        payload=end_event.model_dump(),
                    )
                )
                return result

            except Exception as exc:
                end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
                duration = (end_ts - start_ts).total_seconds()

                error_event = AgentEventSchema(
                    timestamp=end_ts,
                    category="error",
                    component=component,
                    stage="end",
                    status="failed",
                    data={
                        "input": serialized_input,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "meta": {"duration_s": duration},
                    },
                )

                fire_and_forget(
                    state_store.record_event(
                        state_id=state_id,
                        event_type=component,
                        payload=error_event.model_dump(),
                    )
                )
                raise exc

        return wrapper

    return decorator
