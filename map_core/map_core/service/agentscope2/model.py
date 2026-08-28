from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, FinishedReason
from agentscope.tool import ToolChoice
from pydantic import SecretStr

from ...observability import get_tracer
from ..agent.base import AgentExecutionCancelled
from .message import agentscope_messages_to_openai

_tracer = get_tracer(__name__)


class MapChatModelAdapter(ChatModelBase):
    """Expose the existing MAP LLMEngine through AgentScope's model contract."""

    class Parameters(ChatModelBase.Parameters):
        pass

    def __init__(
        self,
        llm: Any,
        *,
        force_tool_call: bool = False,
        response_handler: Callable[[int, Any], None] | None = None,
    ) -> None:
        self.llm = llm
        self.force_tool_call = force_tool_call
        self.response_handler = response_handler
        self.call_count = 0
        self.last_response: Any | None = None
        self.last_terminate_call: Any | None = None
        self.last_terminate_response: Any | None = None
        self.cancel_event: Any | None = None

        config = getattr(llm, "config", None)
        model_name = str(getattr(config, "model", None) or "map-adapted-model")
        base_url = getattr(config, "base_url", None)
        api_key = str(getattr(config, "api_key", "") or "")
        context_size = int(getattr(config, "context_size", 131_072) or 131_072)
        super().__init__(
            credential=OpenAICredential(
                api_key=SecretStr(api_key),
                base_url=base_url,
            ),
            model=model_name,
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
            context_size=context_size,
        )

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, int]:
        if usage is None:
            return {}
        if hasattr(usage, "model_dump") and callable(usage.model_dump):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    @staticmethod
    def _tool_choice_value(tool_choice: ToolChoice | None) -> Any:
        if tool_choice is None:
            return None
        if tool_choice.mode in {"auto", "none", "required"}:
            return tool_choice.mode
        return {
            "type": "function",
            "function": {"name": tool_choice.mode},
        }

    def _resolve_tool_choice(
        self,
        tool_choice: ToolChoice | None,
        tools: list[dict] | None,
        call_index: int,
    ) -> Any:
        explicit = self._tool_choice_value(tool_choice)
        if explicit is not None:
            return explicit
        if self.force_tool_call and call_index == 0 and tools:
            return "required"
        return "auto"

    async def _call_api(
        self,
        model_name: str,
        messages: list[Any],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        del model_name
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentExecutionCancelled("cancelled")
        is_structured_output_call = any(
            (tool.get("function") or {}).get("name") == "generate_structured_output"
            for tool in (tools or [])
            if isinstance(tool, dict)
        )
        call_index = self.call_count
        if not is_structured_output_call:
            self.call_count += 1
        map_messages = agentscope_messages_to_openai(messages)
        response = await self.llm.ask_tool(
            map_messages,
            tools=tools,
            tool_choice=self._resolve_tool_choice(
                tool_choice,
                tools,
                call_index,
            ),
            **kwargs,
        )
        if not is_structured_output_call:
            self.last_response = response
            self.last_terminate_call = None
            self.last_terminate_response = None
        if self.response_handler is not None and not is_structured_output_call:
            self.response_handler(call_index, response)

        raw_tool_calls: Sequence[Any] = response.tool_calls or []
        terminate_call = next(
            (
                call
                for call in raw_tool_calls
                if getattr(getattr(call, "function", None), "name", None) == "terminate"
            ),
            None,
        )
        if terminate_call is not None and not is_structured_output_call:
            # MAP treats terminate as an exit signal before executing any call
            # in the same model response. AgentScope has no built-in terminate
            # tool, so convert the response to a normal final message.
            self.last_terminate_call = terminate_call
            self.last_terminate_response = response
            raw_tool_calls = []

        blocks: list[Any] = []
        reasoning = getattr(response, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            blocks.append(ThinkingBlock(thinking=reasoning))
        content = getattr(response, "content", None)
        if isinstance(content, str) and content:
            blocks.append(TextBlock(text=content))
        for call in raw_tool_calls:
            function = call.function
            blocks.append(
                ToolCallBlock(
                    id=str(call.id),
                    name=str(function.name),
                    input=str(function.arguments or "{}"),
                )
            )

        usage = self._normalize_usage(getattr(response, "usage", None))
        chat_usage = None
        if usage:
            chat_usage = ChatUsage(
                input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
                output_tokens=usage.get(
                    "completion_tokens", usage.get("output_tokens", 0)
                ),
                time=float(getattr(response, "response_time", 0.0) or 0.0),
            )
        return ChatResponse(
            id=str(getattr(response, "request_id", None) or ""),
            content=blocks,
            is_last=True,
            usage=chat_usage,
            finished_reason=FinishedReason.COMPLETED,
            metadata={
                "map_finish_reason": str(getattr(response, "finish_reason", None) or "")
            },
        )

    async def generate_structured_output(
        self,
        messages: list[Any],
        structured_model: type[Any] | dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        before_tokens = await self.count_tokens(messages, tools=None)
        with _tracer.start_as_current_span(
            "map.context.compress",
            attributes={
                "openinference.span.kind": "CHAIN",
                "gen_ai.request.model": self.model,
                "map.context.before_tokens": before_tokens,
                "map.context.window_tokens": self.context_size,
            },
        ) as span:
            result = await super().generate_structured_output(
                messages=messages,
                structured_model=structured_model,
                **kwargs,
            )
            usage = getattr(result, "usage", None)
            if usage is not None:
                span.set_attribute(
                    "gen_ai.usage.output_tokens",
                    int(getattr(usage, "output_tokens", 0) or 0),
                )
            return result
