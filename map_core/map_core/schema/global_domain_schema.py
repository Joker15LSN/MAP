import warnings
from collections.abc import Sequence
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config.config_schema import LLMConfig
from ..service.agent.base import AgentResult
from ..service.agent.tool_registry import (
    find_invalid_tool_agent_names,
    list_registered_tool_agent_names,
)
from ..utils.term_replacer import GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE
from .agent_schema import Message
from .attachment_schema import AttachmentSchema, UploadedKBFileSchema
from .rerank_model_schema import (
    RerankModelConfigSchema,
    create_default_rerank_model_conf,
)
from .scene_agent_config_schema import SceneAgentConfigSchema
from .scene_classification_schema import SceneClassificationResult
from .scene_registry import SUB_SCENES, SceneRegistrySchema
from .tool_extra_result_schema import ToolExtraResultSchema


class AgentDispatchConfigSchema(BaseModel):
    """Configuration for dispatching agents based on scene classification."""

    scene_agent_configs: dict[str, SceneAgentConfigSchema] | None = None

    @model_validator(mode="after")
    def validate_scene_agent_configs_keys(self) -> "AgentDispatchConfigSchema":
        if self.scene_agent_configs is None:
            return self

        allowed = set(SUB_SCENES)
        unknown = [name for name in self.scene_agent_configs if name not in allowed]
        if unknown:
            raise ValueError(
                "unknown agent_code in scene_agent_configs: " + ", ".join(unknown)
            )
        return self


class AgentDispatchConfigRefsSchema(BaseModel):
    """Dispatch config schema for stream-v3 mode."""

    model_config = ConfigDict(extra="forbid")


class EnabledAgentConfigSchema(BaseModel):
    """Display and selection metadata for an enabled scene agent."""

    agent_name: str
    agent_description: str

    @field_validator("agent_name", "agent_description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("enabled_agent_codes metadata cannot be empty")
        return normalized


def _normalize_enabled_agent_codes_payload(
    value: Any,
) -> dict[str, dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("enabled_agent_codes must be a dict")

    normalized: dict[str, dict[str, str]] = {}
    for raw_code, raw_config in value.items():
        cleaned_code = str(raw_code).strip()
        if not cleaned_code:
            raise ValueError("enabled_agent_codes cannot contain empty keys")

        if isinstance(raw_config, EnabledAgentConfigSchema):
            normalized[cleaned_code] = raw_config.model_dump()
            continue

        if not isinstance(raw_config, dict):
            raise ValueError(
                f"enabled_agent_codes[{cleaned_code!r}] must be an object"
            )

        raw_agent_name = raw_config.get("agent_name")
        raw_agent_description = raw_config.get("agent_description")
        agent_name = raw_agent_name.strip() if isinstance(raw_agent_name, str) else ""
        agent_description = (
            raw_agent_description.strip()
            if isinstance(raw_agent_description, str)
            else ""
        )
        if not agent_name:
            raise ValueError(
                f"enabled_agent_codes[{cleaned_code!r}].agent_name cannot be empty"
            )
        if not agent_description:
            raise ValueError(
                f"enabled_agent_codes[{cleaned_code!r}].agent_description cannot be empty"
            )
        normalized[cleaned_code] = {
            "agent_name": agent_name,
            "agent_description": agent_description,
        }
    return normalized


class SceneSelectionConfigSchema(BaseModel):
    """Runtime configuration for two-level scene selection."""

    scene_registry: SceneRegistrySchema | None = None
    big_scene_system_prompt_template: str | None = None
    sub_scene_user_prompt_template: str | None = None
    enabled_agent_codes: dict[str, EnabledAgentConfigSchema] | None = None

    @field_validator("big_scene_system_prompt_template")
    @classmethod
    def validate_big_scene_template(cls, value: str | None) -> str | None:
        if value is None:
            return value
        template = value.strip()
        if not template:
            raise ValueError("big_scene_system_prompt_template cannot be empty")
        if "{scene_catalog_text}" not in template:
            raise ValueError(
                "big_scene_system_prompt_template must contain '{scene_catalog_text}' placeholder"
            )
        return template

    @field_validator("sub_scene_user_prompt_template")
    @classmethod
    def validate_sub_scene_template(cls, value: str | None) -> str | None:
        if value is None:
            return value
        template = value.strip()
        if not template:
            raise ValueError("sub_scene_user_prompt_template cannot be empty")
        required_placeholders = (
            "{query}",
            "{big_scene}",
            "{sub_scene_descriptions}",
        )
        missing = [
            placeholder
            for placeholder in required_placeholders
            if placeholder not in template
        ]
        if missing:
            raise ValueError(
                "sub_scene_user_prompt_template missing placeholders: "
                + ", ".join(missing)
            )
        return template

    @field_validator("enabled_agent_codes", mode="before")
    @classmethod
    def validate_enabled_agent_codes(
        cls, value: Any
    ) -> dict[str, dict[str, str]] | None:
        return _normalize_enabled_agent_codes_payload(value)


class SummarizeConfigSchema(BaseModel):
    """Runtime configuration for the final global summarize step."""

    system_prompt: str | None = None
    user_prompt_template: str | None = None
    llm_config: LLMConfig | None = None

    @field_validator("system_prompt", "user_prompt_template")
    @classmethod
    def validate_non_empty_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return value
        template = value.strip()
        if not template:
            raise ValueError("summarize prompt config cannot be empty")
        return template


class QueryTermReplacementPairSchema(BaseModel):
    """One literal query-term replacement rule."""

    source: list[str]
    target: str

    @field_validator("source", mode="before")
    @classmethod
    def validate_source(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("term replacement source must be a list")

        normalized: list[str] = []
        seen: set[str] = set()
        for raw_source in value:
            if not isinstance(raw_source, str):
                raise ValueError("term replacement source items must be strings")
            source = raw_source.strip()
            if not source:
                raise ValueError("term replacement source cannot contain empty items")
            if source in seen:
                raise ValueError(f"duplicate term replacement source: {source}")
            normalized.append(source)
            seen.add(source)
        if not normalized:
            raise ValueError("term replacement source cannot be empty")
        return normalized


class AgentTermReplacementSchema(BaseModel):
    """Query-term replacement config scoped to one or more scene agents."""

    agent_code: list[str]
    replacements: list[QueryTermReplacementPairSchema] | None = None
    protected_terms: list[str] | None = None
    translations: list[QueryTermReplacementPairSchema] | None = None
    enable_translations: bool = False

    @staticmethod
    def _dedupe_replacement_pairs(
        value: Any,
        *,
        agent_code: Any,
        field_name: str,
    ) -> Any:
        if not isinstance(value, list):
            return value

        seen_sources: set[str] = set()
        duplicate_sources: list[str] = []
        normalized_pairs: list[Any] = []
        for raw_pair in value:
            if isinstance(raw_pair, QueryTermReplacementPairSchema):
                raw_pair = raw_pair.model_dump()
            if not isinstance(raw_pair, dict):
                normalized_pairs.append(raw_pair)
                continue

            raw_sources = raw_pair.get("source")
            if not isinstance(raw_sources, list):
                normalized_pairs.append(raw_pair)
                continue

            normalized_sources: list[Any] = []
            for raw_source in raw_sources:
                source = raw_source.strip() if isinstance(raw_source, str) else None
                if source is not None and source in seen_sources:
                    duplicate_sources.append(source)
                    continue
                if source is not None:
                    seen_sources.add(source)
                normalized_sources.append(raw_source)

            if not normalized_sources:
                continue
            normalized_pair = dict(raw_pair)
            normalized_pair["source"] = normalized_sources
            normalized_pairs.append(normalized_pair)

        if duplicate_sources:
            logger.warning(
                "term_replacements contains duplicate sources. "
                "agent_code={}, field={}, duplicate_count={}, "
                "duplicate_sources={}. Keeping first occurrence.",
                agent_code,
                field_name,
                len(duplicate_sources),
                sorted(set(duplicate_sources))[:20],
            )

        return normalized_pairs

    @field_validator("replacements", "translations", mode="before")
    @classmethod
    def normalize_duplicate_sources(cls, value: Any, info: Any) -> Any:
        return cls._dedupe_replacement_pairs(
            value,
            agent_code=info.data.get("agent_code"),
            field_name=info.field_name,
        )

    @field_validator("agent_code", mode="before")
    @classmethod
    def validate_agent_code(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            warnings.warn(
                "term_replacements.agent_code string input is deprecated and ignored; "
                "use a list of agent codes instead",
                UserWarning,
                stacklevel=2,
            )
            return []
        elif isinstance(value, list):
            raw_agent_codes = value
        else:
            raise ValueError("term_replacements.agent_code must be a string or list")

        normalized: list[str] = []
        seen: set[str] = set()
        for raw_agent_code in raw_agent_codes:
            if not isinstance(raw_agent_code, str):
                raise ValueError("term_replacements.agent_code items must be strings")
            agent_code = raw_agent_code.strip()
            if agent_code.upper() == GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE:
                agent_code = GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE
            if not agent_code:
                raise ValueError("term_replacements.agent_code cannot be empty")
            if (
                agent_code != GLOBAL_DOMAIN_TERM_REPLACEMENT_AGENT_CODE
                and agent_code not in SUB_SCENES
            ):
                raise ValueError(
                    f"unknown agent_code in term_replacements: {agent_code}"
                )
            if agent_code in seen:
                raise ValueError(
                    f"duplicate agent_code in term_replacements: {agent_code}"
                )
            normalized.append(agent_code)
            seen.add(agent_code)

        if not normalized:
            raise ValueError("term_replacements.agent_code cannot be empty")
        return normalized

    @field_validator("protected_terms", mode="before")
    @classmethod
    def validate_protected_terms(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("protected_terms must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_term in value:
            if not isinstance(raw_term, str):
                raise ValueError("protected_terms items must be strings")
            term = raw_term.strip()
            if not term:
                raise ValueError("protected_terms cannot contain empty items")
            if term in seen:
                raise ValueError(f"duplicate protected term: {term}")
            normalized.append(term)
            seen.add(term)
        return normalized

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "AgentTermReplacementSchema":
        for field_name, pairs in [
            ("replacements", self.replacements),
            ("translations", self.translations),
        ]:
            if not pairs:
                continue
            seen: set[str] = set()
            duplicates: list[str] = []
            for pair in pairs:
                for source in pair.source:
                    if source in seen:
                        duplicates.append(source)
                    seen.add(source)
            if duplicates:
                raise ValueError(
                    f"{field_name} contains duplicate source values: "
                    + ", ".join(duplicates)
                )
        return self


class GlobalDomainChatBaseSchema(BaseModel):
    """Shared request fields for global-domain chat endpoints."""

    query: str
    quote: str | None = None
    original_query: str | None = None
    staff_code: str = "missing"
    backend_env: str = "EDITORIAL_STATE"
    backend_env_base_url: str = "missing"
    attachments: list[AttachmentSchema] | None = None
    uploaded_kb_files: list[UploadedKBFileSchema] | None = None
    rerank_model_config: RerankModelConfigSchema = Field(default_factory=create_default_rerank_model_conf)
    history: Sequence[Message | dict[str, Any]] | None = None
    query_rewrite_enabled: bool = False
    query_term_replacer_enabled: bool = True
    term_replacements: list[AgentTermReplacementSchema] | None = None
    scene_selection: SceneSelectionConfigSchema | None = None
    summarize_config: SummarizeConfigSchema | None = None
    chart_plotting_enabled: bool = True
    content_review_enabled: bool = False
    content_review_company_policy_instruction: str = "包含褚健的所有信息都需要过滤/屏蔽"

    @model_validator(mode="after")
    def validate_term_replacements(self) -> "GlobalDomainChatBaseSchema":
        return self


class GlobalDomainChatSchema(GlobalDomainChatBaseSchema):
    """Schema for a global domain chat request."""

    tool_context: dict[str, Any] | None = None
    dispatch_config: AgentDispatchConfigSchema | None = None


class GlobalDomainChatV3Schema(GlobalDomainChatBaseSchema):
    """Schema for stream-v3 refs mode request."""

    model_config = ConfigDict(extra="forbid")

    # tool_context: dict[str, Any]
    # dispatch_config: AgentDispatchConfigRefsSchema


class DebugSelectSceneRequestSchema(BaseModel):
    """Schema for /debug/select_scene with explicit scene filter inputs."""

    query: str
    staff_code: str = "missing"
    history: Sequence[Message | dict[str, Any]] | None = None
    scene_selection: SceneSelectionConfigSchema | None = None
    enabled_agent_codes: dict[str, EnabledAgentConfigSchema] | None = None

    @field_validator("enabled_agent_codes", mode="before")
    @classmethod
    def validate_enabled_agent_codes(
        cls, value: Any
    ) -> dict[str, dict[str, str]] | None:
        return _normalize_enabled_agent_codes_payload(value)

    def to_chat_request(self) -> GlobalDomainChatSchema:
        merged: dict[str, EnabledAgentConfigSchema] = {}
        from_scene_selection = (
            self.scene_selection.enabled_agent_codes
            if self.scene_selection is not None
            else None
        )
        for agent_configs in [
            self.enabled_agent_codes or {},
            from_scene_selection or {},
        ]:
            for code, agent_config in agent_configs.items():
                if code in merged:
                    continue
                merged[code] = agent_config

        resolved_scene_selection = self.scene_selection
        if merged:
            if resolved_scene_selection is None:
                resolved_scene_selection = SceneSelectionConfigSchema(
                    enabled_agent_codes=merged
                )
            else:
                resolved_scene_selection = resolved_scene_selection.model_copy(
                    update={"enabled_agent_codes": merged}
                )

        return GlobalDomainChatSchema(
            query=self.query,
            staff_code=self.staff_code,
            history=self.history,
            scene_selection=resolved_scene_selection,
        )


class GlobalDomainChatResponse(BaseModel):
    """Schema for a global domain chat response."""

    content: str
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None


class SceneAgentDebugRequest(GlobalDomainChatSchema):
    """Schema for a scene agent debug request."""

    agent_code: str
    scene_result: SceneClassificationResult | None = None
    scene_agent_config: SceneAgentConfigSchema | None = None


class SceneAgentDebugResponse(BaseModel):
    """Schema for a scene agent debug response."""

    request_id: str
    state_id: str
    agent_code: str
    result: AgentResult
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None


class ToolAgentDebugRequest(GlobalDomainChatSchema):
    """Schema for directly running a traceable tool agent."""

    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    caller_agent_name: str | None = None
    scene_result: SceneClassificationResult | None = None

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        tool_name = value.strip()
        if not tool_name:
            raise ValueError("tool_name cannot be empty")

        invalid_tool_names = find_invalid_tool_agent_names([tool_name])
        if not invalid_tool_names:
            return tool_name

        allowed = ", ".join(list_registered_tool_agent_names())
        raise ValueError(
            "tool_name must reference a traceable tool agent: "
            f"{tool_name}. Allowed values: {allowed}"
        )


class ToolAgentDebugResponse(BaseModel):
    """Schema for a direct tool-agent debug response."""

    request_id: str
    state_id: str
    tool_name: str
    result: AgentResult
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None


# -------- sse stream --------
class GlobalDomainStreamContext(BaseModel):
    """Schema for a global domain stream context."""

    request_id: str
    state_id: str
    scene_result: SceneClassificationResult
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class GlobalDomainStreamContentDeltaData(BaseModel):
    """Schema for a global domain stream content delta(chunked) data."""

    model_config = {"extra": "allow"}
    content: str


class GlobalDomainStreamMetaData(BaseModel):
    """Schema for a global domain stream meta data."""

    model_config = {"extra": "allow"}


class GlobalDomainStreamDoneData(BaseModel):
    """Last event(chunk) in stream, indicating the agent execution is done. Contains final content and results."""

    content: str
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    request_id: str
    state_id: str


class GlobalDomainStreamErrorData(BaseModel):
    """Schema for a global domain stream error event."""

    error: str
    code: str | None = None
    request_id: str
    state_id: str
    review_result: dict[str, Any] | None = None


class GlobalDomainStreamEvent(BaseModel):
    """Schema for a global domain stream event."""

    event: Literal["start", "content_delta", "meta", "done", "error"]
    data: (
        GlobalDomainStreamContext
        | GlobalDomainStreamContentDeltaData
        | GlobalDomainStreamMetaData
        | GlobalDomainStreamDoneData
        | GlobalDomainStreamErrorData
        | dict[str, Any]
    )

    @model_validator(mode="after")
    def normalize_data_to_dict(self) -> "GlobalDomainStreamEvent":
        if isinstance(self.data, BaseModel):
            self.data = self.data.model_dump()
        return self


# -------- debug: demo --------
class GlobalDomainDemoResponse(BaseModel):
    summary: str
    dispatch_results: list[AgentResult] | None = None
    error: str | None = None


# -------- debug only --------
class SceneClassificationRequest(GlobalDomainChatSchema):
    """Backward-compatible alias for scene-classification debug endpoint."""
