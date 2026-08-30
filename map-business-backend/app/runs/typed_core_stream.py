"""Step 8 PR-K6: typed NDJSON core run stream adapter.

The worker's default :class:`CoreRunStream` now talks to the core
service-identity endpoint (``POST /internal/v1/runs/{run_id}/attempts/
{attempt}/events``) and parses ``application/x-ndjson`` lines into the same
typed :class:`CoreItem` contract as the legacy SSE adapter.  The legacy
:class:`~app.runs.core_transport.HttpCoreRunStream` stays available as an
adapter but is no longer the worker default.

The core-side ``CoreExecutionEvent`` schema is intentionally NOT imported
here: the BFF owns a local parser for the same JSON shape, so a core schema
bump can never leak through a Python import.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .domain import AttemptInput, CoreError, CoreEvent, CoreItem, CoreOutcome

_EVENTS_PATH = "/internal/v1/runs/{run_id}/attempts/{attempt}/events"
_STREAM_CORE_ERROR = "STREAM_CORE_ERROR"

# Frozen core event type -> BFF CoreEvent type.  ``stream.terminal`` is
# handled separately because it projects to CoreOutcome instead of CoreEvent.
_CORE_TYPE_MAP: dict[str, str] = {
    "step.started": "step.started",
    "step.completed": "step.completed",
    "step.failed": "step.failed",
    "message.delta": "message.delta",
    "tool.invocation_created": "tool.invocation_created",
    "tool.invocation_completed": "tool.invocation_completed",
    "tool.invocation_failed": "tool.invocation_failed",
    "model.invocation_created": "model.invocation_created",
    "model.invocation_sent": "model.invocation_sent",
    "model.invocation_succeeded": "model.invocation_succeeded",
    "model.invocation_failed": "model.invocation_failed",
    "model.invocation_unknown": "model.invocation_unknown",
    "checkpoint.written": "checkpoint.written",
    "effect.planned": "effect.planned",
    "effect.executing": "effect.executing",
    "effect.succeeded": "effect.succeeded",
    "effect.failed": "effect.failed",
    "effect.uncertain": "effect.uncertain",
    "effect.reconciling": "effect.reconciling",
    "effect.reconciled": "effect.reconciled",
    "effect.cancelled": "effect.cancelled",
}


@dataclass(frozen=True)
class _CoreExecutionEvent:
    """Local projection of the core NDJSON event shape (schema v1)."""

    type: str
    data: dict[str, Any]
    seq: int | None = None

    @classmethod
    def from_line(cls, line: str) -> _CoreExecutionEvent:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed core event JSON: {exc.msg}") from None
        if not isinstance(payload, dict):
            raise ValueError("core event JSON must be an object")
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            raise ValueError("core event type must be a string")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("core event data must be an object")
        seq = payload.get("seq")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int)):
            raise ValueError("core event seq must be an integer")
        return cls(type=event_type, data=data, seq=seq)


def _project_line(line: str) -> CoreItem:
    try:
        event = _CoreExecutionEvent.from_line(line)
    except ValueError as exc:
        return CoreError(code=_STREAM_CORE_ERROR, message=str(exc))

    if event.type == "stream.terminal":
        status = event.data.get("status")
        if status == "completed":
            return CoreOutcome(status="completed")
        if status == "failed":
            return CoreOutcome(
                status="failed",
                error_code=str(event.data.get("error_code") or _STREAM_CORE_ERROR),
                error_message=str(
                    event.data.get("error_message") or "core stream failed"
                ),
            )
        return CoreError(
            code=_STREAM_CORE_ERROR,
            message=f"unknown stream.terminal status {status!r}",
        )

    mapped_type = _CORE_TYPE_MAP.get(event.type)
    if mapped_type is None:
        return CoreError(
            code=_STREAM_CORE_ERROR,
            message=f"unknown core event type {event.type!r}",
        )
    return CoreEvent(type=mapped_type, data=dict(event.data))


class TypedCoreRunStream:
    """Production adapter: core typed NDJSON -> typed CoreItem stream."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        core_origin: str,
        token: str,
        audience: str = "map-core",
    ) -> None:
        self._client = client
        self._core_origin = core_origin.rstrip("/")
        self._token = token
        self._audience = audience

    def _url(self, attempt: AttemptInput) -> str:
        return (
            f"{self._core_origin}"
            f"{_EVENTS_PATH.format(run_id=attempt.run_id, attempt=attempt.attempt)}"
        )

    def _headers(self, attempt: AttemptInput) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Service-Name": "map-bff",
            "X-Service-Audience": self._audience,
            "X-Workspace-ID": str(attempt.workspace_id),
            "X-Request-ID": str(attempt.run_id),
        }

    async def stream(self, attempt: AttemptInput) -> AsyncIterator[CoreItem]:
        """POST the attempt command to core and project the NDJSON lines.

        Every transport-level failure projects to a retryable
        :class:`CoreError`; it never falls back to the legacy SSE adapter.
        """
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=None, connect=20.0),
            trust_env=False,
        )
        request = client.build_request(
            "POST",
            self._url(attempt),
            json=dict(attempt.command.payload),
            headers=self._headers(attempt),
        )
        response: httpx.Response | None = None
        try:
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                yield CoreError(
                    code="STREAM_CORE_TRANSPORT",
                    message=f"core typed stream transport failure: {exc}",
                )
                return

            try:
                if response.status_code != 200:
                    yield CoreError(
                        code=f"STREAM_CORE_HTTP_{response.status_code}",
                        message=(
                            "core typed stream HTTP "
                            f"{response.status_code}"
                        ),
                    )
                    return

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    item = _project_line(line)
                    if isinstance(item, CoreError):
                        yield item
                        return
                    if isinstance(item, CoreOutcome):
                        yield item
                        return
                    yield item
                yield CoreError(
                    code=_STREAM_CORE_ERROR,
                    message="stream ended without stream.terminal",
                )
            except httpx.HTTPError as exc:
                yield CoreError(
                    code="STREAM_CORE_TRANSPORT",
                    message=f"core typed stream read failure: {exc}",
                )
        finally:
            if response is not None:
                await response.aclose()
            if owns_client:
                await client.aclose()
