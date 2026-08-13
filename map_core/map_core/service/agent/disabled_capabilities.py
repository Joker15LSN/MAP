"""P0-SEC-01: fail-closed registry for disabled host-execution capabilities.

In-process host execution (``python_exec_tool``, ``bash_tool``) is removed
from the production tool registry until execution is served by the
OpenSandbox Server. These names stay *known* so legacy scene configs that
reference them keep validating, but any execution attempt resolves to a
stable ``CAPABILITY_DISABLED`` result instead of silently running on the
host or failing with an unstable "not found" error.

This module must stay dependency-free: it is imported by both
``tool_registry`` and ``tool_executor`` and must not create import cycles.
"""

from __future__ import annotations

DISABLED_HOST_EXEC_CAPABILITIES: frozenset[str] = frozenset(
    {"python_exec_tool", "bash_tool"}
)

CAPABILITY_DISABLED_ERROR = "CAPABILITY_DISABLED"
CAPABILITY_DISABLED_CODE = "capability_disabled"
CAPABILITY_DISABLED_REASON = (
    "In-process host execution is disabled by policy. "
    "This capability is unavailable until it is served by the OpenSandbox Server."
)


def is_disabled_capability(tool_name: str) -> bool:
    return tool_name in DISABLED_HOST_EXEC_CAPABILITIES


def build_capability_disabled_result(tool_name: str) -> dict[str, str]:
    """Stable fail-closed result for explicitly disabled capabilities."""
    return {
        "error": CAPABILITY_DISABLED_ERROR,
        "code": CAPABILITY_DISABLED_CODE,
        "tool": tool_name,
        "reason": CAPABILITY_DISABLED_REASON,
    }
