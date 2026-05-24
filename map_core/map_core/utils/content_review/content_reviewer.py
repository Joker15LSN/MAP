from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from ...config.common import REVIEWER_LLM_CONFIG
from ...config.config_schema import LLMConfig
from ...schema.global_domain_schema import GlobalDomainStreamEvent
from ..llm_engine import LLMEngine
from .review_flags import (
    ENABLED_REVIEW_FLAG_CODES,
    lookup_review_flag,
    normalize_review_flag_codes,
)

CONTENT_REVIEW_BLOCK_CODE = "content_review_blocked"
CONTENT_REVIEW_BLOCK_MARKER = "[[CONTENT_REVIEW_BLOCKED]]"
CONTENT_REVIEW_BLOCK_PHASE = "content_review_blocked"
CONTENT_REVIEW_SAFE_LABEL = "sec"
CONTENT_REVIEW_TOP_LOGPROBS = 2
CONTENT_REVIEW_BLOCK_PROB_THRESHOLD = 0.7

CONTENT_REVIEW_COMPANY_POLICY_KEYWORDS: list[str] = []
CONTENT_REVIEW_TIMEOUT = 5.0
CONTENT_REVIEW_WINDOW_SIZE = 300

CONTENT_REVIEW_OVERLAP_SIZE = 60

XGUARD_POLICY = """
- c: Enterprise Policy - Economic and Legal Issue Filter
  - Classify text as violation if it describes or reports actual economic crimes or legal events.
  - The following MUST be classified as violation:
    - Describing a specific economic crime event: embezzlement, misappropriation of funds, financial fraud, bribery, corruption, illegal fundraising, money laundering that actually occurred.
    - Reporting a specific legal event: criminal investigation, arrest, detention, imprisonment, trial, conviction, serving sentence, release from prison, probation that actually occurred.
    - Reporting specific corporate misconduct: illegal business operations, regulatory violations, corporate fraud, securities violations that actually occurred.
  - Important: Even balanced, factual, or objective reporting of an actual event is a violation.
  - The following should be classified as safe (NOT blocked):
    - General financial knowledge, financial education, or financial literacy content.
    - Discussion of fraud prevention, anti-corruption measures, compliance systems, or regulatory frameworks.
    - Discussion of laws, regulations, or policies in an abstract or educational context.
    - Corporate governance, risk management, internal controls, or compliance training topics.
    - General discussion of financial markets, investments, or economic trends without mentioning specific crimes.
    - Positive or neutral content about companies, executives, or business operations.
""".strip()


@dataclass(slots=True)
class _SourceStreamEnded:
    pass


@dataclass(slots=True)
class _SourceStreamFailed:
    error: Exception


@dataclass(slots=True)
class _ReviewBlockedSignal:
    stage: str
    result: ContentReviewResult


SOURCE_STREAM_ENDED = _SourceStreamEnded()


class ContentReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sexual: bool = False
    political_sensitive: bool = False
    violent_crime: bool = False
    company_policy_violation: bool = False
    blocked: bool = False
    reason: str = ""
    reviewed_text: str = Field(default="", exclude=True)
    _classification_label: str = PrivateAttr(default="")
    _confidence: float | None = PrivateAttr(default=None)

    def is_blocked(self) -> bool:
        return self.blocked or any(
            [
                self.sexual,
                self.political_sensitive,
                self.violent_crime,
                self.company_policy_violation,
            ]
        )

    @property
    def classification_label(self) -> str:
        return self._classification_label

    def set_classification_label(self, label: str | None) -> None:
        self._classification_label = (label or "").strip()

    @property
    def confidence(self) -> float | None:
        return self._confidence

    def set_confidence(self, confidence: float | None) -> None:
        self._confidence = confidence

class StreamContentReviewer:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        company_policy_keywords: list[str] | None = None,
        company_policy_instruction: str | None = None,
        temperature: float = 0.0,
        timeout: float = 15.0,
        window_size: int = 20,
        overlap_size: int = 10,
        enabled: bool | None = None,
        enabled_review_flag_codes: list[str] | set[str] | tuple[str, ...] | None = None,
        chat_template_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.company_policy_keywords = [
            keyword.strip()
            for keyword in (company_policy_keywords or [])
            if keyword and keyword.strip()
        ]
        self.company_policy_instruction = (
            company_policy_instruction.strip()
            if company_policy_instruction and company_policy_instruction.strip()
            else None
        )
        self.temperature = temperature
        self.timeout = timeout
        self.window_size = max(window_size, 1)
        self.overlap_size = min(max(overlap_size, 0), self.window_size - 1)
        self.release_size = max(self.window_size - self.overlap_size, 1)
        self.enabled = bool(api_key) if enabled is None else enabled
        self.enabled_review_flag_codes = normalize_review_flag_codes(
            ENABLED_REVIEW_FLAG_CODES
            if enabled_review_flag_codes is None
            else enabled_review_flag_codes
        )
        self.chat_template_kwargs = dict(chat_template_kwargs) if chat_template_kwargs else {}
        self._llm: LLMEngine | None = None
        self._suppress_nested_review_logs = 0
        self.block_prob_threshold = CONTENT_REVIEW_BLOCK_PROB_THRESHOLD

    async def aclose(self) -> None:
        if self._llm is not None:
            await self._llm.aclose()
            self._llm = None

    async def moderate_event_stream(
        self,
        event_stream: AsyncGenerator[GlobalDomainStreamEvent, None],
        *,
        request_id: str = "missing",
        state_id: str = "missing",
    ) -> AsyncGenerator[GlobalDomainStreamEvent, None]:
        if not self.enabled:
            async for event in event_stream:
                yield event
            return

        logger.info(
            "Content review started for event stream: request_id={}, state_id={}, model={}, window_size={}, overlap_size={}, block_prob_threshold={}",
            request_id,
            state_id,
            self.model,
            self.window_size,
            self.overlap_size,
            self.block_prob_threshold,
        )
        resolved_request_id = request_id
        resolved_state_id = state_id
        pending_review_text = ""
        pending_done_event: GlobalDomainStreamEvent | None = None
        block_started = False
        review_queue_closed = False
        source_stream_finished = False

        source_queue: asyncio.Queue[
            GlobalDomainStreamEvent | _SourceStreamEnded | _SourceStreamFailed
        ] = asyncio.Queue()
        review_queue: asyncio.Queue[str | None] = asyncio.Queue()
        signal_queue: asyncio.Queue[_ReviewBlockedSignal] = asyncio.Queue()

        source_task = asyncio.create_task(
            self._pump_source_stream(event_stream, source_queue)
        )
        review_task = asyncio.create_task(
            self._pump_review_windows(review_queue, signal_queue)
        )

        next_source_task: asyncio.Task[Any] | None = asyncio.create_task(source_queue.get())
        next_signal_task: asyncio.Task[Any] | None = asyncio.create_task(signal_queue.get())
        review_join_task: asyncio.Task[Any] | None = None

        try:
            while True:
                wait_tasks = [
                    task
                    for task in (next_source_task, next_signal_task, review_join_task)
                    if task is not None
                ]
                if not wait_tasks:
                    break

                done_tasks, _ = await asyncio.wait(
                    wait_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if next_signal_task in done_tasks:
                    signal = next_signal_task.result()
                    next_signal_task = asyncio.create_task(signal_queue.get())

                    if signal.stage == "blocked_start" and not block_started:
                        block_started = True
                        yield self._build_blocked_meta_event(
                            result=signal.result,
                            request_id=resolved_request_id,
                            state_id=resolved_state_id,
                        )
                        await self._safe_close_stream(event_stream)
                        if not source_task.done():
                            source_task.cancel()
                        if not review_queue_closed:
                            await review_queue.put(None)
                            review_queue_closed = True
                        if review_join_task is not None:
                            review_join_task.cancel()
                            review_join_task = None
                        pending_done_event = None
                        continue

                    if signal.stage == "blocked_final":
                        self._log_block(
                            "event_stream_block_final",
                            signal.result,
                            request_id=resolved_request_id,
                            state_id=resolved_state_id,
                            review_window_text=signal.result.reviewed_text,
                        )
                        yield self._build_blocked_event(
                            result=signal.result,
                            request_id=resolved_request_id,
                            state_id=resolved_state_id,
                        )
                        return

                if next_source_task in done_tasks:
                    item = next_source_task.result()
                    next_source_task = None

                    if isinstance(item, _SourceStreamFailed):
                        logger.exception(
                            "Content review source stream failed"
                        )
                        raise item.error

                    if item is SOURCE_STREAM_ENDED:
                        source_stream_finished = True
                        if pending_done_event is None and not block_started:
                            break
                        continue

                    if not source_stream_finished:
                        next_source_task = asyncio.create_task(source_queue.get())

                    event = item
                    if event.event == "start":
                        data = self._event_data_as_dict(event)
                        resolved_request_id = str(
                            data.get("request_id") or resolved_request_id
                        )
                        resolved_state_id = str(
                            data.get("state_id") or resolved_state_id
                        )
                        if not block_started:
                            yield event
                        continue

                    if event.event == "content_delta":
                        text = str(self._event_data_as_dict(event).get("content") or "")
                        if text:
                            pending_review_text = await self._submit_review_windows(
                                review_queue,
                                pending_review_text + text,
                            )
                        if not block_started:
                            yield event
                        continue

                    if event.event == "done":
                        pending_review_text = await self._submit_review_windows(
                            review_queue,
                            pending_review_text,
                            flush=True,
                        )
                        pending_done_event = event
                        if not review_queue_closed:
                            await review_queue.put(None)
                            review_queue_closed = True
                        if review_join_task is None:
                            review_join_task = asyncio.create_task(review_queue.join())
                        continue

                    if event.event == "error":
                        if not block_started:
                            yield event
                        return

                    if not block_started:
                        yield event

                if review_join_task in done_tasks and review_join_task is not None:
                    await review_join_task
                    review_join_task = None
                    if pending_done_event is not None and not block_started:
                        yield pending_done_event
                        return
        finally:
            for task in (next_source_task, next_signal_task, review_join_task):
                if task is not None and not task.done():
                    task.cancel()
            if not review_queue_closed:
                try:
                    await review_queue.put(None)
                except RuntimeError:
                    pass
            if not source_task.done():
                source_task.cancel()
            if not review_task.done():
                review_task.cancel()
            await self._await_background_task(source_task, task_name="source_task")
            await self._await_background_task(review_task, task_name="review_task")

    async def review_text(self, text: str) -> ContentReviewResult:
        return await self.review_text_stream(text)

    async def review_text_stream(
        self,
        text: str,
        *,
        on_block_detected: Callable[[ContentReviewResult], Awaitable[None]] | None = None,
    ) -> ContentReviewResult:
        normalized_text = text.strip()
        if not normalized_text or not self.enabled:
            return ContentReviewResult(reviewed_text=normalized_text)
        log_start = self._suppress_nested_review_logs == 0
        log_block = self._suppress_nested_review_logs == 0

        matched_keyword = self._match_company_policy_keyword(normalized_text)
        if matched_keyword is not None:
            if log_start:
                logger.warning(
                    "Content review started for text: source=company_policy, model={}, text_preview={}",
                    self.model,
                    self._preview_text(normalized_text),
                )
            result = ContentReviewResult(
                company_policy_violation=True,
                blocked=True,
                reason=f"company_policy_keyword:{matched_keyword}",
                reviewed_text=normalized_text,
            )
            result.set_classification_label("company_policy_violation")
            result.set_confidence(1.0)
            if log_block:
                self._log_block(
                    "company_policy_keyword",
                    result,
                    keyword=matched_keyword,
                    text=normalized_text,
                )
            if on_block_detected is not None:
                await on_block_detected(result)
            return result

        try:
            if log_start:
                logger.warning(
                    "Content review started for text: source=xguard, model={}, text_preview={}",
                    self.model,
                    self._preview_text(normalized_text),
                )
            llm = self._get_or_create_llm()
            review_stream = llm.asimple_chat_stream(
                prompt=self._build_user_prompt(normalized_text),
                system_prompt=self._build_stream_system_prompt(),
                logprobs=True,
                top_logprobs=CONTENT_REVIEW_TOP_LOGPROBS,
                max_tokens=REVIEWER_LLM_CONFIG.max_tokens,
            )

            blocked_signal_sent = False
            classification_result: ContentReviewResult | None = None
            output_parts: list[str] = []
            classification_text = ""

            async for chunk in review_stream:
                if not isinstance(chunk, dict) or chunk.get("type") != "content":
                    continue

                content_piece = str(chunk.get("data") or "")
                if not content_piece:
                    continue

                output_parts.append(content_piece)
                if classification_result is not None:
                    continue

                classification_text += content_piece
                label = self._extract_stream_classification_label(
                    chunk=chunk,
                    accumulated_text=classification_text,
                )
                if label is None:
                    continue

                classification_result = self._build_stream_classification_result(
                    label=label,
                    reviewed_text=normalized_text,
                )
                token_probability = self._extract_chunk_token_probability(chunk)
                classification_result.set_confidence(token_probability)
                if (
                    classification_result.is_blocked()
                    and token_probability is not None
                    and token_probability < self.block_prob_threshold
                ):
                    logger.warning(
                        "Content review skipped block due to low confidence: model={}, classification_label={}, token_probability={}, threshold={}, text_preview={}",
                        self.model,
                        classification_result.classification_label or "unknown",
                        round(token_probability, 6),
                        self.block_prob_threshold,
                        self._preview_text(normalized_text),
                    )
                    await self._safe_close_stream(review_stream)
                    return ContentReviewResult(reviewed_text=normalized_text)
                if classification_result.is_blocked() and not blocked_signal_sent:
                    blocked_signal_sent = True
                    if on_block_detected is not None:
                        await on_block_detected(classification_result)
                if not classification_result.is_blocked():
                    await self._safe_close_stream(review_stream)
                    return classification_result

            if classification_result is None:
                logger.warning(
                    "Content review stream ended without classification; allow text"
                )
                return ContentReviewResult(reviewed_text=normalized_text)

            if classification_result.is_blocked():
                explanation = self._extract_stream_explanation("".join(output_parts))
                if explanation:
                    classification_result.reason = explanation
                elif not classification_result.reason:
                    label = classification_result.classification_label or "blocked"
                    classification_result.reason = f"classification:{label}"
                if log_block:
                    self._log_block(
                        "xguard_final",
                        classification_result,
                        text=normalized_text,
                    )

            return classification_result
        except Exception:
            logger.exception(
                "Streaming content review request failed; allow stream to continue"
            )
            return ContentReviewResult(reviewed_text=normalized_text)

    @staticmethod
    def _build_user_prompt(text: str) -> str:
        return f"待审核文本：{text}"

    def _build_stream_system_prompt(self) -> str:
        prompt = (
            "你是 YuFeng-XGuard 内容审查器。"
            f"请按 XGuard 的分类方式先输出一个风险标签；安全内容输出 {CONTENT_REVIEW_SAFE_LABEL}。"
            "如果判定为违规，可在后续输出中补充 <explanation>...</explanation> 说明原因。"
            "不要输出 JSON，不要改写输入文本。"
        )
        if self.company_policy_instruction:
            prompt += (
                " 额外业务策略如下，请将其视为最高优先级的补充审查规则："
                f"{self.company_policy_instruction}。"
            )
        if self.company_policy_keywords:
            prompt += (
                " 额外关键词策略："
                + "、".join(self.company_policy_keywords)
                + "。命中任一关键词时必须判定为违规。"
            )
        return prompt

    def _get_or_create_llm(self) -> LLMEngine:
        llm = self._llm
        if llm is None:
            llm = LLMEngine(
                config=LLMConfig(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model=self.model,
                    temperature=self.temperature,
                    timeout=self.timeout,
                    logprobs=True,
                    top_logprobs=CONTENT_REVIEW_TOP_LOGPROBS,
                    chat_template_kwargs=self.chat_template_kwargs,
                )
            )
            self._llm = llm
        return llm

    def _match_company_policy_keyword(self, text: str) -> str | None:
        for keyword in self.company_policy_keywords:
            if keyword in text:
                return keyword
        return None

    async def _drain_pending(
        self,
        pending: str,
        *,
        flush: bool,
    ) -> tuple[ContentReviewResult | None, str, list[str]]:
        safe_parts: list[str] = []

        while len(pending) >= self.window_size:
            self._suppress_nested_review_logs += 1
            try:
                result = await self.review_text(pending[: self.window_size])
            finally:
                self._suppress_nested_review_logs -= 1
            if result.is_blocked():
                return result, pending, safe_parts

            safe_parts.append(pending[: self.release_size])
            pending = pending[self.release_size :]

        if flush and pending:
            self._suppress_nested_review_logs += 1
            try:
                result = await self.review_text(pending)
            finally:
                self._suppress_nested_review_logs -= 1
            if result.is_blocked():
                return result, pending, safe_parts
            safe_parts.append(pending)
            pending = ""

        return None, pending, safe_parts

    async def _submit_review_windows(
        self,
        review_queue: asyncio.Queue[str | None],
        pending_text: str,
        *,
        flush: bool = False,
    ) -> str:
        while len(pending_text) >= self.window_size:
            await review_queue.put(pending_text[: self.window_size])
            pending_text = pending_text[self.release_size :]

        if flush and pending_text:
            await review_queue.put(pending_text)
            return ""

        return pending_text

    async def _pump_source_stream(
        self,
        event_stream: AsyncGenerator[GlobalDomainStreamEvent, None],
        source_queue: asyncio.Queue[
            GlobalDomainStreamEvent | _SourceStreamEnded | _SourceStreamFailed
        ],
    ) -> None:
        try:
            async for event in event_stream:
                await source_queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await source_queue.put(_SourceStreamFailed(error=exc))
        finally:
            await source_queue.put(SOURCE_STREAM_ENDED)

    async def _pump_review_windows(
        self,
        review_queue: asyncio.Queue[str | None],
        signal_queue: asyncio.Queue[_ReviewBlockedSignal],
    ) -> None:
        async def notify_block_start(result: ContentReviewResult) -> None:
            await signal_queue.put(_ReviewBlockedSignal("blocked_start", result))

        while True:
            window_text = await review_queue.get()
            try:
                if window_text is None:
                    return

                self._suppress_nested_review_logs += 1
                try:
                    result = await self.review_text_stream(
                        window_text,
                        on_block_detected=notify_block_start,
                    )
                finally:
                    self._suppress_nested_review_logs -= 1
                if result.is_blocked():
                    await signal_queue.put(_ReviewBlockedSignal("blocked_final", result))
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Content review window failed; continue stream")
            finally:
                review_queue.task_done()

    @staticmethod
    async def _await_background_task(
        task: asyncio.Task[Any],
        *,
        task_name: str,
    ) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Content review background task failed: {}", task_name)

    @staticmethod
    def _extract_stream_classification_label(
        *,
        chunk: dict[str, Any],
        accumulated_text: str,
    ) -> str | None:
        token_label = StreamContentReviewer._extract_logprob_token_label(chunk)
        if token_label:
            return token_label

        normalized = accumulated_text.lstrip()
        if not normalized:
            return None

        match = re.match(r"([A-Za-z0-9_:-]+)(?=$|[\s<])", normalized)
        if match:
            return match.group(1)
        if normalized.lower() == CONTENT_REVIEW_SAFE_LABEL:
            return normalized
        return None

    @staticmethod
    def _extract_logprob_token_label(chunk: dict[str, Any]) -> str | None:
        logprobs = chunk.get("logprobs")
        if not isinstance(logprobs, dict):
            return None
        content_items = logprobs.get("content")
        if not isinstance(content_items, list) or not content_items:
            return None
        first_item = content_items[0]
        if not isinstance(first_item, dict):
            return None
        token = str(first_item.get("token") or "").strip()
        return token or None

    @staticmethod
    def _extract_chunk_token_probability(chunk: dict[str, Any]) -> float | None:
        logprobs = chunk.get("logprobs")
        if not isinstance(logprobs, dict):
            return None
        content_items = logprobs.get("content")
        if not isinstance(content_items, list) or not content_items:
            return None
        first_item = content_items[0]
        if not isinstance(first_item, dict):
            return None
        raw_logprob = first_item.get("logprob")
        if not isinstance(raw_logprob, (int, float)):
            return None
        return math.exp(float(raw_logprob))

    def _build_stream_classification_result(
        self,
        *,
        label: str,
        reviewed_text: str,
    ) -> ContentReviewResult:
        normalized_label = label.strip()
        lowered_label = normalized_label.lower()

        result = ContentReviewResult(reviewed_text=reviewed_text)
        result.set_classification_label(normalized_label)

        if lowered_label == CONTENT_REVIEW_SAFE_LABEL:
            return result

        if (
            lookup_review_flag(lowered_label) is not None
            and lowered_label not in self.enabled_review_flag_codes
        ):
            result.reason = f"classification_ignored:{normalized_label}"
            return result

        result.blocked = True
        result.reason = f"classification:{normalized_label}"
        if lowered_label in {"sexual", "porn", "pornography"}:
            result.sexual = True
        elif lowered_label in {"political_sensitive", "politics", "political"}:
            result.political_sensitive = True
        elif lowered_label in {"violent_crime", "violence", "crime"}:
            result.violent_crime = True
        elif lowered_label in {
            "company_policy_violation",
            "company_policy",
            "policy",
            "c",
        }:
            result.company_policy_violation = True

        return result

    @staticmethod
    def _extract_stream_explanation(output_text: str) -> str:
        match = re.search(
            r"<explanation>\s*(.*?)\s*</explanation>",
            output_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()

        lines = [line.strip() for line in output_text.splitlines() if line.strip()]
        if len(lines) >= 2:
            return "\n".join(lines[1:]).strip()
        return ""

    @staticmethod
    async def _safe_close_stream(stream: AsyncGenerator[Any, None]) -> None:
        try:
            await stream.aclose()
        except RuntimeError:
            pass
        except Exception:
            logger.exception("Failed to close source stream after moderation block")

    @staticmethod
    def _event_data_as_dict(event: Any) -> dict[str, Any]:
        data = getattr(event, "data", None)
        if isinstance(data, dict):
            return data
        model_dump = getattr(data, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        return {}

    @staticmethod
    def _format_review_result(result: ContentReviewResult) -> dict[str, Any]:
        flag_id = result.classification_label or "unknown"
        flag_info = lookup_review_flag(flag_id)
        return {
            "flag_id": flag_id,
            "risk_dimension": flag_info.risk_dimension if flag_info is not None else None,
            "risk_category": flag_info.risk_category if flag_info is not None else None,
            "confidence": result.confidence,
            "blocked": result.blocked,
            "reason": result.reason,
            "review_window_text": result.reviewed_text,
        }

    @staticmethod
    def _preview_text(text: str, limit: int = 120) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit] + "..."

    def _log_block(
        self,
        stage: str,
        result: ContentReviewResult,
        **context: Any,
    ) -> None:
        logger.warning(
            "Content review blocked: stage={}, model={}, classification_label={}, confidence={}, reason={}, context={}",
            stage,
            self.model,
            result.classification_label or "unknown",
            None if result.confidence is None else round(result.confidence, 6),
            result.reason or "",
            {
                key: self._preview_text(value) if isinstance(value, str) else value
                for key, value in context.items()
            },
        )

    @classmethod
    def _build_blocked_meta_event(
        cls,
        *,
        result: ContentReviewResult,
        request_id: str,
        state_id: str,
    ) -> GlobalDomainStreamEvent:
        return GlobalDomainStreamEvent(
            event="meta",
            data={
                "phase": CONTENT_REVIEW_BLOCK_PHASE,
                "request_id": request_id,
                "state_id": state_id,
                "review_result": cls._format_review_result(result),
            },
        )

    @classmethod
    def _build_blocked_event(
        cls,
        *,
        result: ContentReviewResult,
        request_id: str,
        state_id: str,
    ) -> GlobalDomainStreamEvent:
        return GlobalDomainStreamEvent(
            event="error",
            data={
                "error": "stream blocked by content review",
                "code": CONTENT_REVIEW_BLOCK_CODE,
                "request_id": request_id,
                "state_id": state_id,
                "review_result": cls._format_review_result(result),
            },
        )


def build_stream_content_reviewer(
    *,
    enabled: bool,
    company_policy_instruction: str | None,
) -> StreamContentReviewer:
    return StreamContentReviewer(
        base_url=REVIEWER_LLM_CONFIG.base_url,
        api_key=REVIEWER_LLM_CONFIG.api_key,
        model=REVIEWER_LLM_CONFIG.model,
        company_policy_keywords=list(CONTENT_REVIEW_COMPANY_POLICY_KEYWORDS),
        company_policy_instruction=company_policy_instruction,
        temperature=REVIEWER_LLM_CONFIG.temperature,
        timeout=CONTENT_REVIEW_TIMEOUT,
        window_size=CONTENT_REVIEW_WINDOW_SIZE,
        overlap_size=CONTENT_REVIEW_OVERLAP_SIZE,
        enabled=enabled,
        enabled_review_flag_codes=ENABLED_REVIEW_FLAG_CODES,
        chat_template_kwargs={"policy": XGUARD_POLICY} if XGUARD_POLICY else None,
    )
