from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from loguru import logger

from ...schema.agent_schema import Message
from ..model_invocation import (
    ModelInvocation,
    ModelInvocationRequest,
    StructuredOutput,
)
from .prompts import (
    CONTEXT_COMPRESSION_SYSTEM_PROMPT,
    CONTEXT_COMPRESSION_USER_PROMPT_TEMPLATE,
)
from .schema import (
    ContextCompressionLLMOutput,
    ContextCompressionResult,
    ContextCompressorConfig,
)

SUMMARY_MESSAGE_NAME = "context_compressor"
SUMMARY_MESSAGE_PREFIX = "[COMPRESSED_HISTORY]"
_ALLOWED_KEYS_BY_ROLE: dict[str, set[str]] = {
    "system": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "name", "tool_calls", "function_call"},
    "tool": {"role", "content", "tool_call_id"},
}


async def compress_history(
    history: Sequence[Message | dict[str, Any]] | None,
    *,
    llm: ModelInvocation,
    config: ContextCompressorConfig | None = None,
    focus_instruction: str | None = None,
) -> ContextCompressionResult:
    """Compress old conversation history with an LLM and keep recent messages verbatim."""
    resolved_config = config or ContextCompressorConfig()
    normalized_history = normalize_history(history)
    original_chars = _count_chars(normalized_history)

    if not normalized_history:
        return _result(
            compressed_history=[],
            preserved_messages=[],
            original_message_count=0,
            original_chars=0,
            skipped=True,
            reason="empty_history",
        )

    if not resolved_config.enabled:
        return _result(
            compressed_history=normalized_history,
            preserved_messages=normalized_history,
            original_message_count=len(normalized_history),
            original_chars=original_chars,
            skipped=True,
            reason="disabled",
        )

    if not _should_compress(normalized_history, original_chars, resolved_config):
        return _result(
            compressed_history=normalized_history,
            preserved_messages=normalized_history,
            original_message_count=len(normalized_history),
            original_chars=original_chars,
            skipped=True,
            reason="below_threshold",
        )

    preserved_messages = _preserve_recent_messages(
        normalized_history, resolved_config.preserve_recent_messages
    )
    compressible_count = len(normalized_history) - len(preserved_messages)
    compressible_messages = normalized_history[:compressible_count]
    if not compressible_messages:
        return _result(
            compressed_history=preserved_messages,
            preserved_messages=preserved_messages,
            original_message_count=len(normalized_history),
            original_chars=original_chars,
            skipped=True,
            reason="no_compressible_messages",
        )

    history_text = render_history_for_compression(compressible_messages, resolved_config)
    prompt = CONTEXT_COMPRESSION_USER_PROMPT_TEMPLATE.format(
        focus_instruction=(focus_instruction or "无"),
        history_text=history_text,
    )

    try:
        messages = [
            {"role": "system", "content": CONTEXT_COMPRESSION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        outcome = await llm.invoke(
            ModelInvocationRequest(
                messages=messages,
                temperature=resolved_config.temperature,
                max_tokens=resolved_config.max_tokens,
                timeout=resolved_config.timeout,
                structured=StructuredOutput(
                    schema=ContextCompressionLLMOutput.model_json_schema(),
                    name="context_compression",
                    parse=False,
                ),
            )
        )
        outcome.raise_for_status()
        parsed = parse_llm_output(str(outcome.content or ""))
        summary = format_summary(parsed, max_chars=resolved_config.max_summary_chars)
        compressed_history = [build_summary_message(summary), *preserved_messages]
        usage = _normalize_usage(
            outcome.usage.to_dict() if outcome.usage else None
        )
        return _result(
            compressed_history=compressed_history,
            preserved_messages=preserved_messages,
            original_message_count=len(normalized_history),
            original_chars=original_chars,
            summary=summary,
            usage=usage,
            skipped=False,
            reason=None,
        )
    except Exception as exc:
        logger.warning("Context compression failed: {}", exc)
        if resolved_config.raise_on_error:
            raise
        return _fallback_trim_result(
            normalized_history,
            preserved_messages=preserved_messages,
            original_chars=original_chars,
            reason=f"llm_error:{type(exc).__name__}",
        )


def normalize_history(
    history: Sequence[Message | dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not history:
        return []

    normalized: list[dict[str, Any]] = []
    for msg in history:
        if isinstance(msg, Message):
            raw = msg.to_dict()
        elif isinstance(msg, dict):
            raw = dict(msg)
        else:
            continue

        role = raw.get("role")
        if role not in _ALLOWED_KEYS_BY_ROLE:
            continue

        allowed_keys = _ALLOWED_KEYS_BY_ROLE[str(role)]
        item = {
            k: _json_compatible(v)
            for k, v in raw.items()
            if k in allowed_keys
        }
        if "content" not in item:
            if role == "assistant" and (
                "tool_calls" in item or "function_call" in item
            ):
                item["content"] = ""
            else:
                continue

        if item.get("content") is None:
            if role == "assistant":
                item["content"] = ""
            else:
                continue
        elif not isinstance(item.get("content"), str):
            item["content"] = str(item.get("content"))

        if role == "tool" and "tool_call_id" not in item:
            continue

        item["role"] = role
        normalized.append(item)

    return normalized


def render_history_for_compression(
    messages: Sequence[dict[str, Any]],
    config: ContextCompressorConfig | None = None,
) -> str:
    resolved_config = config or ContextCompressorConfig()
    rendered_messages: list[str] = []
    omitted_large_messages = 0
    for index, message in enumerate(messages, start=1):
        content = str(message.get("content") or "")
        if len(content) > resolved_config.max_render_chars_per_message:
            truncated_chars = (
                len(str(message.get("content") or ""))
                - resolved_config.max_render_chars_per_message
            )
            content = (
                content[: resolved_config.max_render_chars_per_message]
                + f"\n...[truncated {truncated_chars} chars]"
            )
            omitted_large_messages += 1

        extras: dict[str, Any] = {}
        for key in ("name", "tool_call_id", "tool_calls", "function_call"):
            if key in message:
                extras[key] = message[key]
        extras_text = ""
        if extras:
            extras_text = "\nmetadata: " + json.dumps(
                extras, ensure_ascii=False, default=str
            )

        rendered_messages.append(
            f"<message index=\"{index}\" role=\"{message.get('role')}\">\n"
            f"{content}{extras_text}\n"
            f"</message>"
        )

    text = "\n\n".join(rendered_messages)
    if len(text) <= resolved_config.max_input_chars:
        return text

    suffix = text[-resolved_config.max_input_chars :]
    return (
        f"[omitted older rendered history; input exceeded {resolved_config.max_input_chars} chars; "
        f"large_messages_truncated={omitted_large_messages}]\n"
        f"{suffix}"
    )


def parse_llm_output(content: str) -> ContextCompressionLLMOutput:
    payload = _extract_json_object(content)
    return ContextCompressionLLMOutput.model_validate(payload)


def format_summary(output: ContextCompressionLLMOutput, *, max_chars: int) -> str:
    parts: list[str] = []
    summary = output.summary.strip()
    if summary:
        parts.append(f"概要: {summary}")
    _append_section(parts, "用户偏好/约束", output.user_preferences)
    _append_section(parts, "未完成问题", output.open_questions)
    _append_section(parts, "已确认决定", output.decisions)
    _append_section(parts, "关键实体", output.entities)
    _append_section(parts, "工具结果", output.tool_results)
    _append_section(parts, "注意事项", output.warnings)
    text = "\n".join(parts).strip()
    if not text:
        text = "无可保留的历史上下文。"
    if len(text) > max_chars:
        return (
            text[:max_chars]
            + f"\n...[summary truncated {len(text) - max_chars} chars]"
        )
    return text


def build_summary_message(summary: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "name": SUMMARY_MESSAGE_NAME,
        "content": f"{SUMMARY_MESSAGE_PREFIX}\n{summary}",
    }


def _should_compress(
    history: Sequence[dict[str, Any]],
    original_chars: int,
    config: ContextCompressorConfig,
) -> bool:
    return (
        len(history) >= config.trigger_message_count
        or original_chars >= config.trigger_char_count
    )


def _preserve_recent_messages(
    history: Sequence[dict[str, Any]],
    preserve_recent_messages: int,
) -> list[dict[str, Any]]:
    if preserve_recent_messages <= 0:
        return []
    if len(history) <= preserve_recent_messages:
        return list(history)
    return list(history[-preserve_recent_messages:])


def _fallback_trim_result(
    normalized_history: list[dict[str, Any]],
    *,
    preserved_messages: list[dict[str, Any]],
    original_chars: int,
    reason: str,
) -> ContextCompressionResult:
    return _result(
        compressed_history=preserved_messages,
        preserved_messages=preserved_messages,
        original_message_count=len(normalized_history),
        original_chars=original_chars,
        skipped=True,
        reason=reason,
    )


def _result(
    *,
    compressed_history: list[dict[str, Any]],
    preserved_messages: list[dict[str, Any]],
    original_message_count: int,
    original_chars: int,
    skipped: bool,
    reason: str | None,
    summary: str | None = None,
    usage: dict[str, int] | None = None,
) -> ContextCompressionResult:
    return ContextCompressionResult(
        compressed_history=compressed_history,
        summary=summary,
        preserved_messages=preserved_messages,
        original_message_count=original_message_count,
        compressed_message_count=len(compressed_history),
        original_chars=original_chars,
        compressed_chars=_count_chars(compressed_history),
        skipped=skipped,
        reason=reason,
        usage=usage,
    )


def _append_section(parts: list[str], title: str, values: Sequence[str]) -> None:
    normalized = [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]
    if not normalized:
        return
    parts.append(f"{title}:")
    parts.extend(f"- {value}" for value in normalized)


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise
        payload = json.loads(text[start : end + 1])

    if not isinstance(payload, dict):
        raise ValueError("LLM compression output must be a JSON object")
    return payload


def _count_chars(messages: Sequence[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, default=str))


def _json_compatible(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    try:
        json.dumps(value, ensure_ascii=False)
    except TypeError:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    return value


def _normalize_usage(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    usage = {
        k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, int)
    }
    return usage or None
