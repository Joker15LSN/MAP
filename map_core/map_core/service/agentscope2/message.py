from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agentscope.message import (
    AssistantMsg,
    DataBlock,
    HintBlock,
    Msg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)

from ...schema.agent_schema import Message
from ...utils.serialization import safe_serialize


def _history_item_to_dict(item: Message | dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(item, Message):
        return item.to_dict()
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict") and callable(item.to_dict):
        value = item.to_dict()
        if isinstance(value, dict):
            return dict(value)
    return {"role": "user", "content": str(item)}


def _data_block_text(block: DataBlock) -> str:
    return json.dumps(safe_serialize(block.model_dump()), ensure_ascii=False)


def _tool_result_text(block: ToolResultBlock) -> str:
    if isinstance(block.output, str):
        return block.output
    parts: list[str] = []
    for item in block.output:
        if isinstance(item, TextBlock):
            parts.append(item.text)
        elif isinstance(item, DataBlock):
            parts.append(_data_block_text(item))
        else:
            parts.append(str(item))
    return "".join(parts)


def message_text(message: Msg | None) -> str:
    if message is None:
        return ""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, DataBlock):
            parts.append(_data_block_text(block))
        elif isinstance(block, HintBlock):
            if isinstance(block.hint, str):
                parts.append(block.hint)
            else:
                for item in block.hint:
                    if isinstance(item, TextBlock):
                        parts.append(item.text)
                    elif isinstance(item, DataBlock):
                        parts.append(_data_block_text(item))
    return "".join(parts)


def message_reasoning(message: Msg | None) -> str | None:
    if message is None:
        return None
    parts = [
        block.thinking
        for block in message.content
        if isinstance(block, ThinkingBlock) and block.thinking
    ]
    content = "".join(parts).strip()
    return content or None


def agentscope_messages_to_openai(messages: Sequence[Msg]) -> list[dict[str, Any]]:
    """Convert AgentScope messages to the OpenAI-compatible shape MAP uses."""

    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role != "assistant":
            payload = {
                "role": message.role,
                "content": message_text(message),
            }
            if message.name and message.name != message.role:
                payload["name"] = message.name
            converted.append(payload)
            continue

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        emitted_block = False

        def flush_assistant() -> None:
            nonlocal emitted_block
            if not (text_parts or reasoning_parts or tool_calls):
                return
            assistant_payload: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            reasoning = "".join(reasoning_parts).strip()
            if reasoning:
                assistant_payload["reasoning_content"] = reasoning
            if tool_calls:
                assistant_payload["tool_calls"] = list(tool_calls)
            converted.append(assistant_payload)
            text_parts.clear()
            reasoning_parts.clear()
            tool_calls.clear()
            emitted_block = True

        for block in message.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, DataBlock):
                text_parts.append(_data_block_text(block))
            elif isinstance(block, ThinkingBlock):
                reasoning_parts.append(block.thinking)
            elif isinstance(block, HintBlock):
                flush_assistant()
                hint_message = AssistantMsg(name=message.name, content=[block])
                converted.append(
                    {
                        "role": "user",
                        "content": message_text(hint_message),
                    }
                )
                emitted_block = True
            elif isinstance(block, ToolCallBlock):
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": block.input,
                        },
                    }
                )
            elif isinstance(block, ToolResultBlock):
                flush_assistant()
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.id,
                        "content": _tool_result_text(block),
                    }
                )
                emitted_block = True

        flush_assistant()
        if not emitted_block:
            converted.append({"role": "assistant", "content": ""})
    return converted


def agentscope_context_to_map(messages: Sequence[Msg]) -> list[dict[str, Any]]:
    return agentscope_messages_to_openai(messages)


def map_history_to_agentscope(
    history: Sequence[Message | dict[str, Any]] | Sequence[Any] | None,
) -> list[Msg]:
    if not history:
        return []

    converted: list[Msg] = []
    for raw_item in history:
        item = _history_item_to_dict(raw_item)
        role = item.get("role")
        content = item.get("content")
        content_text = "" if content is None else str(content)
        name = str(item.get("name") or role or "user")

        if role == "system":
            converted.append(SystemMsg(name=name, content=content_text))
            continue
        if role == "user":
            converted.append(UserMsg(name=name, content=content_text))
            continue
        if role == "tool":
            result_block = ToolResultBlock(
                id=str(item.get("tool_call_id") or "missing-tool-call-id"),
                name=name,
                output=content_text,
                state=ToolResultState.SUCCESS,
            )
            if converted and converted[-1].role == "assistant":
                converted[-1].content.append(result_block)
            else:
                converted.append(AssistantMsg(name="assistant", content=[result_block]))
            continue

        blocks: list[Any] = []
        reasoning = item.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            blocks.append(ThinkingBlock(thinking=reasoning))
        if content_text:
            blocks.append(TextBlock(text=content_text))
        for tool_call in item.get("tool_calls") or []:
            if hasattr(tool_call, "model_dump"):
                tool_call = tool_call.model_dump()
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            blocks.append(
                ToolCallBlock(
                    id=str(tool_call.get("id") or "missing-tool-call-id"),
                    name=str(function.get("name") or "unknown_tool"),
                    input=str(function.get("arguments") or "{}"),
                )
            )
        converted.append(AssistantMsg(name=name, content=blocks))
    return converted
