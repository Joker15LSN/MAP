from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from loguru import logger

from ..schema.agent_schema import Message
from ..schema.attachment_schema import UploadedKBFileSchema
from .model_invocation import ModelInvocation, ModelInvocationRequest


class QueryRewriter:
    DEFAULT_TIMEOUT_S = 5.0

    def __init__(
        self,
        *,
        llm: ModelInvocation,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        logger_: Any | None = None,
    ) -> None:
        self.llm = llm
        self.timeout_s = timeout_s
        self._logger = logger_ or logger

    async def rewrite(
        self,
        query: str,
        history: Sequence[dict[str, Any]] | Sequence[Message] | Sequence[Any] | None,
        *,
        uploaded_kb_files: Sequence[UploadedKBFileSchema] | None = None,
    ) -> str:
        """Rewrite input query using latest 1-turn history to resolve ambiguity."""
        if (not history or len(history) == 0) and not uploaded_kb_files:
            return query

        try:
            history_text = self._build_latest_turn_text(history or [])
            uploaded_file_text = self._build_uploaded_file_context(uploaded_kb_files)
            if not history_text and not uploaded_file_text:
                return query

            response = await asyncio.wait_for(
                self._call_llm(
                    query=query,
                    history_text=history_text,
                    uploaded_file_text=uploaded_file_text,
                ),
                timeout=self.timeout_s,
            )
            rewritten = response.content.strip()
            self._logger.info(f"Query rewrite: '{query}' -> '{rewritten}'")
            return rewritten

        except asyncio.TimeoutError:
            self._logger.warning(
                f"Query rewrite timed out ({self.timeout_s}s), using original query."
            )
            return query
        except Exception as exc:
            self._logger.warning(f"Query rewrite failed: {exc}, using original query.")
            return query

    @staticmethod
    def _build_latest_turn_text(
        history: Sequence[dict[str, Any]] | Sequence[Message] | Sequence[Any],
    ) -> str:
        latest_turn = history[-2:] if len(history) >= 2 else history
        normalized_turn: list[dict[str, Any]] = []
        for msg in latest_turn:
            if isinstance(msg, dict):
                normalized_turn.append(msg)
                continue
            if isinstance(msg, Message):
                normalized_turn.append(msg.to_dict())

        return "\n".join(
            [
                f"{msg['role']}: {msg['content']}"
                for msg in normalized_turn
                if "role" in msg and "content" in msg
            ]
        )

    @staticmethod
    def _build_uploaded_file_context(
        uploaded_kb_files: Sequence[UploadedKBFileSchema] | None,
    ) -> str:
        if not uploaded_kb_files:
            return ""

        file_names = [
            kb_file.file_name.strip()
            for kb_file in uploaded_kb_files
            if kb_file.file_name.strip()
        ]
        if not file_names:
            return ""

        file_lines = [
            f"{index}. {file_name}" for index, file_name in enumerate(file_names, 1)
        ]
        return "用户当前上传文件列表如下，顺序从旧到新，越靠后越新：\n" + "\n".join(
            file_lines
        )

    async def _call_llm(
        self,
        *,
        query: str,
        history_text: str,
        uploaded_file_text: str,
    ) -> Any:
        system_prompt = (
            "你是意图理解专家。请根据历史对话背景，补全或重写用户当前的简短问题，使其意图清晰完整。\n"
            "规则：\n"
            "1. 如果用户问题已经完整，直接返回原问题。\n"
            "2. 如果用户问题指代不明（如“它”、“这个”、“刚才的”），结合历史补全主语或上下文。\n"
            "3. 如果上一轮 assistant 给出了推荐问题，当前用户只回复“好的”“可以”等简短确认，"
            "请把该推荐问题显式带上，再保留用户当前确认内容。\n"
            "只能使用上一轮 assistant 原文中明确出现的推荐问题，不得编造或套用其他示例。\n"
            "4. 如果上一轮 assistant 没有明确推荐问题，用户又只回复“好的”“可以”等简短确认，"
            "不要引入新主题；能从上下文确认继续事项就简短补全，否则直接返回原问题。\n"
            "5. 仅输出重写后的问题，不要包含任何解释，不得反问。\n"
            "6. 如果用户上传了文件，请结合上传文件列表补全当前问题中的文件指代。\n"
            "7. 上传文件列表越靠后越新；当用户问题没有明确指定文件，例如“总结一下”“分析这个”“提炼重点”，默认优先指向最新上传的文件。\n"
            "8. 如果用户问题明确提到某个文件名或多个文件，应尊重用户显式指定，不要强行改成最新文件。\n"
            "9. 改写时可以包含文件名，以便后续工具准确定位文件。\n"
        )
        current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        file_context = (
            f"\n\n上传文件：\n{uploaded_file_text}" if uploaded_file_text else ""
        )
        prompt = (
            f"当前日期：{current_date}\n\n"
            f"历史对话背景：\n{history_text or '<无>'}"
            f"{file_context}\n\n当前问题：{query}\n重写后的问题："
        )

        outcome = await self.llm.invoke(
            ModelInvocationRequest(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
            )
        )
        outcome.raise_for_status()
        return outcome
