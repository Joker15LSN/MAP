"""Artifact offloading adapters for the AgentScope runtime.

EXPERIMENTAL — not wired into production yet.

No production call site injects an ``ArtifactStorePort`` implementation
(``AgentRuntime._build_agentscope_agent()`` has no artifact store
dependency), so ``AgentScopeArtifactOffloader`` is never constructed in a
running service and the ``map.context.offload`` span never appears in
production traces. Before claiming Tool Result Offload as done, an artifact
store adapter, lifecycle injection, a config switch, failure fallback and
large-result tests are still required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agentscope.message import Msg, ToolResultBlock

from ...observability import get_tracer
from ...utils.serialization import safe_serialize

_tracer = get_tracer(__name__)


@dataclass
class ArtifactRef:
    """Minimal reference returned by an artifact store put()."""

    artifact_id: str
    uri: str


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Framework-neutral artifact store used for context offloading."""

    async def put(
        self,
        *,
        kind: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, Any],
    ) -> ArtifactRef: ...


class AgentScopeArtifactOffloader:
    """AgentScope Offloader backed by a MAP artifact store implementation."""

    def __init__(
        self,
        artifact_store: ArtifactStorePort,
        *,
        agent_code: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.agent_code = agent_code
        self.request_metadata = dict(request_metadata or {})

    def _metadata(self, session_id: str) -> dict[str, Any]:
        return {
            **self.request_metadata,
            "agent_code": self.agent_code,
            "agentscope_session_id": session_id,
        }

    async def offload_context(self, session_id: str, msgs: list[Msg]) -> str:
        with _tracer.start_as_current_span(
            "map.context.offload",
            attributes={
                "map.agent.code": self.agent_code,
                "map.context.message_count": len(msgs),
                "map.context.offload_kind": "agent_context",
            },
        ) as span:
            content = json.dumps(
                [safe_serialize(msg.model_dump(mode="json")) for msg in msgs],
                ensure_ascii=False,
            ).encode("utf-8")
            ref = await self.artifact_store.put(
                kind="agent_context",
                content=content,
                content_type="application/json",
                metadata=self._metadata(session_id),
            )
            span.set_attribute("map.artifact.id", ref.artifact_id)
            span.set_attribute("map.artifact.size_bytes", len(content))
            return ref.uri

    async def offload_tool_result(
        self,
        session_id: str,
        tool_result: ToolResultBlock,
    ) -> str:
        with _tracer.start_as_current_span(
            "map.context.offload",
            attributes={
                "map.agent.code": self.agent_code,
                "map.context.offload_kind": "tool_result",
                "map.tool.name": tool_result.name,
            },
        ) as span:
            content = json.dumps(
                safe_serialize(tool_result.model_dump(mode="json")),
                ensure_ascii=False,
            ).encode("utf-8")
            ref = await self.artifact_store.put(
                kind="tool_result",
                content=content,
                content_type="application/json",
                metadata=self._metadata(session_id),
            )
            span.set_attribute("map.artifact.id", ref.artifact_id)
            span.set_attribute("map.artifact.size_bytes", len(content))
            return ref.uri
