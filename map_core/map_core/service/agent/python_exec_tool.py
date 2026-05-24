from __future__ import annotations

import asyncio
import io
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..state_store import safe_serialize
from .tool_call_agent import Tool

TOOL_NAME = "python_exec_tool"


class PythonExecToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=10, gt=0, le=120)

    @field_validator("code")
    @classmethod
    def _validate_code_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("`code` must be a non-empty string.")
        return value


class PythonExecThreadInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str


class PythonExecToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    stdout: str = ""
    result: Any = None
    error: str | list[dict[str, Any]] | None = None
    thread: PythonExecThreadInfo | None = None


def create_python_exec_tool() -> Tool:
    async def _handler(
        args: dict[str, Any], _request: Any, _parid: str
    ) -> dict[str, Any]:
        try:
            payload = PythonExecToolInput.model_validate(args)
        except ValidationError as exc:
            return {
                "success": False,
                "error": exc.errors(),
            }

        code = payload.code
        timeout_seconds = payload.timeout_seconds

        def _run_sync() -> PythonExecToolOutput:
            stdout_buffer = io.StringIO()
            global_namespace: dict[str, Any] = {"__builtins__": __builtins__}
            local_namespace: dict[str, Any] = {}

            try:
                with redirect_stdout(stdout_buffer):
                    exec(
                        compile(code, "<python_exec_tool>", "exec"),
                        global_namespace,
                        local_namespace,
                    )

                return PythonExecToolOutput(
                    success=True,
                    stdout=stdout_buffer.getvalue(),
                    result=safe_serialize(local_namespace.get("result")),
                    thread=PythonExecThreadInfo(
                        id=threading.get_ident(),
                        name=threading.current_thread().name,
                    ),
                )
            except Exception:
                return PythonExecToolOutput(
                    success=False,
                    stdout=stdout_buffer.getvalue(),
                    error=traceback.format_exc(),
                    thread=PythonExecThreadInfo(
                        id=threading.get_ident(),
                        name=threading.current_thread().name,
                    ),
                )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_sync), timeout=timeout_seconds
            )
            return result.model_dump()
        except asyncio.TimeoutError:
            return PythonExecToolOutput(
                success=False,
                error=f"Execution timed out after {timeout_seconds} seconds.",
            ).model_dump()

    return Tool(
        name=TOOL_NAME,
        description="Execute Python code in a separate worker thread. Network access available.",
        parameters=PythonExecToolInput.model_json_schema(),
        handler=_handler,
    )
