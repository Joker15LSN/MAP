"""AgentScope 2.x adaptation layer for MAP.

Ports the hgt-2 validated adapter modules (model / tool / agent /
offloader) onto MAP's existing ReAct runtime components
(ToolExecutor / ToolCallSession / ToolCallExitHandler / TraceableAgent),
keeping the MAP SSE action-event contract and Mongo state events intact.

Note: the artifact offloader module is EXPERIMENTAL and not wired into
production (no call site injects an artifact store); see ``offloader.py``.
"""

from .agent import AgentScopeSceneAgent
from .model import MapChatModelAdapter
from .tool import MapToolAdapter, extract_result_for_llm_context

__all__ = [
    "AgentScopeSceneAgent",
    "MapChatModelAdapter",
    "MapToolAdapter",
    "extract_result_for_llm_context",
]
