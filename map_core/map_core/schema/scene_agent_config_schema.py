from pydantic import BaseModel, Field, model_validator

from ..config.config_schema import LLMConfig
from ..service.agent.tool_registry import (
    find_invalid_tool_names,
    list_registered_tool_names,
)


class BaseScenePostSummaryConfig(BaseModel):
    enabled: bool = False
    system_prompt: str | None = None
    user_prompt_template: str | None = None
    llm_config: LLMConfig | None = None


class BaseSceneAgentConfig(BaseModel):
    prompt: str
    additional_user_prompt: str = ""
    tool_names: list[str] = Field(default_factory=list)
    max_steps: int = 6
    description: str = ""
    force_tool_call: bool = False
    stop_on_no_tool_call: bool = True  # compatiable
    llm_config: LLMConfig | None = None


class ScenePostSummaryConfig(BaseScenePostSummaryConfig):
    pass


class ScenePostSummaryConfigSchema(BaseScenePostSummaryConfig):
    """Configuration for post-summary generation after scene agent finishes execution."""


class SceneAgentConfig(BaseSceneAgentConfig):
    scene_post_summary: ScenePostSummaryConfig | None = None


class SceneAgentConfigSchema(BaseSceneAgentConfig):
    """Configuration for a single scene agent."""

    scene_post_summary: ScenePostSummaryConfigSchema | None = None

    @model_validator(mode="after")
    def validate_tool_names(self) -> "SceneAgentConfigSchema":
        invalid_tool_names = find_invalid_tool_names(self.tool_names)
        if not invalid_tool_names:
            return self

        allowed = ", ".join(list_registered_tool_names())
        invalid_values = ", ".join(invalid_tool_names)
        raise ValueError(
            "scene agent config contains unknown tool_names: "
            f"{invalid_values}. Allowed values: {allowed}"
        )
