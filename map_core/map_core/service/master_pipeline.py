from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import uuid4

from fastapi import Request
from loguru import logger

from ..config.common import DEEPSEEKV3_LOCAL_CONFIG
from ..schema.attachment_schema import AttachmentSchema
from ..schema.master_pipeline_schema import (
    MasterAgentChatSchema,
    MasterAgentConfigSchema,
    MasterPipelineStreamEvent,
)
from ..schema.tool_extra_result_schema import ToolExtraResultSchema
from ..utils.llm_engine import LLMEngine
from .agent.base import AgentActionEvent, AgentRequest, AgentResult
from .agent.tool_call_agent import Tool
from .agent.tool_registry import build_tool_registry
from .agent_runtime import AgentExecutionSpec, AgentRuntime
from .attachment_collector import AttachmentCollector
from .global_domain_helpers import (
    normalize_attachment_results,
    normalize_tool_extra_results,
    serialize_attachment_results,
    serialize_tool_extra_results,
    stream_event_data_as_dict,
)
from .state_store import GlobalAgentStateStore, fire_and_forget
from .tool_extra_result_collector import ToolExtraResultCollector


class MasterPipeline:
    MASTER_AGENT_CODE = "master_agent"

    def __init__(
        self,
        llm: LLMEngine | None = None,
        request: MasterAgentChatSchema | None = None,
        http_request: Request | None = None,
        staff_code: str | None = None,
        tool_registry: dict[str, Tool] | None = None,
    ) -> None:
        self.staff_code = (
            getattr(request, "staff_code", "missing")
            if request
            else staff_code or "missing"
        )
        self.request_id: str = (
            getattr(http_request.state, "request_id", None) if http_request else None
        ) or "missing"
        self.session_id: str | None = (
            getattr(http_request.state, "session_id", None) if http_request else None
        )
        raw_request_token = (
            getattr(http_request.state, "request_token", None) if http_request else None
        )
        self.request_token: str | None = (
            raw_request_token.strip()
            if isinstance(raw_request_token, str) and raw_request_token.strip()
            else None
        )
        self.x_userid: str = (
            getattr(http_request.state, "x_userid", "missing")
            if http_request
            else "missing"
        )
        self.x_username: str = (
            getattr(http_request.state, "x_username", "missing")
            if http_request
            else "missing"
        )
        self.llm = llm or LLMEngine(config=DEEPSEEKV3_LOCAL_CONFIG)
        self.tool_registry = tool_registry or cast(
            dict[str, Tool], build_tool_registry(self.llm)
        )
        self.agent_runtime = AgentRuntime(
            llm=self.llm,
            tool_registry=self.tool_registry,
        )
        self.attachment_collector = AttachmentCollector()
        self.tool_extra_result_collector = ToolExtraResultCollector()
        self.state_id = str(uuid4())
        self.state_store = GlobalAgentStateStore.instance()
        self.agent_runtime.set_execution_context(self.state_store, self.state_id)
        self.base_state = {
            "_id": self.state_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "staff_code": self.staff_code,
            "meta": {},
            "agent_code": "MasterPipeline",
            "agent_name": "MasterPipeline",
        }

    @staticmethod
    def _resolve_original_query(request: MasterAgentChatSchema) -> str:
        original_query = getattr(request, "original_query", None)
        if isinstance(original_query, str) and original_query:
            return original_query
        return request.query

    def _build_master_spec(
        self,
        request: MasterAgentChatSchema,
    ) -> AgentExecutionSpec:
        config = request.master_config or MasterAgentConfigSchema()
        return AgentExecutionSpec(
            name=self.MASTER_AGENT_CODE,
            system_prompt=config.prompt,
            additional_user_prompt=config.additional_user_prompt,
            tool_names=list(config.tool_names),
            max_steps=config.max_steps,
            force_tool_call=config.force_tool_call,
            llm_config=config.llm_config,
            agent_name=self.MASTER_AGENT_CODE,
        )

    def _build_agent_extra(self, request: MasterAgentChatSchema) -> dict[str, Any]:
        original_query = self._resolve_original_query(request)
        extra = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "staff_code": self.staff_code,
            "original_query": original_query,
            "backend_env": request.backend_env,
            "backend_env_base_url": request.backend_env_base_url,
            "request_token": self.request_token,
            "x_userid": self.x_userid,
            "x_username": self.x_username,
            "attachment_collector": self.attachment_collector,
            "tool_extra_result_collector": self.tool_extra_result_collector,
            "rerank_model_config": request.rerank_model_config.model_dump(),
        }
        if isinstance(request.attachments, list):
            extra["attachments"] = [item.model_dump() for item in request.attachments]
        if request.uploaded_kb_files:
            extra["uploaded_kb_files"] = [
                item.model_dump() for item in request.uploaded_kb_files
            ]
        if isinstance(request.tool_context, dict):
            extra["tool_context"] = request.tool_context
        return extra

    def _build_agent_request(self, request: MasterAgentChatSchema) -> AgentRequest:
        return AgentRequest(
            query=request.query,
            original_query=self._resolve_original_query(request),
            staff_code=self.staff_code,
            history=request.history,
            extra=self._build_agent_extra(request),
            state_store=self.state_store,
            state_id=self.state_id,
        )

    @staticmethod
    def _build_action_meta(action_event: AgentActionEvent) -> dict[str, Any]:
        return {
            "agent_code": action_event.agent_code,
            "agent_name": action_event.agent_name or action_event.agent_code,
            "step": action_event.step,
            "action": action_event.action,
            "message": action_event.message,
            "payload": action_event.payload,
        }

    @staticmethod
    def _build_result_meta(result: AgentResult) -> dict[str, Any]:
        meta_data = dict(result.meta_data or {})
        return {
            "agent_code": meta_data.get("agent_code", result.name),
            "agent_name": meta_data.get("agent_name", result.name),
            "duration_s": meta_data.get("duration_s"),
            "success": result.success,
            "error": result.error,
            "token_usage": meta_data.get("token_usage"),
        }

    async def pipeline_stream(
        self,
        request: MasterAgentChatSchema,
    ) -> AsyncGenerator[MasterPipelineStreamEvent, None]:
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="request.start",
                payload={
                    "request_id": self.request_id,
                    "session_id": self.session_id,
                    "staff_code": self.staff_code,
                    "query": request.query,
                    "original_query": self._resolve_original_query(request),
                },
                base_state=self.base_state,
            )
        )

        yield MasterPipelineStreamEvent(
            event="start",
            data={
                "request_id": self.request_id,
                "state_id": self.state_id,
            },
        )

        try:
            spec = self._build_master_spec(request)
            agent = self.agent_runtime.build_agent(spec)
            agent_request = self._build_agent_request(request)

            yield MasterPipelineStreamEvent(
                event="meta",
                data={
                    "phase": "master_agent_ready",
                    "agent": {
                        "agent_code": spec.name,
                        "agent_name": spec.agent_name or spec.name,
                        "tool_names": spec.tool_names,
                        "max_steps": spec.max_steps,
                    },
                },
            )

            result: AgentResult | None = None
            async for item in self.agent_runtime.run_stream([agent], agent_request):
                if isinstance(item, AgentActionEvent):
                    yield MasterPipelineStreamEvent(
                        event="meta",
                        data={
                            "phase": "agent_action",
                            "agent": self._build_action_meta(item),
                        },
                    )
                    continue
                result = item
                result_meta = self._build_result_meta(result)
                yield MasterPipelineStreamEvent(
                    event="meta",
                    data={
                        "phase": "agent_result",
                        "agent": result_meta,
                    },
                )
                if result.content:
                    yield MasterPipelineStreamEvent(
                        event="content_delta",
                        data={"content": result.content},
                    )

            if result is None:
                raise RuntimeError("master pipeline completed without agent result")

            attachment_results = self.attachment_collector.list_items()
            tool_extra_results = self.tool_extra_result_collector.list_items()
            done_meta = {
                "agent": self._build_result_meta(result),
            }
            yield MasterPipelineStreamEvent(
                event="done",
                data={
                    "content": result.content,
                    "result": result.model_dump(),
                    "attachment_results": serialize_attachment_results(
                        attachment_results
                    ),
                    "tool_extra_results": serialize_tool_extra_results(
                        tool_extra_results
                    ),
                    "meta": done_meta,
                },
            )
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="request.end",
                    payload={
                        "request_id": self.request_id,
                        "state_id": self.state_id,
                        "content": result.content,
                        "success": result.success,
                        "error": result.error,
                    },
                    base_state=self.base_state,
                )
            )
        except Exception as exc:
            logger.exception("Master pipeline failed: {}", exc)
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="request.end",
                    payload={
                        "request_id": self.request_id,
                        "state_id": self.state_id,
                        "success": False,
                        "error": str(exc),
                    },
                    base_state=self.base_state,
                )
            )
            yield MasterPipelineStreamEvent(
                event="error",
                data={"error": str(exc)},
            )

    async def consume_event_stream(self, request: MasterAgentChatSchema) -> dict[str, Any]:
        content_parts: list[str] = []
        result: AgentResult | None = None
        attachment_results: list[AttachmentSchema] | None = None
        tool_extra_results: list[ToolExtraResultSchema] | None = None
        meta: dict[str, Any] = {}
        error_message: str | None = None

        async for event in self.pipeline_stream(request):
            if event.event == "meta":
                data = stream_event_data_as_dict(event)
                if data.get("phase") == "agent_action":
                    continue
                meta.update(data)
                continue
            if event.event == "content_delta":
                data = stream_event_data_as_dict(event)
                content = data.get("content")
                if content:
                    content_parts.append(str(content))
                continue
            if event.event == "done":
                data = stream_event_data_as_dict(event)
                attachment_results = normalize_attachment_results(
                    data.get("attachment_results")
                )
                tool_extra_results = normalize_tool_extra_results(
                    data.get("tool_extra_results")
                )
                raw_result = data.get("result")
                if isinstance(raw_result, dict):
                    result = AgentResult.model_validate(raw_result)
                done_meta = data.get("meta")
                if isinstance(done_meta, dict):
                    meta.update(done_meta)
                return {
                    "content": str(data.get("content") or ""),
                    "result": result,
                    "attachment_results": attachment_results,
                    "tool_extra_results": tool_extra_results,
                    "meta": meta,
                }
            if event.event == "error":
                data = stream_event_data_as_dict(event)
                error_message = str(data.get("error") or "master pipeline failed")

        if error_message:
            raise RuntimeError(error_message)

        return {
            "content": "".join(content_parts),
            "result": result,
            "attachment_results": attachment_results,
            "tool_extra_results": tool_extra_results,
            "meta": meta,
        }
