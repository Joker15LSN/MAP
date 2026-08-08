"""Re-review P3-5.8 acceptance tests: ``_trace`` must not leak externally.

The dispatcher attaches ``payload["_trace"]`` as an internal envelope field
so async handlers can persist trace correlation. Regression for the finding
that the same shared payload was then pushed to external webhooks and (for
agentic events without a nested ``data`` key) duplicated into the Mongo
``payload`` field — changing the external event contract when OTel is on.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from map_core.service.state_store import (
    MongoAgentStateHandler,
    WebHookAgentStateHandler,
)

TRACE_CTX = {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "span_id": "00f067aa0ba902b7",
}


class _FakePostClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any], headers: Any = None):
        self.sent.append(json)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

        return _Resp()

    async def aclose(self) -> None:
        pass


class _FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    async def insert_one(self, document: dict[str, Any]) -> None:
        self.documents.append(document)


def test_webhook_event_excludes_internal_trace_field() -> None:
    handler = WebHookAgentStateHandler(webhook_url="http://hook.test/callback")
    handler.client = _FakePostClient()  # type: ignore[assignment]

    payload = {"content": "hi", "agent_code": "demo", "_trace": TRACE_CTX}
    asyncio.run(
        handler.handle_event(state_id="s1", event_type="agent_message", payload=payload)
    )

    assert len(handler.client.sent) == 1
    event = handler.client.sent[0]["event"]
    assert "_trace" not in event, (
        "_trace is an internal envelope field and must not reach webhooks"
    )
    # business fields stay intact
    assert event["content"] == "hi"
    assert event["agent_code"] == "demo"


def test_mongo_agentic_payload_excludes_internal_trace_field(monkeypatch) -> None:
    handler = MongoAgentStateHandler.__new__(MongoAgentStateHandler)
    handler._agent_executions_col_name = "agent_executions"
    handler._tool_call_col_name = "tool_call_records"
    handler._request_col_name = "request_records"
    handler._llm_call_col_name = "llm_call_records"
    handler._state_context = {}
    handler._seq = defaultdict(int)
    handler._seq_lock = asyncio.Lock()
    handler._indexes_ensured = set()
    handler._index_lock = asyncio.Lock()

    collection = _FakeCollection()

    async def fake_get_collection(name: str):
        return collection

    monkeypatch.setattr(handler, "_get_collection", fake_get_collection)

    # No nested ``data`` key -> handler falls back to persisting the whole
    # payload; ``_trace`` must still be stripped from the payload field.
    payload = {"content": "hi", "_trace": TRACE_CTX}
    asyncio.run(
        handler._handle_agentic_event(
            state_id="s1",
            event_type="agent_message",
            payload=payload,
            base_state=None,
        )
    )

    assert len(collection.documents) == 1
    document = collection.documents[0]
    assert "_trace" not in document["payload"]
    assert document["payload"]["content"] == "hi"
    # trace correlation is preserved on the dedicated top-level fields
    assert document["trace_id"] == TRACE_CTX["trace_id"]
    assert document["span_id"] == TRACE_CTX["span_id"]
