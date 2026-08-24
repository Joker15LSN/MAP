"""attachment_file_read_tool - DISABLED (P0-SEC-01, review R-02).

Read files from request attachments.
The tool name stays *known* for legacy scene configs; any execution attempt
returns the stable CAPABILITY_DISABLED result and no file, network or
process access happens on the host.
"""

from __future__ import annotations

from typing import Any

from .base import ExecutionResult
from .disabled_capabilities import (
    CAPABILITY_DISABLED_CODE,
    CAPABILITY_DISABLED_ERROR,
    CAPABILITY_DISABLED_REASON,
)
from .tool_call_agent import Tool

TOOL_NAME = "attachment_file_read_tool"


def create_attachment_file_read_tool() -> Tool:
    async def _handler(
        args: dict[str, Any], request: Any, _parid: str
    ) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            error=f"{CAPABILITY_DISABLED_ERROR}: {CAPABILITY_DISABLED_REASON}",
            data_source={"code": CAPABILITY_DISABLED_CODE, "tool": TOOL_NAME},
        )

    return Tool(
        name=TOOL_NAME,
        description=(
            "Read files from request attachments. DISABLED: file access is "
            "unavailable until it is served by the OpenSandbox Server and "
            "the private artifact store."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file_id whitelist.",
                },
            },
            "additionalProperties": False,
        },
        handler=_handler,
    )
