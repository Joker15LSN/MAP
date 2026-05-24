from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .tool_call_agent import Tool

TOOL_NAME = "bash_tool"

_MAX_OUTPUT_BYTES = 10 * 1024  # 10 KB per stream
_WORKSPACE_ROOT = Path("/tmp/map_bash_workspaces")
_WORKSPACE_TTL = 300.0  # seconds idle before cleanup
_REAP_INTERVAL = 60.0  # reaper wake-up period
_MAX_AS = 256 * 1024 * 1024  # virtual address space per sandbox (256 MB)

_BWRAP_BASE_ARGS: list[str] = [
    "--unshare-user",
    "--unshare-uts",
    "--uid",
    "0",
    "--gid",
    "0",
    "--hostname",
    "sandbox",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--ro-bind",
    "/usr",
    "/usr",
    "--symlink",
    "usr/bin",
    "/bin",
    "--symlink",
    "usr/lib",
    "/lib",
    "--symlink",
    "usr/lib32",
    "/lib32",
    "--symlink",
    "usr/lib64",
    "/lib64",
    "--symlink",
    "usr/sbin",
    "/sbin",
    "--tmpfs",
    "/tmp",
    "--tmpfs",
    "/root",
    "--setenv",
    "HOME",
    "/root",
    "--setenv",
    "PATH",
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "--die-with-parent",
]

_OPTIONAL_RO_MOUNTS: list[str] = [
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/ca-certificates",
]


def _base_bwrap_args() -> list[str]:
    """bwrap args without --bind workspace and without the executable."""
    args = ["bwrap", *_BWRAP_BASE_ARGS]
    for path in _OPTIONAL_RO_MOUNTS:
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    return args


def _truncate(data: bytes, limit: int) -> str:
    text = data.decode(errors="replace")
    if len(data) > limit:
        text = text[:limit] + f"\n... (truncated, {len(data)} bytes total)"
    return text


@dataclass
class _WorkspaceEntry:
    path: Path
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.last_used > _WORKSPACE_TTL


class _WorkspaceManager:
    def __init__(self) -> None:
        self._entries: dict[str, _WorkspaceEntry] = {}
        self._reaper: asyncio.Task[None] | None = None

    def get_or_create(self, session_id: str) -> Path:
        if session_id in self._entries:
            self._entries[session_id].touch()
            return self._entries[session_id].path

        path = _WORKSPACE_ROOT / session_id
        path.mkdir(parents=True, exist_ok=True)
        self._entries[session_id] = _WorkspaceEntry(path=path)
        logger.debug(f"[bash_tool] Created workspace: {path}")
        self._ensure_reaper()
        return path

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            try:
                loop = asyncio.get_running_loop()
                self._reaper = loop.create_task(
                    self._reap_loop(), name="bash_tool_workspace_reaper"
                )
            except RuntimeError:
                pass  # no running loop (sync context / tests)

    async def _reap_loop(self) -> None:
        while self._entries:
            await asyncio.sleep(_REAP_INTERVAL)
            self._reap_expired()

    def _reap_expired(self) -> None:
        expired = [sid for sid, e in self._entries.items() if e.expired]
        for sid in expired:
            entry = self._entries.pop(sid)
            shutil.rmtree(entry.path, ignore_errors=True)
            logger.info(f"[bash_tool] Removed workspace: {entry.path}")


_workspace_manager = _WorkspaceManager()


class BashToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., min_length=1, description="Bash script to execute")
    timeout_seconds: float = Field(default=10.0, gt=0, le=60.0)

    @field_validator("command")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("`command` must be a non-empty string.")
        return v


class BashToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    error: str | None = None


def create_bash_tool() -> Tool:
    # if not shutil.which("bwrap"):
    #     raise RuntimeError(
    #         "bwrap not found. Install it with: sudo apt install bubblewrap"
    #     )
    # if not shutil.which("prlimit"):
    #     raise RuntimeError(
    #         "prlimit not found. Install it with: sudo apt install util-linux"
    #     )

    base_args = _base_bwrap_args()

    async def _handler(
        args: dict[str, Any], _request: Any, parid: str
    ) -> dict[str, Any]:
        try:
            payload = BashToolInput.model_validate(args)
        except ValidationError as exc:
            return BashToolOutput(success=False, error=str(exc.errors())).model_dump()

        workspace = _workspace_manager.get_or_create(parid)
        cmd = [
            "prlimit",
            f"--as={_MAX_AS}",
            f"--cpu={int(payload.timeout_seconds + 2)}",
            *base_args,
            "--bind",
            str(workspace),
            "/workspace",
            "--setenv",
            "WORKSPACE",
            "/workspace",
            "/usr/bin/bash",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return BashToolOutput(
                success=False, error=f"Failed to start sandbox: {exc}"
            ).model_dump()

        try:
            raw_stdout, raw_stderr = await asyncio.wait_for(
                proc.communicate(payload.command.encode()),
                timeout=payload.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return BashToolOutput(
                success=False,
                error=f"Execution timed out after {payload.timeout_seconds}s.",
            ).model_dump()

        exit_code = proc.returncode or 0
        return BashToolOutput(
            success=exit_code == 0,
            stdout=_truncate(raw_stdout, _MAX_OUTPUT_BYTES),
            stderr=_truncate(raw_stderr, _MAX_OUTPUT_BYTES),
            exit_code=exit_code,
        ).model_dump()

    return Tool(
        name=TOOL_NAME,
        description=(
            "Execute a bash script in an isolated sandbox (bwrap). "
            "Files written to /workspace persist across tool calls within the same agent session. "
            "Network access is available. All other filesystem changes are discarded after execution. "
            "Use for shell commands, file processing, or multi-step scripting tasks."
        ),
        parameters=BashToolInput.model_json_schema(),
        handler=_handler,
    )
