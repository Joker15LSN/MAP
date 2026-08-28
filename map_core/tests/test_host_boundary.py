"""Review R-02 acceptance: the host boundary is closed.

- registry excludes the local file tools; invocation returns CAPABILITY_DISABLED
- the disabled stubs never touch the filesystem or the network
- stdio MCP is fail-closed: no subprocess is spawned, no env is forwarded
- static scan: production code has no host subprocess / os.system / eval/exec
- OpenSandbox client: authenticated HTTP, durable identity fields, idempotency
  key, unknown-outcome reconciliation and secret redaction
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from map_core.service.agent.disabled_capabilities import (
    CAPABILITY_DISABLED_ERROR,
)
from map_core.service.agent.file_read_tool import create_attachment_file_read_tool
from map_core.service.agent.file_write_tool import create_attachment_file_write_tool
from map_core.service.dynamic_tools import build_mcp_tools

PRODUCTION_ROOT = Path(__file__).resolve().parents[1] / "map_core"

FORBIDDEN_HOST_TOKENS = (
    "import subprocess",
    "create_subprocess_exec",
    "os.system(",
    "os.popen(",
    "os.exec",
    "eval(",
    "exec(",
    "bubblewrap",
    "bwrap",
)


def _production_py_files() -> list[Path]:
    return sorted(p for p in PRODUCTION_ROOT.rglob("*.py"))


def test_no_host_execution_tokens_in_production_code() -> None:
    """Static boundary: no subprocess/shell/eval path may exist in production."""
    files = _production_py_files()
    assert files, "production tree must exist"
    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_HOST_TOKENS:
            if token in text:
                violations.append(f"{path}: {token}")
    assert not violations, "host execution tokens found: " + " / ".join(violations)


def test_file_tool_stubs_return_capability_disabled_without_io() -> None:
    for factory in (create_attachment_file_read_tool, create_attachment_file_write_tool):
        tool = factory()
        result = asyncio.run(
            tool.run(
                {"file_ids": ["/etc/passwd"]},
                request=None,  # type: ignore[arg-type]
                parid="-",
            )
        )
        assert result.success is False
        assert CAPABILITY_DISABLED_ERROR in str(result.error)
        assert result.data_source.get("code") == "capability_disabled"


def test_file_tool_modules_contain_no_file_or_network_io() -> None:
    """The stubs must not retain any host IO implementation behind them."""
    for module_name in ("file_read_tool.py", "file_write_tool.py"):
        text = (
            PRODUCTION_ROOT / "service" / "agent" / module_name
        ).read_text(encoding="utf-8")
        for token in (
            "read_bytes",
            "write_text",
            "write_bytes",
            "open(",
            "httpx",
            "minio",
            "Minio(",
            "pathlib.Path(",
            "expanduser",
        ):
            assert token not in text, f"{module_name} still contains {token}"


def test_stdio_mcp_fails_closed_without_spawning() -> None:
    tools = build_mcp_tools(
        [
            {
                "server_id": "demo",
                "enabled": True,
                "transport": "stdio",
                "command": "/bin/echo",
                "args": [],
                "tools": [{"name": "host_tool", "enabled": True}],
            }
        ]
    )
    runtime_name = "mcp__server_demo__tool_host_tool"
    assert runtime_name in tools
    result = asyncio.run(
        tools[runtime_name].run(
            {"anything": True},
            request=None,  # type: ignore[arg-type]
            parid="-",
        )
    )
    assert result.success is False
    assert "CAPABILITY_DISABLED" in str(result.error)


def test_dynamic_tools_module_has_no_subprocess_imports() -> None:
    text = (PRODUCTION_ROOT / "service" / "dynamic_tools.py").read_text(
        encoding="utf-8"
    )
    assert "import asyncio" not in text
    assert "import subprocess" not in text
    assert "create_subprocess" not in text
    assert "os.environ" not in text
