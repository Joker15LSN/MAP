from __future__ import annotations

import json
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Sequence, cast, overload
from zoneinfo import ZoneInfo

from loguru import logger

from ...config.common import DEEPSEEKV3_LOCAL_CONFIG
from ...config.config_schema import LLMConfig
from ...schema.agent_schema import Message
from ...utils.global_context import agent_log_context
from ...utils.llm_engine import LLMEngine
from ...utils.model_invocation import ModelInvocationRequest
from .base import AgentRequest, AgentResult, BaseAgent


class SummarizeAgent(BaseAgent):
    name = "summarize_agent"
    description = "对多智能体输出进行汇总"
    TOOL_RESULT_MAX_CHARS = 10000
    DEFAULT_STREAM_SYSTEM_PROMPT = (
        "你是智能助手。请基于多智能体的输出结果，生成面向用户的详细总结。"
        "要求：\n"
        "1) 优先说明关键结论与数据；\n"
        "2) 若结果不足以回答，说明原因；\n"
        "3) 中文输出。"
    )
    DEFAULT_SYSTEM_PROMPT = (
        "你是汇总助手。请基于多智能体的输出结果，生成面向用户的简洁总结。"
        "要求：\n"
        "1) 优先抽取关键结论与数据；\n"
        "2) 若结果不足以回答，说明缺失点；\n"
        "3) 3-8 条要点，中文输出。"
    )
    DEFAULT_USER_PROMPT_TEMPLATE = (
        "用户问题：{query}\n历史问答：\n{history_context}"
        "多智能体答复：{dispatch_results_json}"
    )
    PROMPT_TIMEZONE = ZoneInfo("Asia/Shanghai")

    def __init__(self, llm: LLMEngine | None = None) -> None:
        super().__init__(llm or LLMEngine(config=DEEPSEEKV3_LOCAL_CONFIG))

    @classmethod
    def _filter_dispatch_result_for_summary(cls, item: Any) -> dict[str, Any] | None:
        """Keep only the final agent-facing reply needed by summarize.

        Intentionally strip internal loop traces from `data_source`, including
        tool-call history, tool observations, and exit metadata.
        """
        if not hasattr(item, "content") or not hasattr(item, "data_source"):
            return None

        meta_data = (
            item.meta_data if isinstance(getattr(item, "meta_data", None), dict) else {}
        )
        data_source = (
            item.data_source
            if isinstance(getattr(item, "data_source", None), dict)
            else {}
        )
        tool_observations = getattr(item, "tool_observations", None)
        if not isinstance(tool_observations, list):
            tool_observations = None
        content = (
            item.content
            if item.content and isinstance(item.content, str) and item.content.strip()
            else ""
        )
        source = data_source.get("source")
        response_source = source if isinstance(source, str) and source.strip() else None
        if response_source not in {"llm", "scene_post_summary"}:
            response_source = "direct_reply"
        tool_results: list[dict[str, Any]] = []
        tool_results_source = "none"
        content_source = "content" if content else "empty"

        if not content:
            if tool_observations is not None:
                tool_results = cls._extract_tool_results_from_observations(
                    tool_observations
                )
                tool_results_source = "tool_observations"
            else:
                content = cls._serialize_summary_value(data_source)
                if content:
                    content_source = "data_source"
                    tool_results_source = "data_source"
                else:
                    tool_results = cls._extract_tool_results_from_history(data_source)
                    if tool_results:
                        tool_results_source = "history_tool_messages"
            if not content and not tool_results:
                content_source = "empty"
        logger.info(
            "Summarize payload source resolved: agent_code={} content_source={} tool_results_source={} tool_results_count={}",
            getattr(item, "name", None),
            content_source,
            tool_results_source,
            len(tool_results),
        )

        return {
            "agent_code": getattr(item, "name", None),
            "agent_name": meta_data.get("agent_name") or getattr(item, "name", None),
            "content": content,
            "tool_results": tool_results,
            "response_source": response_source,
            "success": getattr(item, "success", None),
            "error": getattr(item, "error", None),
            "exit": getattr(item, "exit", None),
        }
    @classmethod
    def _extract_tool_results_from_observations(
        cls, tool_observations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for observation in tool_observations:
            if not isinstance(observation, dict):
                continue
            content, success = cls._parse_tool_result_payload(
                observation.get("tool_result")
            )
            if not content:
                continue
            results.append(
                {
                    "tool_name": observation.get("tool_name"),
                    "tool_call_id": observation.get("tool_call_id"),
                    "content": content[: cls.TOOL_RESULT_MAX_CHARS],
                    "success": success,
                }
            )
        return results

    @classmethod
    def _extract_tool_results_from_history(
        cls, data_source: dict[str, Any]
    ) -> list[dict[str, Any]]:
        history = data_source.get("history")
        if not isinstance(history, list):
            return []

        tool_name_by_call_id: dict[str, str] = {}
        for message in history:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not isinstance(function, dict):
                    continue
                tool_name = function.get("name")
                if isinstance(tool_name, str) and tool_name.strip():
                    tool_name_by_call_id[call_id] = tool_name

        results: list[dict[str, Any]] = []
        for message in history:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content, success = cls._parse_tool_result_payload(message.get("content"))
            if not content:
                continue
            tool_call_id = message.get("tool_call_id")
            results.append(
                {
                    "tool_name": tool_name_by_call_id.get(tool_call_id)
                    if isinstance(tool_call_id, str)
                    else None,
                    "tool_call_id": tool_call_id,
                    "content": content[: cls.TOOL_RESULT_MAX_CHARS],
                    "success": success,
                }
            )
        return results

    @staticmethod
    def _serialize_summary_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value).strip()

    @staticmethod
    def _parse_tool_result_payload(value: Any) -> tuple[str, bool | None]:
        payload = value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return "", None
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped, None

        if isinstance(payload, dict):
            success = payload.get("success")
            resolved_success = success if isinstance(success, bool) else None
            if resolved_success is False:
                return "", resolved_success

            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip(), resolved_success

            data_source = payload.get("data_source")
            serialized_data_source = SummarizeAgent._serialize_summary_value(data_source)
            if serialized_data_source:
                return serialized_data_source, resolved_success

            serialized_payload = SummarizeAgent._serialize_summary_value(payload)
            if serialized_payload:
                return serialized_payload, resolved_success

        serialized_value = SummarizeAgent._serialize_summary_value(payload)
        if serialized_value:
            return serialized_value, None
        return "", None

    def _process_dispatch_results(self, dispatch_results):
        """Normalize dispatch results into a compact summarize payload.

        For AgentResult:
        - Preserve agent identity so summarize can attribute outputs.
        - Keep only final reply text plus compact source/status fields.
        - Strip internal loop trace payloads from `data_source`.
        For other objects: try `model_dump`, otherwise fall back to `str()`.
        """
        payload = []
        for item in dispatch_results or []:
            filtered = self._filter_dispatch_result_for_summary(item)
            if filtered is not None:
                payload.append(filtered)
            elif hasattr(item, "model_dump") and callable(item.model_dump):
                try:
                    payload.append(item.model_dump())
                except Exception:
                    payload.append(str(item))
            else:
                payload.append(item)
        return payload

    def build_summarize_debug_payload(
        self, request: AgentRequest
    ) -> dict[str, Any]:
        payload = self._process_dispatch_results(request.dispatch_results)
        return {
            "effective_dispatch_result_count": len(payload),
            "effective_dispatch_results": payload,
        }

    @staticmethod
    def _build_history_context(
        history: Sequence[Message | dict[str, Any]] | None,
    ) -> str:
        if not history:
            return ""

        cutoff = 10
        latest_turns = history[-cutoff:] if len(history) >= cutoff else history
        history_lines: list[str] = []
        for msg in latest_turns:
            if isinstance(msg, dict):
                role = msg.get("role")
                content = msg.get("content")
            elif isinstance(msg, Message):
                role = msg.role
                content = msg.content
            else:
                continue

            if role and content:
                history_lines.append(f"{role}: {content}")

        history_text = "\n".join(history_lines)
        if not history_text:
            return ""
        return f"参考历史对话:\n{history_text}\n\n"

    def _build_messages(
        self,
        request: AgentRequest,
        *,
        stream: bool,
    ) -> tuple[str, str]:
        debug_payload = self.build_summarize_debug_payload(request)
        payload = debug_payload["effective_dispatch_results"]
        history_context = self._build_history_context(request.history)
        summarize_config = (
            request.extra.get("summarize_config")
            if isinstance(request.extra, dict)
            else None
        )
        system_prompt = (
            summarize_config.get("system_prompt")
            if isinstance(summarize_config, dict)
            else None
        ) or (
            self.DEFAULT_STREAM_SYSTEM_PROMPT if stream else self.DEFAULT_SYSTEM_PROMPT
        )
        user_prompt_template = (
            summarize_config.get("user_prompt_template")
            if isinstance(summarize_config, dict)
            else None
        ) or self.DEFAULT_USER_PROMPT_TEMPLATE
        current_date = datetime.now(self.PROMPT_TIMEZONE).date().isoformat()
        system_prompt = f"{system_prompt}\n当前日期：{current_date}"
        prompt_payload = {
            "query": request.query,
            "dispatch_results_json": json.dumps(payload, ensure_ascii=False),
            "history_context": history_context,
        }
        try:
            user_content = user_prompt_template.format(**prompt_payload)
        except Exception as exc:
            logger.warning(
                "SummarizeAgent user prompt format failed, using default template: {}",
                exc,
            )
            user_content = self.DEFAULT_USER_PROMPT_TEMPLATE.format(**prompt_payload)
        return system_prompt, user_content

    def _resolve_request_llm_config(self, request: AgentRequest) -> LLMConfig | None:
        summarize_config = (
            request.extra.get("summarize_config")
            if isinstance(request.extra, dict)
            else None
        )
        llm_config = (
            summarize_config.get("llm_config")
            if isinstance(summarize_config, dict)
            else None
        )
        if llm_config is None:
            return None
        if isinstance(llm_config, LLMConfig):
            return llm_config
        return LLMConfig.model_validate(llm_config)

    def _resolve_llm(self, request: AgentRequest) -> LLMEngine:
        llm_config = self._resolve_request_llm_config(request)
        if llm_config is None:
            return self.llm
        return LLMEngine(config=llm_config)

    @overload
    async def run(
        self, request: AgentRequest, *, parid: str = "-", stream: Literal[False] = False
    ) -> AgentResult: ...

    @overload
    async def run(
        self, request: AgentRequest, *, parid: str = "-", stream: Literal[True]
    ) -> AsyncGenerator[str | dict[str, Any], None]: ...

    async def run(
        self, request: AgentRequest, *, parid: str = "-", stream: bool = False
    ) -> AgentResult | AsyncGenerator[str | dict[str, Any], None]:
        if stream:

            async def _stream() -> AsyncGenerator[str | dict[str, Any], None]:
                with agent_log_context(self.agent_id, parent_id=parid):
                    system_prompt, user_content = self._build_messages(
                        request, stream=True
                    )
                    llm = self._resolve_llm(request)

                    try:
                        async for chunk in llm.asimple_chat_stream(
                            prompt=user_content,
                            system_prompt=system_prompt,
                        ):
                            if isinstance(chunk, dict):
                                chunk_type = chunk.get("type")
                                if chunk_type == "usage":
                                    usage_data = chunk.get("data")
                                    if isinstance(usage_data, dict):
                                        typed_usage = cast(dict[str, int], usage_data)
                                        self._accumulate_usage(typed_usage)
                                    continue
                                if chunk_type != "content":
                                    continue
                                if "data" not in chunk:
                                    continue
                                yield chunk
                            else:
                                yield str(chunk)
                    except Exception:
                        logger.exception("SummarizeAgent stream failed")
                        raise

            return _stream()

        with agent_log_context(self.agent_id, parent_id=parid):
            debug_payload = self.build_summarize_debug_payload(request)
            payload = debug_payload["effective_dispatch_results"]
            system_prompt, user_content = self._build_messages(request, stream=False)
            llm = self._resolve_llm(request)

            try:
                outcome = await llm.invoke(
                    ModelInvocationRequest(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ]
                    )
                )
                outcome.raise_for_status()
                self._accumulate_usage(
                    outcome.usage.to_dict() if outcome.usage else None
                )
                return AgentResult(
                    name=self.name,
                    content=outcome.content.strip(),
                    data_source={
                        "source": "dispatch_results",
                        "count": len(payload),
                    },
                )
            except Exception as exc:
                logger.exception("SummarizeAgent run failed")
                return AgentResult(
                    success=False,
                    name=self.name,
                    content="",
                    data_source={},
                    error=str(exc),
                )
