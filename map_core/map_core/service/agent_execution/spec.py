from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from ...config.config_schema import LLMConfig
from ...schema.scene_agent_config_schema import ScenePostSummaryConfig

ENGINE_ENV_VAR = "MAP_AGENT_ENGINE"
VALID_ENGINES = ("legacy", "agentscope")
DEFAULT_ENGINE = "agentscope"


class AgentExecutionSpec(BaseModel):
    """Execution input for one agent run.

    This is the public caller-facing contract. The engine is intentionally
    NOT part of this model: engine selection is a composition-root concern
    (``AgentRuntime`` / ``MAP_AGENT_ENGINE``), not caller knowledge.
    """

    name: str
    system_prompt: str
    additional_user_prompt: str = ""
    tool_names: list[str] = Field(default_factory=list)
    max_steps: int = 6
    force_tool_call: bool = False
    llm_config: LLMConfig | None = None
    scene_post_summary: ScenePostSummaryConfig | None = None
    agent_name: str | None = None


def resolve_agent_engine(
    requested: Literal["legacy", "agentscope"] | None,
) -> str:
    """Resolve the execution engine: request-level > env var > agentscope.

    The default engine of the new public module is AgentScope. Legacy is
    only reachable through an explicit rollback switch at the composition
    root (``engine="legacy"``) or through ``MAP_AGENT_ENGINE=legacy``.
    """
    if requested in VALID_ENGINES:
        return requested
    env_value = os.getenv(ENGINE_ENV_VAR, "").strip().lower()
    if env_value in VALID_ENGINES:
        return env_value
    return DEFAULT_ENGINE
