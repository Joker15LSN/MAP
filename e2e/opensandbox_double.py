#!/usr/bin/env python3
"""E2E OpenSandbox 0.2.2 contract double (S6-02/S6-03 fault matrix).

A REAL HTTP server implementing the OpenSandbox subset the map_core client
uses, with per-Idempotency-Key dedup and observable server-side action
counters:

- POST /api/v1/sandboxes            (create; dedup by Idempotency-Key)
- POST /api/v1/sandboxes/{id}/execute (execute; dedup by Idempotency-Key)
- GET  /api/v1/sandboxes/{id}       (state incl. executions)
- DELETE /api/v1/sandboxes/{id}
- GET  /__counts                    {"create_actions", "execute_actions",
                                     "request_bytes"}
- POST /__reset                     zero the counters
- GET  /health

request_bytes counts EVERY request-body byte the server received, so the
runner can prove a rejected Core request never reached the provider (zero
remote bytes).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "sandbox_seq": 1,
    "create_actions": 0,
    "execute_actions": 0,
    "request_bytes": 0,
    "creates": {},   # idempotency key -> sandbox_id
    "sandboxes": {},  # workspace_id -> {sandbox_id, status, executions}
}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: ANN002 - silence
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        with LOCK:
            STATE["request_bytes"] += len(raw)
        return json.loads(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        with LOCK:
            if self.path == "/health":
                return self._json(200, {"status": "ok"})
            if self.path == "/__counts":
                return self._json(
                    200,
                    {
                        "create_actions": STATE["create_actions"],
                        "execute_actions": STATE["execute_actions"],
                        "request_bytes": STATE["request_bytes"],
                    },
                )
            if "/api/v1/sandboxes/" in self.path:
                sandbox_id = self.path.rsplit("/", 1)[-1]
                for sandbox in STATE["sandboxes"].values():
                    if sandbox["sandbox_id"] == sandbox_id:
                        return self._json(200, sandbox)
        return self._json(404, {"error": "unknown sandbox"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        with LOCK:
            if self.path == "/__reset":
                STATE["create_actions"] = 0
                STATE["execute_actions"] = 0
                STATE["request_bytes"] = 0
                STATE["creates"] = {}
                STATE["sandboxes"] = {}
                return self._json(200, {"reset": True})
            key = self.headers.get("Idempotency-Key")
            if self.path == "/api/v1/sandboxes":
                payload = self._read()
                if key and key in STATE["creates"]:
                    return self._json(
                        201,
                        {"sandbox_id": STATE["creates"][key], "status": "ready"},
                    )
                sandbox_id = f"sb-{STATE['sandbox_seq']}"
                STATE["sandbox_seq"] += 1
                STATE["create_actions"] += 1
                if key:
                    STATE["creates"][key] = sandbox_id
                STATE["sandboxes"][payload.get("workspace_id")] = {
                    "sandbox_id": sandbox_id,
                    "status": "ready",
                    "executions": [],
                }
                return self._json(201, {"sandbox_id": sandbox_id, "status": "ready"})
            if self.path.endswith("/execute"):
                sandbox_id = self.path.rsplit("/", 2)[-2]
                payload = self._read()
                sandbox = STATE["sandboxes"].get(payload.get("workspace_id"))
                if sandbox is None or sandbox["sandbox_id"] != sandbox_id:
                    return self._json(404, {"error": "unknown sandbox"})
                for execution in sandbox["executions"]:
                    if execution.get("key") == key:
                        return self._json(
                            200,
                            {
                                "sandbox_id": sandbox_id,
                                "status": "completed",
                                "exit_code": 0,
                                "output": execution["output"],
                            },
                        )
                execution = {
                    "key": key,
                    "command": payload.get("command"),
                    "output": f"ok: {payload.get('command')}",
                }
                sandbox["executions"].append(execution)
                STATE["execute_actions"] += 1
                return self._json(
                    200,
                    {
                        "sandbox_id": sandbox_id,
                        "status": "completed",
                        "exit_code": 0,
                        "output": execution["output"],
                    },
                )
        return self._json(404, {"error": "no route"})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        with LOCK:
            sandbox_id = self.path.rsplit("/", 1)[-1]
            for ws, sandbox in list(STATE["sandboxes"].items()):
                if sandbox["sandbox_id"] == sandbox_id:
                    del STATE["sandboxes"][ws]
            self.send_response(204)
            self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    print("opensandbox-double listening on :8099", flush=True)
    server.serve_forever()
