from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ...schema.agent_schema import Message


@dataclass
class ToolCallSession:
    messages: list[dict[str, Any]]
    tool_called: bool = False
    tool_observations: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_request(
        cls,
        *,
        request_query: str,
        history: Sequence[Message | dict[str, Any]] | None,
        history_normalizer: Callable[
            [Sequence[Message | dict[str, Any]] | None], list[dict[str, Any]]
        ],
        additional_user_prompt: str | None = None,
    ) -> ToolCallSession:
        messages = history_normalizer(history)
        query = request_query
        if additional_user_prompt:
            query = f"{additional_user_prompt}\n{query}"
        messages.append({"role": "user", "content": query})
        return cls(messages=messages)

    def append_assistant_tool_calls(self, response: Any) -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    call.model_dump() for call in (response.tool_calls or [])
                ],
            }
        )

    def append_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def append_tool_message(
        self,
        call_id: str,
        payload: Any,
        *,
        serializer: Callable[[Any], Any],
    ) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(serializer(payload), ensure_ascii=False),
            }
        )

    def append_tool_results(
        self,
        *,
        tool_calls: Sequence[Any],
        results: dict[str, Any],
        terminate_tool_name: str,
        serializer: Callable[[Any], Any],
    ) -> None:
        for call in tool_calls:
            if call.function.name == terminate_tool_name:
                continue
            if call.id in results:
                self.append_tool_message(call.id, results[call.id], serializer=serializer)

    def mark_tool_called(self, called: bool = True) -> None:
        self.tool_called = self.tool_called or called

    def add_tool_observations(self, observations: Sequence[dict[str, Any]]) -> None:
        self.tool_observations.extend(observations)

    def final_messages_with_assistant(self, content: str) -> list[dict[str, Any]]:
        return [
            *self.messages,
            {
                "role": "assistant",
                "content": content,
            },
        ]

    def latest_non_empty_content(self) -> str:
        for msg in reversed(self.messages):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""
