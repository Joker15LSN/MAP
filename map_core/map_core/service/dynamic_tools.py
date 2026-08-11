from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx
from opentelemetry.propagate import inject as otel_inject

from ..utils.llm_engine import LLMEngine
from .agent.base import AgentRequest, ToolResult
from .agent.tool_runtime import Tool


def _slugify(value: str, *, prefix: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value).strip()).strip("-").lower()
    if not normalized:
        normalized = "unknown"
    return normalized if normalized.startswith(f"{prefix}-") else f"{prefix}-{normalized}"


def mcp_tool_runtime_name(server_id: str, tool_name: str) -> str:
    return (
        f"mcp__{_slugify(server_id, prefix='server').replace('-', '_')}"
        f"__{_slugify(tool_name, prefix='tool').replace('-', '_')}"
    )


def skill_runtime_tool_name(skill_id: str) -> str:
    return f"skill__{_slugify(skill_id, prefix='skill').replace('-', '_')}"


async def _call_http_mcp_tool(
    *,
    server: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
) -> ToolResult:
    url = str(server.get("url") or "").strip()
    if not url:
        return ToolResult(
            success=False,
            name=tool_name,
            error="MCP server url is empty.",
        )
    headers = {
        str(key): str(value)
        for key, value in (server.get("headers") or {}).items()
        if value
    }
    # Propagate the current Tool span to the MCP service. Business headers
    # configured by admins win, but W3C propagation fields must always be
    # generated dynamically: a statically configured traceparent would pin
    # the call to a stale/non-existent trace and break end-to-end linking.
    propagation_keys = {"traceparent", "tracestate", "baggage"}
    headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in propagation_keys
    }
    propagation_headers: dict[str, str] = {}
    try:
        otel_inject(propagation_headers)
    except Exception:
        propagation_headers = {}
    headers.update(propagation_headers)
    payload = {
        "jsonrpc": "2.0",
        "id": "map-mcp-tool-call",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    timeout_s = max(5, int(server.get("timeout_s") or 30))
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    if isinstance(data, dict) and data.get("error"):
        return ToolResult(
            success=False,
            name=tool_name,
            error=json.dumps(data.get("error"), ensure_ascii=False),
            data_source={"raw": data},
        )
    result = data.get("result") if isinstance(data, dict) else data
    return ToolResult(
        name=tool_name,
        content=_stringify_mcp_result(result),
        data_source={"source": "mcp", "server_id": server.get("server_id"), "raw": result},
    )


async def _call_stdio_mcp_tool(
    *,
    server: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
) -> ToolResult:
    command = str(server.get("command") or "").strip()
    if not command:
        return ToolResult(
            success=False,
            name=tool_name,
            error="MCP stdio command is empty.",
        )
    env = os.environ.copy()
    for env_name, env_ref in (server.get("env_refs") or {}).items():
        if env_ref in os.environ:
            env[str(env_name)] = os.environ[str(env_ref)]
    proc = await asyncio.create_subprocess_exec(
        command,
        *[str(item) for item in server.get("args") or []],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    request = {
        "jsonrpc": "2.0",
        "id": "map-mcp-tool-call",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await proc.stdin.drain()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=max(5, int(server.get("timeout_s") or 30)),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return ToolResult(
            success=False,
            name=tool_name,
            error="MCP stdio tool call timeout.",
        )
    if proc.returncode not in {0, None} and stderr:
        return ToolResult(
            success=False,
            name=tool_name,
            error=stderr.decode("utf-8", errors="ignore")[:1000],
        )
    text = stdout.decode("utf-8", errors="ignore").strip()
    try:
        parsed = json.loads(text.splitlines()[-1]) if text else {}
    except json.JSONDecodeError:
        parsed = {"content": text}
    result = parsed.get("result") if isinstance(parsed, dict) else parsed
    return ToolResult(
        name=tool_name,
        content=_stringify_mcp_result(result),
        data_source={"source": "mcp", "server_id": server.get("server_id"), "raw": result},
    )


def _stringify_mcp_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(parts)
        if isinstance(content, str):
            return content
    return json.dumps(result, ensure_ascii=False)


def build_mcp_tools(servers: list[dict[str, Any]]) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    for server in servers or []:
        if not server.get("enabled", True):
            continue
        server_id = str(server.get("server_id") or "").strip()
        if not server_id:
            continue
        for tool_config in server.get("tools") or []:
            if not isinstance(tool_config, dict) or not tool_config.get("enabled", True):
                continue
            raw_tool_name = str(tool_config.get("name") or "").strip()
            if not raw_tool_name:
                continue
            runtime_name = mcp_tool_runtime_name(server_id, raw_tool_name)

            async def _handler(
                args: dict[str, Any],
                _request: AgentRequest,
                _parid: str,
                *,
                current_server: dict[str, Any] = dict(server),
                current_tool_name: str = raw_tool_name,
            ) -> ToolResult:
                transport = str(current_server.get("transport") or "stdio")
                if transport in {"sse", "streamable_http"}:
                    return await _call_http_mcp_tool(
                        server=current_server,
                        tool_name=current_tool_name,
                        args=args,
                    )
                return await _call_stdio_mcp_tool(
                    server=current_server,
                    tool_name=current_tool_name,
                    args=args,
                )

            tools[runtime_name] = Tool(
                name=runtime_name,
                description=str(tool_config.get("description") or raw_tool_name),
                parameters=tool_config.get("input_schema")
                if isinstance(tool_config.get("input_schema"), dict)
                else {"type": "object", "properties": {}},
                handler=_handler,
            )
    return tools


def build_prompt_skill_tools(
    *,
    skills: list[dict[str, Any]],
    descriptors: list[dict[str, Any]],
    llm: LLMEngine,
) -> dict[str, Tool]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in skills or []:
        if isinstance(item, dict) and item.get("skill_id"):
            by_id[str(item["skill_id"])] = item
    for item in descriptors or []:
        if not isinstance(item, dict) or item.get("executor_type") != "prompt_skill":
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        if skill_id:
            by_id.setdefault(skill_id, item)

    tools: dict[str, Tool] = {}
    for skill_id, skill in by_id.items():
        if str(skill.get("status") or "active") != "active":
            continue
        runtime_name = str(skill.get("tool_name") or skill_runtime_tool_name(skill_id))
        content = str(skill.get("content") or "").strip()
        description = str(skill.get("description") or skill.get("display_name") or skill_id)

        async def _handler(
            args: dict[str, Any],
            request: AgentRequest,
            _parid: str,
            *,
            current_skill: dict[str, Any] = dict(skill),
            current_content: str = content,
        ) -> ToolResult:
            query = str(args.get("query") or request.query)
            prompt = (
                f"用户问题：{query}\n\n"
                f"调用参数：{json.dumps(args, ensure_ascii=False)}"
            )
            response = await llm.asimple_chat(
                prompt=prompt,
                system_prompt=current_content
                or "你是一个可复用的业务 skill，请根据用户问题和参数完成任务。",
            )
            return ToolResult(
                name=str(current_skill.get("tool_name") or skill_runtime_tool_name(str(current_skill.get("skill_id")))),
                content=response.content.strip(),
                data_source={
                    "source": "prompt_skill",
                    "skill_id": current_skill.get("skill_id"),
                    "usage": response.usage,
                },
            )

        tools[runtime_name] = Tool(
            name=runtime_name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "需要 skill 处理的用户问题或子问题。",
                    }
                },
            },
            handler=_handler,
        )
    return tools
