"""Deterministic OpenAI-compatible fake LLM for the Compose E2E (R2-P1-05).

Runs as its own container (``fake-llm``) and is the ONLY fake in the E2E
topology: map_core's ``MAP_LLM_BASE_URL`` points here, so every real LLM
call of the real pipeline lands on this server. No map_core code is
replaced.

Routing is schema-driven and stateless (deterministic):

- ``response_format.json_schema`` with a ``big_scenes`` property
  -> big-scene classification JSON picking the first allowed scene;
- ``...`` with ``big_scene`` + ``sub_scenes`` properties
  -> sub-scene classification JSON picking the first allowed agent;
- ``...`` with an ``agent_routes`` property -> master routing JSON;
- requests carrying ``tools`` with ``tool_choice == required``
  -> a tool_call for the first non-terminate tool (agent step 1);
- everything else -> the fixed plain answer (agent final step / summary).

Control endpoints used by the runner:

- ``GET  /health``           -> readiness probe;
- ``POST /__e2e/config``     -> ``{"stream_token_delay_s": float}`` slows
  streaming down so stop/abort can land mid-stream;
- ``POST /__e2e/reset``      -> restore defaults and clear stats;
- ``GET  /__e2e/stats``      -> call counts per kind (for the E2E report).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9999
ANSWER = "这是 MAP 端到端测试的确定性回答。"

_LOCK = threading.Lock()
_STATE: dict = {
    "stream_token_delay_s": 0.0,
    "calls": [],
}


def _record_call(entry: dict) -> None:
    with _LOCK:
        _STATE["calls"].append(entry)
        if len(_STATE["calls"]) > 500:
            _STATE["calls"] = _STATE["calls"][-500:]


def _stream_delay() -> float:
    with _LOCK:
        return float(_STATE.get("stream_token_delay_s") or 0.0)


def _last_user_message(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                ]
                return "".join(parts)
    return ""


def _enum_of(prop: dict) -> list:
    values = prop.get("enum") if isinstance(prop, dict) else None
    return list(values) if isinstance(values, list) else []


def _classification_payload(schema: dict) -> dict | None:
    """Build a valid classification JSON straight from the request schema."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return None

    if "big_scenes" in props:
        # BigSceneClassificationResult: pick the first allowed big scene.
        items = props["big_scenes"].get("items") or {}
        if "$ref" in items and isinstance(schema.get("$defs"), dict):
            ref_name = str(items["$ref"]).rsplit("/", 1)[-1]
            items = schema["$defs"].get(ref_name, {})
        scene_enum = _enum_of((items.get("properties") or {}).get("big_scene") or {})
        if not scene_enum:
            return None
        return {
            "big_scenes": [
                {
                    "big_scene": str(scene_enum[0]),
                    "confidence": 0.95,
                    "reason": "E2E deterministic scene selection.",
                }
            ]
        }

    if "big_scene" in props and "sub_scenes" in props:
        # SubSceneResult: pick the first allowed big scene and agent.
        big_enum = _enum_of(props.get("big_scene") or {})
        sub_items = props.get("sub_scenes", {}).get("items") or {}
        sub_enum = _enum_of(sub_items)
        if not big_enum or not sub_enum:
            return None
        return {
            "big_scene": str(big_enum[0]),
            "sub_scenes": [str(sub_enum[0])],
            "confidence": 0.95,
            "reason": "E2E deterministic agent selection.",
        }

    if "agent_routes" in props:
        items = props["agent_routes"].get("items") or {}
        code_enum = _enum_of((items.get("properties") or {}).get("agent_code") or {})
        routes = (
            [{"agent_code": str(code_enum[0]), "confidence": 0.95, "reason": "E2E."}]
            if code_enum
            else []
        )
        return {"agent_routes": routes}

    return None


def _tool_call_payload(body: dict, user_query: str) -> dict:
    """Emit a tool_call for the first non-terminate tool in the request."""
    tools = body.get("tools") or []
    chosen = None
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else None
        if name and str(name).lower() not in {"terminate", "finish", "response"}:
            chosen = fn
            break
    chosen = chosen or (tools[0].get("function") if tools else None)
    name = (chosen or {}).get("name") or "general_qa_agent"

    params = (chosen or {}).get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else {}
    args: dict = {}
    if isinstance(props, dict) and "query" in props:
        args["query"] = user_query or "E2E deterministic query"
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def _usage(prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _completion_body(model: str, message: dict, finish_reason: str, usage: dict) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
        "usage": usage,
    }


def _stream_chunks(model: str, text: str, delay_s: float, include_usage: bool):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    def _chunk(delta: dict, finish_reason=None, usage=None) -> dict:
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
            **({"usage": usage} if usage is not None else {}),
        }

    yield _chunk({"role": "assistant", "content": ""})
    for token in text:
        if delay_s > 0:
            time.sleep(delay_s)
        yield _chunk({"content": token})
    yield _chunk({}, finish_reason="stop")
    if include_usage:
        yield _chunk({}, finish_reason=None, usage=_usage(10, len(text)))


class FakeLLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep container logs readable
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            self._send_json({"ok": True, "service": "map-e2e-fake-llm"})
            return
        if self.path == "/__e2e/stats":
            with _LOCK:
                calls = list(_STATE["calls"])
            by_kind: dict[str, int] = {}
            for call in calls:
                by_kind[call["kind"]] = by_kind.get(call["kind"], 0) + 1
            self._send_json({"total": len(calls), "by_kind": by_kind, "calls": calls})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if self.path == "/__e2e/config":
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, status=400)
                return
            with _LOCK:
                if "stream_token_delay_s" in body:
                    _STATE["stream_token_delay_s"] = float(body["stream_token_delay_s"])
            self._send_json({"ok": True})
            return

        if self.path == "/__e2e/reset":
            with _LOCK:
                _STATE["stream_token_delay_s"] = 0.0
                _STATE["calls"] = []
            self._send_json({"ok": True})
            return

        if self.path not in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json({"error": "not found"}, status=404)
            return

        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, status=400)
            return

        model = str(body.get("model") or "fake-e2e-model")
        stream = bool(body.get("stream"))
        messages = body.get("messages") or []
        user_query = _last_user_message(messages)

        response_format = body.get("response_format") or {}
        schema = (
            response_format.get("json_schema", {}).get("schema")
            if isinstance(response_format, dict)
            else None
        )
        classification = _classification_payload(schema) if isinstance(schema, dict) else None

        tool_choice = body.get("tool_choice")
        tool_required = tool_choice == "required" or (
            isinstance(tool_choice, dict) and tool_choice.get("type") == "required"
        )
        has_tools = bool(body.get("tools"))

        if classification is not None:
            kind = "json_schema"
            message = {"role": "assistant", "content": json.dumps(classification, ensure_ascii=False)}
            finish_reason = "stop"
        elif has_tools and tool_required:
            kind = "tool_call"
            message = _tool_call_payload(body, user_query)
            finish_reason = "tool_calls"
        else:
            kind = "plain"
            message = {"role": "assistant", "content": ANSWER}
            finish_reason = "stop"

        _record_call(
            {
                "kind": kind,
                "stream": stream,
                "model": model,
                "has_tools": has_tools,
                "tool_choice": tool_choice if isinstance(tool_choice, str) else (tool_choice or {}).get("type") if isinstance(tool_choice, dict) else None,
            }
        )

        if not stream:
            self._send_json(
                _completion_body(model, message, finish_reason, _usage(10, 5))
            )
            return

        if kind == "tool_call":
            # Streaming tool selection: one delta carrying the full tool_call.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    **message["tool_calls"][0],
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            finish_chunk = {
                **chunk,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ],
            }
            try:
                for payload in (chunk, finish_chunk):
                    self.wfile.write(
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                    )
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        text = str(message.get("content") or ANSWER)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for chunk in _stream_chunks(model, text, _stream_delay(), include_usage):
                self.wfile.write(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                )
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            # BFF aborted the stream (stop scenario): expected, not an error.
            pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), FakeLLMHandler)
    print(f"fake-llm listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
