#!/usr/bin/env python3
"""One-command Compose cross-service E2E runner (R2-P1-05).

What it does, in ONE command (stdlib-only, no host dependencies):

    python3 e2e/run_e2e.py

1. Picks a RANDOM compose project name, unique container names and free
   published ports, then starts a FRESH topology from
   ``docker-compose.yml`` + ``docker-compose.otel.yml`` +
   ``e2e/docker-compose.e2e.yml`` (new named volumes; never touches the
   local dev stack or its 15432 data).
2. Topology under test: real PostgreSQL, real MongoDB, real map_core
   (algorithm-service), real BFF (backend-service), real outbox worker,
   real frontend (vite dev server), OTel Collector + Jaeger. The ONLY
   fake is ``fake-llm`` at the LLM boundary (``MAP_LLM_BASE_URL`` ->
   deterministic OpenAI-compatible server); no service under acceptance
   is replaced.
3. Drives REAL browser traffic through frontend -> BFF -> core
   (Playwright/Chromium: create -> stream -> reload -> stop -> feedback
   -> withdraw, plus wire-level identity-header assertions) AND real
   HTTP/SSE against the published BFF port (no ASGI in-process
   transport): happy path, idempotent replays, duplicate request_id
   replay, mid-stream stop/abort, core restart recovery, feedback +
   withdraw, audit chain + append-only tamper rejection.
4. Runs the secure identity matrix by recreating the BFF with
   ``MAP_AUTH_MODE=trusted_header`` (forged admin always 401, member
   403 on admin writes, service audience/scope, cross-user/workspace)
   and restores dev mode afterwards.
5. Runs the fault matrix: BFF restart, PostgreSQL pause/unpause
   interruption with recovery, worker kill + expired-lease takeover.
6. Verifies ID consistency across PostgreSQL / MongoDB / OTel:
   request_id + trace_id + session_id + workspace_id land in Mongo
   ``request_records``; conversation/message/workspace IDs are checked
   in PostgreSQL; the same trace exists in Jaeger with spans from BOTH
   services.
7. Emits a report (service versions, request IDs, DB counts, final
   states, audit chain result) as JSON + console summary.
8. ALWAYS cleans up: ``docker compose down -v --remove-orphans``;
   on failure the compose logs are dumped first.

Exit code 0 = every scenario green; non-zero otherwise.

Suites (R3-P1-02): ``--suite pr`` runs the stable CI subset (browser
happy path + identity boundary on top of the core happy path);
``--suite full`` (default) adds the whole fault matrix.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.otel.yml",
    REPO_ROOT / "e2e" / "docker-compose.e2e.yml",
]
SERVICES = [
    "postgres",
    "mongo",
    "fake-llm",
    "migrate",
    "algorithm-service",
    "backend-service",
    "worker-service",
    "frontend-service",
    "otel-collector",
    "jaeger",
]
HEALTHY_REQUIRED = {
    "postgres",
    "mongo",
    "fake-llm",
    "algorithm-service",
    "backend-service",
}
RUNNING_REQUIRED = HEALTHY_REQUIRED | {
    "worker-service",
    "frontend-service",
    "otel-collector",
    "jaeger",
}

ANSWER = "这是 MAP 端到端测试的确定性回答。"
WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
WORKSPACE_ID_B = "00000000-0000-0000-0000-000000000002"

HEALTH_TIMEOUT_S = 600.0
FRONTEND_READY_TIMEOUT_S = 900.0  # container runs npm ci on a fresh volume
POLL_S = 2.0


class E2EFailure(AssertionError):
    """A scenario assertion failed; abort the run (cleanup still runs)."""


class Ctx:
    def __init__(self) -> None:
        self.project = f"map-e2e-{secrets.token_hex(4)}"
        self.prefix = self.project
        self.ports: dict[str, int] = {}
        self.data_dir = REPO_ROOT / "e2e" / "tmp" / self.project
        self.env: dict[str, str] = {}
        self.bff: str = ""
        self.frontend: str = ""
        self.jaeger: str = ""
        self.fake_llm: str = ""
        self.postgres_container = f"{self.prefix}-postgres"
        self.mongo_container = f"{self.prefix}-mongo"
        self.algo_container = f"{self.prefix}-algorithm-service"
        self.backend_container = f"{self.prefix}-backend-service"
        self.worker_container = f"{self.prefix}-worker-service"
        self.report: dict = {
            "project": self.project,
            "suite": "full",
            "scenarios": {},
            "versions": {},
            "request_ids": {},
            "db_counts": {},
            "final_states": {},
            "audit": {},
            "otel": {},
            "browser": {},
            "identity": {},
            "faults": {},
        }
        self.started = time.time()


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd: list[str], *, env: dict | None = None, check: bool = True, timeout: float = 900.0) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    try:
        return subprocess.run(
            cmd,
            env=merged,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        tail_out = (exc.stdout or "")[-3000:]
        tail_err = (exc.stderr or "")[-3000:]
        raise E2EFailure(
            f"command failed (exit {exc.returncode}): {' '.join(cmd[:6])}...\n"
            f"--- stdout (tail) ---\n{tail_out}\n--- stderr (tail) ---\n{tail_err}"
        ) from exc


def compose(ctx: Ctx, args: list[str], *, check: bool = True, timeout: float = 1800.0) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose"]
    for file in COMPOSE_FILES:
        cmd += ["-f", str(file)]
    cmd += ["-p", ctx.project, "--profile", "otel", *args]
    return run(cmd, env=ctx.env, check=check, timeout=timeout)


def http_request(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.getcode()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, resp_headers, raw
    return status, resp_headers, raw


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    status, _, raw = http_request(method, url, body, headers, timeout)
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", "replace")[:2000]}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


def poll_until(fn, *, timeout_s: float, what: str, interval_s: float = POLL_S) -> object:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - poll anything, keep trying
            last_error = exc
        time.sleep(interval_s)
    detail = f" (last error: {last_error})" if last_error else ""
    raise E2EFailure(f"timed out after {timeout_s:.0f}s waiting for {what}{detail}")


def traceparent_for(trace_id: str) -> str:
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


# ---------------------------------------------------------------------------
# docker helpers
# ---------------------------------------------------------------------------


def docker_exec(container: str, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "exec", container, *cmd], check=check, timeout=120.0)


def psql(ctx: Ctx, sql: str) -> str:
    result = docker_exec(
        ctx.postgres_container,
        # -q suppresses command tags ("INSERT 0 1") that would otherwise
        # pollute RETURNING captures used as ids in later statements.
        ["psql", "-U", "map_admin", "-d", "map", "-Atqc", sql],
    )
    return result.stdout.strip()


def mongosh_eval(ctx: Ctx, expr: str) -> str:
    result = docker_exec(
        ctx.mongo_container,
        [
            "mongosh",
            "--quiet",
            "-u",
            "map",
            "-p",
            "map",
            "--authenticationDatabase",
            "admin",
            "map_db_dev",
            "--eval",
            expr,
        ],
    )
    return result.stdout.strip()


def compose_ps(ctx: Ctx) -> dict[str, dict]:
    result = compose(ctx, ["ps", "--format", "json"], check=False, timeout=60.0)
    services: dict[str, dict] = {}
    text = result.stdout.strip()
    if not text:
        return services
    entries: list[dict] = []
    try:
        parsed = json.loads(text)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(item if isinstance(item, dict) else {})
    for entry in entries:
        name = entry.get("Service") or entry.get("service")
        if not name:
            continue
        services[str(name)] = {
            "state": str(entry.get("State") or entry.get("state") or "").lower(),
            "health": str(entry.get("Health") or entry.get("health") or "").lower(),
            "exit_code": entry.get("ExitCode", entry.get("exitCode")),
        }
    return services


# ---------------------------------------------------------------------------
# SSE client (real HTTP against the published BFF port)
# ---------------------------------------------------------------------------


class SseCollector:
    """Reads an SSE POST response on a background thread."""

    def __init__(self, host: str, port: int, path: str, body: dict, headers: dict):
        self.events: list[tuple[str, dict]] = []
        self.error: Exception | None = None
        self.finished = threading.Event()
        self.first_delta = threading.Event()
        self.status: int | None = None
        self.resp_headers: dict[str, str] = {}
        self._host = host
        self._port = port
        self._path = path
        self._body = body
        self._headers = headers
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "SseCollector":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=120.0)
            payload = json.dumps(self._body).encode("utf-8")
            headers = {"Content-Type": "application/json", **self._headers}
            conn.request("POST", self._path, body=payload, headers=headers)
            resp = conn.getresponse()
            self.status = resp.status
            self.resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            buffer = b""
            while True:
                chunk = resp.read(256)
                if not chunk:
                    break
                buffer += chunk
                while b"\n\n" in buffer:
                    frame, buffer = buffer.split(b"\n\n", 1)
                    self._parse_frame(frame.decode("utf-8", "replace"))
            if buffer.strip():
                self._parse_frame(buffer.decode("utf-8", "replace"))
            conn.close()
        except Exception as exc:  # noqa: BLE001 - surfaced via .error
            self.error = exc
        finally:
            self.finished.set()
            self.first_delta.set()

    def _parse_frame(self, frame: str) -> None:
        event = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            return
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            data = {"_raw": "\n".join(data_lines)}
        self.events.append((event, data))
        if event == "content_delta":
            self.first_delta.set()

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    def deltas_after(self, index: int) -> list[tuple[str, dict]]:
        return self.events[index:]


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def scenario_versions(ctx: Ctx) -> None:
    status, body = http_json("GET", f"{ctx.bff}/openapi.json")
    expect(status == 200, f"BFF openapi.json returned {status}")
    ctx.report["versions"]["bff"] = body.get("info", {}).get("version")
    result = docker_exec(
        ctx.algo_container,
        [
            "python",
            "-c",
            "import json,urllib.request;"
            "print(json.load(urllib.request.urlopen('http://127.0.0.1:10000/openapi.json'))['info']['version'])",
        ],
    )
    ctx.report["versions"]["map_core"] = result.stdout.strip()
    result = docker_exec(ctx.fake_llm_container, ["python", "--version"])
    ctx.report["versions"]["fake_llm_runtime"] = result.stdout.strip()
    log(
        f"versions: bff={ctx.report['versions']['bff']} "
        f"map_core={ctx.report['versions']['map_core']}"
    )


def scenario_model_center_redirect(ctx: Ctx) -> None:
    """Point every model-center large model at the fake LLM.

    The BFF embeds the model-center row (base_url/model) into each core
    request payload (``route_llm_config`` / ``summary_llm_config`` /
    per-agent ``llm_config``), so ``MAP_LLM_BASE_URL`` alone is NOT
    enough: the admin state must also resolve to the fake endpoint.
    Done through the REAL admin mutation API, so the audit chain covers
    this config write as well.
    """
    headers = {"X-Workspace-ID": WORKSPACE_ID}
    status, model = http_json("GET", f"{ctx.bff}/api/admin/model-center", None, headers)
    expect(status == 200, f"model-center GET returned {status}")
    for row in model.get("large_models") or []:
        row["model_url"] = "http://fake-llm:9999/v1"
        row["model_name"] = "fake-e2e-model"
    status, body = http_json("PUT", f"{ctx.bff}/api/admin/model-center", model, headers)
    expect(
        status == 200,
        f"model-center redirect PUT returned {status}: {body}",
    )
    log("model-center redirected to fake-llm via admin API")


def scenario_happy_path(ctx: Ctx) -> dict:
    trace_id = uuid.uuid4().hex
    request_id = f"e2e-req-{secrets.token_hex(4)}"
    session_id = f"e2e-sess-{secrets.token_hex(4)}"
    headers = {
        "X-Request-ID": request_id,
        "X-Session-ID": session_id,
        "X-Workspace-ID": WORKSPACE_ID,
        "traceparent": traceparent_for(trace_id),
        "Idempotency-Key": f"e2e-conv-{secrets.token_hex(4)}",
    }

    # create + idempotent replay
    status, created = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global", "title": "E2E 跨服务会话"},
        headers,
    )
    expect(status == 201, f"conversation create returned {status}: {created}")
    conversation_id = created["id"]
    status, replayed = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global", "title": "E2E 跨服务会话"},
        headers,
    )
    expect(status in (200, 201), f"conversation replay returned {status}")
    expect(
        replayed.get("id") == conversation_id,
        "idempotent conversation replay returned a different id",
    )

    # real SSE stream through the real topology
    sse = SseCollector(
        "127.0.0.1",
        ctx.ports["MAP_BFF_PORT"],
        f"/api/v1/conversations/{conversation_id}/messages:stream",
        {"query": "介绍一下杭州", "request_id": request_id},
        headers,
    ).start()
    expect(sse.finished.wait(90.0), "SSE stream did not finish within 90s")
    expect(sse.error is None, f"SSE stream raised: {sse.error}")
    expect(sse.status == 200, f"SSE stream HTTP status {sse.status}")
    expect(
        sse.resp_headers.get("x-request-id") == request_id,
        f"BFF did not echo X-Request-ID (got {sse.resp_headers.get('x-request-id')!r})",
    )
    names = sse.event_names()
    expect("start" in names, f"missing start event; got {names}")
    expect(names[-1] == "done", f"last event is not done; got {names}")
    done_data = sse.events[-1][1]
    content = "".join(d.get("content", "") for name, d in sse.events if name == "content_delta")
    expect(
        done_data.get("status", "completed") in {"completed", None} or done_data.get("content"),
        f"unexpected done payload: {done_data}",
    )

    start_data = next(d for name, d in sse.events if name == "start")
    message_id = start_data.get("message_id")
    expect(bool(message_id), f"start event missing message_id: {start_data}")

    # PostgreSQL final state
    message_row = psql(
        ctx,
        "SELECT status || '|' || coalesce(content,'') || '|' || coalesce(request_id::text,'') "
        f"FROM map_control.messages WHERE id = '{message_id}'",
    )
    parts = message_row.split("|", 2)
    expect(parts[0] == "completed", f"assistant message status={parts[0]!r} (row={message_row!r})")
    expect(ANSWER in parts[1], f"assistant content mismatch: {parts[1]!r}")
    expect(parts[2] == request_id, f"PG request_id {parts[2]!r} != {request_id!r}")

    # MongoDB request_records must carry the SAME request_id and trace_id
    mongo_doc = poll_until(
        lambda: mongosh_eval(
            ctx,
            "JSON.stringify(db.request_records.findOne("
            f"{{request_id: '{request_id}'}}))",
        ),
        timeout_s=20.0,
        what=f"mongo request_records[{request_id}]",
    )
    expect(mongo_doc not in {"null", ""}, f"request_records missing for {request_id}")
    record = json.loads(mongo_doc)
    expect(
        record.get("trace_id") == trace_id,
        f"mongo trace_id {record.get('trace_id')!r} != client trace {trace_id!r}",
    )
    expect(record.get("session_id") == session_id, "mongo session_id mismatch")
    expect(
        record.get("workspace_id") == WORKSPACE_ID,
        f"mongo workspace_id {record.get('workspace_id')!r} != {WORKSPACE_ID!r}",
    )

    # Full-chain ID consistency in PostgreSQL: conversation/message rows
    # must carry the SAME workspace and linkage IDs as the HTTP layer.
    conv_ws = psql(
        ctx,
        f"SELECT workspace_id::text FROM map_control.conversations WHERE id = '{conversation_id}'",
    )
    expect(conv_ws == WORKSPACE_ID, f"PG conversation workspace_id={conv_ws!r}")
    msg_link = psql(
        ctx,
        "SELECT conversation_id::text || '|' || workspace_id::text "
        f"FROM map_control.messages WHERE id = '{message_id}'",
    )
    expect(
        msg_link == f"{conversation_id}|{WORKSPACE_ID}",
        f"PG message linkage mismatch: {msg_link!r}",
    )

    # Mongo llm_call_records must cover the SAME request: the real fake
    # LLM calls land here (earlier runs queried the wrong collection and
    # reported 0).
    def _llm_call_recorded():
        count = mongosh_eval(
            ctx,
            "db.llm_call_records.countDocuments("
            f"{{request_id: '{request_id}'}})",
        )
        return int(count) >= 1

    poll_until(_llm_call_recorded, timeout_s=30.0, what=f"llm_call_records[{request_id}]")

    # OTel: the SAME trace must exist in Jaeger with spans from both
    # services. The BFF uses a BatchSpanProcessor, so its spans may land
    # several seconds after map-core's; poll until BOTH services are
    # present instead of asserting on the first partial fetch.
    def _jaeger_trace_both_services():
        status, body = http_json("GET", f"{ctx.jaeger}/api/traces/{trace_id}", timeout=10.0)
        if status != 200 or not body.get("data"):
            return None
        trace = body["data"][0]
        processes = trace.get("processes") or {}
        names = {p.get("serviceName") or "" for p in processes.values()}
        has_bff = any("backend" in name for name in names)
        has_core = any("core" in name for name in names)
        return trace if has_bff and has_core else None

    trace = poll_until(
        _jaeger_trace_both_services, timeout_s=60.0, what=f"jaeger trace {trace_id} (both services)"
    )
    processes = trace.get("processes") or {}
    service_names = sorted({p.get("serviceName") for p in processes.values()})
    expect(
        any("backend" in name for name in service_names),
        f"jaeger trace lacks BFF spans; services={service_names}",
    )
    expect(
        any("core" in name for name in service_names),
        f"jaeger trace lacks map_core spans; services={service_names}",
    )
    ctx.report["otel"] = {
        "trace_id": trace_id,
        "jaeger_span_count": len(trace.get("spans") or []),
        "services": service_names,
    }

    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "request_id": request_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "content": content,
    }


def scenario_duplicate_request(ctx: Ctx, happy: dict) -> None:
    headers = {
        "X-Request-ID": happy["request_id"],
        "X-Session-ID": happy["session_id"],
        "X-Workspace-ID": WORKSPACE_ID,
    }
    sse = SseCollector(
        "127.0.0.1",
        ctx.ports["MAP_BFF_PORT"],
        f"/api/v1/conversations/{happy['conversation_id']}/messages:stream",
        {"query": "重复请求", "request_id": happy["request_id"]},
        headers,
    ).start()
    expect(sse.finished.wait(30.0), "duplicate request stream did not finish")
    expect(sse.status == 200, f"duplicate request HTTP status {sse.status}")
    done = [d for name, d in sse.events if name == "done"]
    expect(bool(done), f"duplicate request produced no done event: {sse.events}")
    expect(
        done[-1].get("replayed") is True or done[-1].get("message_id") == happy["message_id"],
        f"duplicate request_id was not replayed: {done}",
    )


def scenario_stop_mid_stream(ctx: Ctx) -> dict:
    # Slow the fake LLM down so the stream is still open when we stop it.
    status, _ = http_json(
        "POST", f"{ctx.fake_llm}/__e2e/config", {"stream_token_delay_s": 0.35}
    )
    expect(status == 200, f"fake-llm config returned {status}")

    trace_id = uuid.uuid4().hex
    request_id = f"e2e-stop-{secrets.token_hex(4)}"
    headers = {
        "X-Request-ID": request_id,
        "X-Workspace-ID": WORKSPACE_ID,
        "traceparent": traceparent_for(trace_id),
    }
    status, created = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global", "title": "E2E stop 场景"},
        headers,
    )
    expect(status == 201, f"stop conversation create returned {status}")
    conversation_id = created["id"]

    sse = SseCollector(
        "127.0.0.1",
        ctx.ports["MAP_BFF_PORT"],
        f"/api/v1/conversations/{conversation_id}/messages:stream",
        {"query": "慢慢回答我", "request_id": request_id},
        headers,
    ).start()
    expect(sse.first_delta.wait(30.0), "no content_delta before stop")
    start_data = next(d for name, d in sse.events if name == "start")
    message_id = start_data["message_id"]
    stop_index = len(sse.events)

    status, stop_body = http_json(
        "POST", f"{ctx.bff}/api/v1/messages/{message_id}:stop", None, headers
    )
    expect(status == 200, f"stop returned {status}: {stop_body}")
    expect(stop_body.get("abort") is True, f"stop did not hit the registry: {stop_body}")

    expect(sse.finished.wait(15.0), "stream did not close within 15s after stop")
    tail = sse.deltas_after(stop_index)
    late_deltas = [d for name, d in tail if name == "content_delta"]
    expect(
        len(late_deltas) <= 1,
        f"stream kept producing after stop: {len(late_deltas)} extra deltas",
    )
    done = [d for name, d in sse.events if name == "done"]
    expect(bool(done), f"stop stream emitted no done event: {sse.events}")

    final_status = psql(
        ctx,
        f"SELECT status FROM map_control.messages WHERE id = '{message_id}'",
    )
    expect(final_status == "stopped", f"stopped message final status={final_status!r}")

    http_json("POST", f"{ctx.fake_llm}/__e2e/config", {"stream_token_delay_s": 0.0})
    return {"conversation_id": conversation_id, "message_id": message_id, "request_id": request_id}


def scenario_core_restart(ctx: Ctx) -> dict:
    docker_exec(ctx.algo_container, ["true"])  # container exists
    run(["docker", "restart", ctx.algo_container], timeout=300.0)

    def _algo_healthy():
        services = compose_ps(ctx)
        info = services.get("algorithm-service") or {}
        return info.get("state") == "running" and info.get("health") == "healthy"

    poll_until(_algo_healthy, timeout_s=180.0, what="algorithm-service healthy after restart")

    trace_id = uuid.uuid4().hex
    request_id = f"e2e-restart-{secrets.token_hex(4)}"
    headers = {
        "X-Request-ID": request_id,
        "X-Workspace-ID": WORKSPACE_ID,
        "traceparent": traceparent_for(trace_id),
    }
    status, created = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global", "title": "E2E core 重启恢复"},
        headers,
    )
    expect(status == 201, f"post-restart conversation create returned {status}")
    sse = SseCollector(
        "127.0.0.1",
        ctx.ports["MAP_BFF_PORT"],
        f"/api/v1/conversations/{created['id']}/messages:stream",
        {"query": "重启后还能用吗", "request_id": request_id},
        headers,
    ).start()
    expect(sse.finished.wait(90.0), "post-restart stream did not finish")
    expect(sse.status == 200, f"post-restart stream HTTP status {sse.status}")
    expect(sse.event_names()[-1] == "done", f"post-restart stream events: {sse.event_names()}")
    return {"request_id": request_id}


def scenario_feedback_and_audit(ctx: Ctx, happy: dict) -> None:
    headers = {"X-Workspace-ID": WORKSPACE_ID}
    message_id = happy["message_id"]

    status, body = http_json(
        "PUT",
        f"{ctx.bff}/api/v1/messages/{message_id}/feedback",
        {"rating": "unhelpful", "reason_codes": ["incorrect"]},
        headers,
    )
    expect(status == 200, f"feedback PUT returned {status}: {body}")

    status, body = http_json("GET", f"{ctx.bff}/api/v1/admin/feedback", None, headers)
    expect(status == 200 and body.get("count", 0) >= 1, f"admin feedback list: {status} {body}")

    status, body = http_json("DELETE", f"{ctx.bff}/api/v1/messages/{message_id}/feedback", None, headers)
    expect(status == 200, f"feedback withdraw returned {status}")
    status, body = http_json("GET", f"{ctx.bff}/api/v1/messages/{message_id}/feedback", None, headers)
    expect(status == 200 and body in ({}, None, {"feedback": None}), f"withdrawn feedback still visible: {body}")

    # tombstone + outbox in PG
    withdrawn = psql(
        ctx,
        "SELECT count(*) FROM map_control.message_feedback "
        f"WHERE message_id = '{message_id}' AND status = 'withdrawn'",
    )
    expect(withdrawn == "1", f"expected withdrawn tombstone, got {withdrawn!r}")

    # outbox event persisted atomically with the tombstone. Per
    # SPEC/contracts/job-outbox.md the outbox currently has a writer only
    # (no relay), so the E2E verifies durable persistence, NOT delivery.
    def _outbox_persisted():
        return (
            psql(
                ctx,
                "SELECT count(*) FROM map_control.outbox_events "
                "WHERE event_type = 'feedback_withdrawn' "
                f"AND aggregate_id = '{message_id}'",
            )
            == "1"
        )

    poll_until(_outbox_persisted, timeout_s=30.0, what="feedback_withdrawn outbox row")

    # admin config write -> audit event; chain verifies end-to-end
    status, model = http_json("GET", f"{ctx.bff}/api/admin/model-center", None, headers)
    expect(status == 200, f"model-center GET returned {status}")
    status, body = http_json("PUT", f"{ctx.bff}/api/admin/model-center", model, headers)
    expect(status == 200, f"model-center PUT returned {status}: {body}")

    status, verify = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events/verify", None, headers)
    expect(status == 200 and verify.get("ok") is True, f"audit chain verify failed: {verify}")
    status, listing = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events", None, headers)
    expect(status == 200 and listing.get("total", 0) >= 1, f"audit events listing: {listing}")
    ctx.report["audit"] = {
        "verify_ok": verify.get("ok"),
        "total_events": listing.get("total"),
    }

    # R2-P1-04 re-check inside the E2E topology: the app role the BFF/worker
    # actually use must NOT be able to rewrite or delete recorded audit
    # history. The privilege contract (migration 9a2b3c4d5e6f) grants the
    # app role exactly: events = SELECT+INSERT (append-only), chain_head =
    # SELECT+INSERT+UPDATE (the writer's advance point), mutations = full
    # DML — so only the append-only events table is probed, and the probes
    # use real columns so a permission failure (not a SQL typo) is asserted.
    tamper_probes = [
        "UPDATE map_control.config_audit_events SET action = action",
        "DELETE FROM map_control.config_audit_events",
        "TRUNCATE map_control.config_audit_events",
    ]
    for statement in tamper_probes:
        tamper = docker_exec(
            ctx.postgres_container,
            ["psql", "-U", "map", "-d", "map", "-Atc", statement],
            check=False,
        )
        expect(
            tamper.returncode != 0 and "permission denied" in tamper.stderr.lower(),
            f"app role tampered with audit tables! sql={statement!r} "
            f"rc={tamper.returncode} err={tamper.stderr!r}",
        )
    ctx.report["audit"]["app_role_tamper_rejected"] = True


def scenario_worker_reconcile(ctx: Ctx, happy: dict) -> None:
    """Real worker proof: enqueue a ``message_reconcile`` job and require the
    running worker to claim it (lease/fence path of R2-P0-WORKER) and
    reconcile a stale ``streaming`` message to ``failed/STREAM_INTERRUPTED``.
    """
    stale_id = psql(
        ctx,
        "INSERT INTO map_control.messages "
        "(conversation_id, workspace_id, role, status, content, version, updated_at) "
        f"VALUES ('{happy['conversation_id']}', '{WORKSPACE_ID}', 'assistant', "
        "'streaming', '', 1, now() - interval '1 hour') RETURNING id",
    )
    expect(bool(stale_id), "failed to insert stale streaming message")
    job_id = psql(
        ctx,
        "INSERT INTO map_control.jobs "
        "(workspace_id, job_type, status, priority, attempt, max_attempts) "
        f"VALUES ('{WORKSPACE_ID}', 'message_reconcile', 'queued', 0, 0, 3) RETURNING id",
    )
    expect(bool(job_id), "failed to enqueue message_reconcile job")

    def _job_succeeded():
        return (
            psql(ctx, f"SELECT status FROM map_control.jobs WHERE id = '{job_id}'")
            == "succeeded"
        )

    poll_until(_job_succeeded, timeout_s=60.0, what="worker claims and finishes reconcile job")

    row = psql(
        ctx,
        "SELECT status || '|' || coalesce(stream_error, '') "
        f"FROM map_control.messages WHERE id = '{stale_id}'",
    )
    expect(
        row == "failed|STREAM_INTERRUPTED",
        f"stale streaming message not reconciled: {row!r}",
    )
    # The happy-path message must keep its terminal state (idempotent
    # reconciler never touches non-streaming rows).
    untouched = psql(
        ctx,
        f"SELECT status FROM map_control.messages WHERE id = '{happy['message_id']}'",
    )
    expect(untouched == "completed", f"reconciler damaged a terminal message: {untouched!r}")


# ---------------------------------------------------------------------------
# browser E2E (R3-P1-02: browser -> frontend -> BFF -> core)
# ---------------------------------------------------------------------------


def scenario_browser(ctx: Ctx) -> dict:
    """Run the Playwright scenarios in a subprocess and cross-check the
    captured conversation/session IDs against PostgreSQL and MongoDB."""
    out_path = REPO_ROOT / "e2e" / "tmp" / f"browser-{ctx.project}.json"
    cmd = [
        sys.executable,
        str(REPO_ROOT / "e2e" / "browser_e2e.py"),
        "--frontend-url",
        ctx.frontend,
        "--fake-llm-url",
        ctx.fake_llm,
        "--workspace-id",
        WORKSPACE_ID,
        "--out",
        str(out_path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1200.0, cwd=REPO_ROOT
    )
    if proc.returncode == 77:
        raise E2EFailure(
            "browser E2E requires Playwright: "
            "pip install playwright && playwright install chromium"
        )
    expect(
        proc.returncode == 0,
        f"browser E2E failed (exit {proc.returncode}):\n"
        f"--- stdout (tail) ---\n{proc.stdout[-3000:]}\n"
        f"--- stderr (tail) ---\n{proc.stderr[-3000:]}",
    )
    browser_report = json.loads(out_path.read_text(encoding="utf-8"))
    expect(
        browser_report.get("result") == "PASS",
        f"browser report not PASS: {browser_report.get('failure')}",
    )

    conversation_id = browser_report.get("conversation_id")
    expect(bool(conversation_id), "browser report missing conversation_id")
    session_ids = browser_report.get("session_ids") or []
    expect(len(session_ids) == 1, f"browser session ids not unique: {session_ids}")
    session_id = session_ids[0]

    # PG cross-check: the browser-created conversation lives in the right
    # workspace and its assistant message completed with the fake answer.
    conv_ws = psql(
        ctx,
        f"SELECT workspace_id::text FROM map_control.conversations WHERE id = '{conversation_id}'",
    )
    expect(conv_ws == WORKSPACE_ID, f"browser conversation workspace={conv_ws!r}")
    msg_row = psql(
        ctx,
        "SELECT status || '|' || coalesce(content,'') "
        "FROM map_control.messages "
        f"WHERE conversation_id = '{conversation_id}' AND role = 'assistant' "
        "ORDER BY created_at LIMIT 1",
    )
    parts = msg_row.split("|", 1)
    expect(parts[0] == "completed", f"browser assistant message status={parts[0]!r}")
    expect(ANSWER in (parts[1] if len(parts) > 1 else ""), f"browser answer mismatch: {msg_row!r}")

    # Mongo cross-check: the browser's X-Session-ID reached map_core.
    mongo_doc = poll_until(
        lambda: mongosh_eval(
            ctx,
            "JSON.stringify(db.request_records.findOne("
            f"{{session_id: '{session_id}'}}))",
        ),
        timeout_s=30.0,
        what=f"mongo request_records[session={session_id}]",
    )
    expect(mongo_doc not in {"null", ""}, "mongo has no record for the browser session")
    record = json.loads(mongo_doc)
    expect(
        record.get("workspace_id") == WORKSPACE_ID,
        f"browser session mongo workspace_id={record.get('workspace_id')!r}",
    )
    browser_report["pg_cross_check"] = "PASS"
    browser_report["mongo_cross_check"] = "PASS"
    ctx.report["browser"] = browser_report
    ctx.report["request_ids"]["browser_session"] = session_id
    return browser_report


# ---------------------------------------------------------------------------
# secure identity matrix (R3-P1-02: trusted_header mode)
# ---------------------------------------------------------------------------


def recreate_backend(ctx: Ctx, extra_env: dict[str, str]) -> None:
    """Force-recreate backend-service with changed env and wait for /ready."""
    ctx.env.update(extra_env)
    compose(ctx, ["up", "-d", "--force-recreate", "--no-deps", "backend-service"], timeout=900.0)

    def _healthy():
        info = compose_ps(ctx).get("backend-service") or {}
        return info.get("state") == "running" and info.get("health") == "healthy"

    poll_until(_healthy, timeout_s=300.0, what="backend-service healthy after recreate")


def scenario_identity_boundary(ctx: Ctx) -> dict:
    """Recreate the BFF in trusted_header mode and run the identity matrix.

    Covers: forged admin (always 401), member 403 on admin writes/audit
    reads, service audience/scope enforcement, cross-user and
    cross-workspace isolation. Restores dev mode at the end.
    """
    secret = f"e2e-proxy-{secrets.token_hex(16)}"
    credentials = [
        {
            "key_id": "k-e2e-obs",
            "token": "svc-e2e-obs-token",
            "service_name": "obs-service",
            "audience": "map-bff",
            "scopes": ["internal.ping"],
        },
        {
            "key_id": "k-e2e-wrongaud",
            "token": "svc-e2e-wrongaud-token",
            "service_name": "other-service",
            "audience": "not-map-bff",
            "scopes": ["internal.ping"],
        },
        {
            "key_id": "k-e2e-noscope",
            "token": "svc-e2e-noscope-token",
            "service_name": "limited-service",
            "audience": "map-bff",
            "scopes": [],
        },
    ]
    recreate_backend(
        ctx,
        {
            "MAP_AUTH_MODE": "trusted_header",
            "MAP_TRUSTED_PROXY_SECRET": secret,
            "MAP_TRUSTED_PROXY_REQUIRED": "true",
            "MAP_SERVICE_CREDENTIALS": json.dumps(credentials),
            "MAP_SERVICE_AUDIENCE": "map-bff",
        },
    )
    proxy = {"X-Trusted-Proxy-Secret": secret}
    results: dict[str, str] = {}
    try:
        # 1. forged admin is ALWAYS 401 (no secret / wrong secret), even
        #    when the forged headers claim platform_admin.
        for _ in range(3):
            forged = {
                "X-UserId": "mallory",
                "X-User-Roles": "platform_admin",
                "X-Workspace-ID": WORKSPACE_ID,
            }
            status, _ = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events", None, forged)
            expect(status == 401, f"forged admin without secret got {status}, want 401")
            wrong = {**forged, "X-Trusted-Proxy-Secret": "wrong-secret"}
            status, _ = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events", None, wrong)
            expect(status == 401, f"forged admin with wrong secret got {status}, want 401")
        results["forged_admin_always_401"] = "PASS"

        # 2. legitimate admin through the trusted proxy.
        alice = {
            **proxy,
            "X-UserId": "alice",
            "X-User-Roles": "platform_admin",
            "X-Workspace-ID": WORKSPACE_ID,
        }
        status, listing = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events", None, alice)
        expect(status == 200, f"alice audit read got {status}: {listing}")
        status, conv = http_json(
            "POST", f"{ctx.bff}/api/v1/conversations", {"mode": "global", "title": "secure"}, alice
        )
        expect(status == 201, f"alice conversation create got {status}: {conv}")
        alice_conv = conv["id"]
        results["trusted_admin_allowed"] = "PASS"

        # 3. member role: admin writes and audit reads are 403.
        bob = {
            **proxy,
            "X-UserId": "bob",
            "X-User-Roles": "member",
            "X-Workspace-ID": WORKSPACE_ID,
        }
        status, model = http_json("GET", f"{ctx.bff}/api/admin/model-center", None, bob)
        expect(status == 200, f"member model-center read got {status}")
        status, body = http_json("PUT", f"{ctx.bff}/api/admin/model-center", model, bob)
        expect(status == 403, f"member model-center write got {status}, want 403: {body}")
        status, _ = http_json("GET", f"{ctx.bff}/api/v1/admin/audit-events", None, bob)
        expect(status == 403, f"member audit read got {status}, want 403")
        results["member_403"] = "PASS"

        # 4. cross-user isolation inside the same workspace.
        status, _ = http_json(
            "GET", f"{ctx.bff}/api/v1/conversations/{alice_conv}", None, bob
        )
        expect(status == 404, f"bob fetched alice's conversation: {status}")
        status, bob_list = http_json("GET", f"{ctx.bff}/api/v1/conversations", None, bob)
        expect(status == 200 and isinstance(bob_list, list), f"bob list got {status}")
        expect(
            all(item.get("id") != alice_conv for item in bob_list),
            "bob's conversation list leaked alice's conversation",
        )
        results["cross_user_isolation"] = "PASS"

        # 5. cross-workspace isolation (even for a platform_admin of ws B).
        carol = {
            **proxy,
            "X-UserId": "carol",
            "X-User-Roles": "platform_admin",
            "X-Workspace-ID": WORKSPACE_ID_B,
        }
        status, _ = http_json(
            "GET", f"{ctx.bff}/api/v1/conversations/{alice_conv}", None, carol
        )
        expect(status == 404, f"carol fetched ws-A conversation: {status}")
        status, carol_list = http_json("GET", f"{ctx.bff}/api/v1/conversations", None, carol)
        expect(
            status == 200 and isinstance(carol_list, list) and not carol_list,
            f"carol list leaked ws-A data: {status} {carol_list}",
        )
        results["cross_workspace_isolation"] = "PASS"

        # 6. service identity: inherent claims only, audience + scope enforced.
        ok_status, ok_body = http_json(
            "GET",
            f"{ctx.bff}/internal/v1/ping",
            None,
            {"Authorization": "Bearer svc-e2e-obs-token"},
        )
        expect(ok_status == 200, f"service ping got {ok_status}: {ok_body}")
        expect(ok_body.get("service") == "obs-service", f"service name: {ok_body}")
        expect(ok_body.get("audience") == "map-bff", f"service audience: {ok_body}")
        expect(ok_body.get("key_id") == "k-e2e-obs", f"service key_id: {ok_body}")
        expect(ok_body.get("scopes") == ["internal.ping"], f"service scopes: {ok_body}")

        status, _ = http_json(
            "GET",
            f"{ctx.bff}/internal/v1/ping",
            None,
            {"Authorization": "Bearer svc-e2e-wrongaud-token"},
        )
        expect(status == 401, f"wrong-audience service token got {status}, want 401")
        status, _ = http_json(
            "GET",
            f"{ctx.bff}/internal/v1/ping",
            None,
            {"Authorization": "Bearer svc-e2e-noscope-token"},
        )
        expect(status == 403, f"no-scope service token got {status}, want 403")
        status, _ = http_json(
            "GET",
            f"{ctx.bff}/internal/v1/ping",
            None,
            {
                "X-Service-Name": "obs-service",
                "X-Service-Scopes": "internal.ping,admin",
            },
        )
        expect(status == 401, f"forged X-Service-* headers got {status}, want 401")
        status, _ = http_json(
            "GET", f"{ctx.bff}/internal/v1/ping", None, {**proxy, "X-UserId": "alice"}
        )
        expect(status == 401, f"user principal on /internal got {status}, want 401")
        status, _ = http_json(
            "POST",
            f"{ctx.bff}/api/v1/conversations",
            {"mode": "global"},
            {"Authorization": "Bearer svc-e2e-obs-token", "X-Workspace-ID": WORKSPACE_ID},
        )
        expect(status == 401, f"service token on user API got {status}, want 401")
        results["service_identity"] = "PASS"
    finally:
        # Restore dev mode for the remaining (fault) scenarios.
        recreate_backend(
            ctx,
            {
                "MAP_AUTH_MODE": "dev",
                "MAP_TRUSTED_PROXY_SECRET": "",
                "MAP_SERVICE_CREDENTIALS": "",
            },
        )
    status, _ = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global"},
        {"X-Workspace-ID": WORKSPACE_ID},
    )
    expect(status == 201, f"dev mode not restored (create got {status})")
    results["dev_mode_restored"] = "PASS"
    ctx.report["identity"] = results
    return results


# ---------------------------------------------------------------------------
# fault matrix (R3-P1-02)
# ---------------------------------------------------------------------------


def scenario_bff_restart(ctx: Ctx, happy: dict) -> None:
    """BFF restart: durable state survives, streaming works again."""
    run(["docker", "restart", ctx.backend_container], timeout=300.0)

    def _healthy():
        info = compose_ps(ctx).get("backend-service") or {}
        return info.get("state") == "running" and info.get("health") == "healthy"

    poll_until(_healthy, timeout_s=300.0, what="backend-service healthy after restart")

    headers = {"X-Workspace-ID": WORKSPACE_ID}
    status, restored = http_json(
        "GET", f"{ctx.bff}/api/v1/conversations/{happy['conversation_id']}", None, headers
    )
    expect(status == 200, f"post-BFF-restart conversation restore got {status}")
    expect(
        any(m.get("id") == happy["message_id"] for m in restored.get("messages") or []),
        "happy-path message lost after BFF restart",
    )

    request_id = f"e2e-bffrestart-{secrets.token_hex(4)}"
    stream_headers = {**headers, "X-Request-ID": request_id}
    status, created = http_json(
        "POST",
        f"{ctx.bff}/api/v1/conversations",
        {"mode": "global", "title": "E2E BFF 重启恢复"},
        stream_headers,
    )
    expect(status == 201, f"post-BFF-restart create got {status}")
    sse = SseCollector(
        "127.0.0.1",
        ctx.ports["MAP_BFF_PORT"],
        f"/api/v1/conversations/{created['id']}/messages:stream",
        {"query": "重启后还能用吗", "request_id": request_id},
        stream_headers,
    ).start()
    expect(sse.finished.wait(90.0), "post-BFF-restart stream did not finish")
    expect(sse.status == 200, f"post-BFF-restart stream HTTP status {sse.status}")
    expect(
        sse.event_names()[-1] == "done",
        f"post-BFF-restart stream events: {sse.event_names()}",
    )
    ctx.report["request_ids"]["bff_restart"] = request_id
    ctx.report["faults"]["bff_restart_recovery"] = "PASS"


def scenario_pg_interruption(ctx: Ctx) -> None:
    """PostgreSQL pause: BFF fails closed during the outage, then recovers."""
    run(["docker", "pause", ctx.postgres_container], timeout=120.0)
    time.sleep(3.0)
    outage_rejected = False
    try:
        status, body = http_json(
            "POST",
            f"{ctx.bff}/api/v1/conversations",
            {"mode": "global"},
            {"X-Workspace-ID": WORKSPACE_ID},
            timeout=8.0,
        )
        # Any outcome is acceptable EXCEPT a silent success.
        outage_rejected = status >= 500
        expect(
            outage_rejected,
            f"BFF succeeded during PG outage: {status} {body}",
        )
    except E2EFailure:
        raise
    except Exception:  # noqa: BLE001 - connection reset/timeout = rejected
        outage_rejected = True
    expect(outage_rejected, "PG outage was not visible through the BFF")

    run(["docker", "unpause", ctx.postgres_container], timeout=120.0)

    def _pg_and_bff_healthy():
        services = compose_ps(ctx)
        pg = services.get("postgres") or {}
        bff = services.get("backend-service") or {}
        return (
            pg.get("state") == "running"
            and pg.get("health") == "healthy"
            and bff.get("state") == "running"
            and bff.get("health") == "healthy"
        )

    poll_until(_pg_and_bff_healthy, timeout_s=300.0, what="postgres + BFF healthy after unpause")

    # Recovery: the first post-outage create+stream may race with pool
    # reconnection, so poll until a full turn completes.
    request_id = f"e2e-pgrecover-{secrets.token_hex(4)}"
    headers = {"X-Workspace-ID": WORKSPACE_ID, "X-Request-ID": request_id}

    def _full_turn_after_outage():
        status, created = http_json(
            "POST", f"{ctx.bff}/api/v1/conversations", {"mode": "global"}, headers
        )
        if status != 201:
            return None
        sse = SseCollector(
            "127.0.0.1",
            ctx.ports["MAP_BFF_PORT"],
            f"/api/v1/conversations/{created['id']}/messages:stream",
            {"query": "数据库恢复了吗", "request_id": request_id},
            headers,
        ).start()
        if not sse.finished.wait(60.0) or sse.status != 200:
            return None
        return created["id"] if sse.event_names() and sse.event_names()[-1] == "done" else None

    poll_until(_full_turn_after_outage, timeout_s=180.0, what="full turn after PG recovery")
    ctx.report["request_ids"]["pg_recovery"] = request_id
    ctx.report["faults"]["pg_interruption_recovery"] = "PASS"


def scenario_worker_kill_takeover(ctx: Ctx, happy: dict) -> None:
    """Worker kill + lease takeover.

    A job is left RUNNING by a dead worker (expired lease); the real
    worker container is killed and restarted, and the restarted worker
    must reclaim the expired job (attempt bump proves the takeover) and
    finish it exactly once.
    """
    stale_id = psql(
        ctx,
        "INSERT INTO map_control.messages "
        "(conversation_id, workspace_id, role, status, content, version, updated_at) "
        f"VALUES ('{happy['conversation_id']}', '{WORKSPACE_ID}', 'assistant', "
        "'streaming', '', 1, now() - interval '1 hour') RETURNING id",
    )
    expect(bool(stale_id), "failed to insert stale streaming message")
    job_id = psql(
        ctx,
        "INSERT INTO map_control.jobs "
        "(workspace_id, job_type, status, priority, attempt, max_attempts, "
        "lease_owner, lease_expires_at, started_at) "
        f"VALUES ('{WORKSPACE_ID}', 'message_reconcile', 'running', 0, 1, 3, "
        "'dead-worker', now() + interval '2 seconds', now()) RETURNING id",
    )
    expect(bool(job_id), "failed to insert dead-worker job")

    # Kill the real worker mid-flight-window, then bring it back.
    run(["docker", "kill", ctx.worker_container], timeout=120.0)
    compose(ctx, ["up", "-d", "--no-deps", "worker-service"], timeout=600.0)

    def _worker_running():
        info = compose_ps(ctx).get("worker-service") or {}
        return info.get("state") == "running"

    poll_until(_worker_running, timeout_s=180.0, what="worker-service running after kill")

    def _job_succeeded():
        return (
            psql(ctx, f"SELECT status FROM map_control.jobs WHERE id = '{job_id}'")
            == "succeeded"
        )

    # Lease expiry (2s) + poll interval + container boot headroom.
    poll_until(_job_succeeded, timeout_s=120.0, what="lease takeover completes the job")

    lease_row = psql(
        ctx,
        "SELECT attempt || '|' || coalesce(lease_owner, '') "
        f"FROM map_control.jobs WHERE id = '{job_id}'",
    )
    attempt_str, owner = lease_row.split("|", 1)
    expect(int(attempt_str) >= 2, f"takeover did not bump attempt: {lease_row!r}")
    expect(owner == "", f"terminal job still holds a lease owner: {lease_row!r}")

    row = psql(
        ctx,
        "SELECT status || '|' || coalesce(stream_error, '') "
        f"FROM map_control.messages WHERE id = '{stale_id}'",
    )
    expect(
        row == "failed|STREAM_INTERRUPTED",
        f"taken-over job did not reconcile the stale message: {row!r}",
    )
    ctx.report["faults"]["worker_kill_lease_takeover"] = "PASS"
    ctx.report["faults"]["takeover_attempt"] = int(attempt_str)


def collect_db_counts(ctx: Ctx) -> None:
    ctx.report["db_counts"] = {
        "pg_conversations": psql(ctx, "SELECT count(*) FROM map_control.conversations"),
        "pg_messages": psql(ctx, "SELECT count(*) FROM map_control.messages"),
        "pg_audit_events": psql(ctx, "SELECT count(*) FROM map_control.config_audit_events"),
        "pg_outbox_events": psql(ctx, "SELECT count(*) FROM map_control.outbox_events"),
        "mongo_request_records": mongosh_eval(ctx, "db.request_records.countDocuments({})"),
        "mongo_llm_calls": mongosh_eval(ctx, "db.llm_call_records.countDocuments({})"),
        "mongo_agent_executions": mongosh_eval(ctx, "db.agent_executions.countDocuments({})"),
        "mongo_tool_call_records": mongosh_eval(ctx, "db.tool_call_records.countDocuments({})"),
    }
    status, stats = http_json("GET", f"{ctx.fake_llm}/__e2e/stats")
    ctx.report["fake_llm_calls"] = stats.get("by_kind") if status == 200 else None


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def start_stack(ctx: Ctx) -> None:
    port_env_keys = [
        "MAP_POSTGRES_PORT",
        "MAP_MONGO_PORT",
        "MAP_ALGO_PORT",
        "MAP_BFF_PORT",
        "MAP_FRONTEND_PORT",
        "MAP_OBS_BACKEND_PORT",
        "MAP_OBS_FRONTEND_PORT",
        "MAP_OTEL_GRPC_PORT",
        "MAP_OTEL_HTTP_PORT",
        "MAP_JAEGER_UI_PORT",
        "MAP_JAEGER_OTLP_GRPC_PORT",
        "E2E_FAKE_LLM_PORT",
    ]
    for key in port_env_keys:
        ctx.ports[key] = free_port()
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.env = {
        **{key: str(port) for key, port in ctx.ports.items()},
        "E2E_PREFIX": ctx.prefix,
        "E2E_DATA_DIR": str(ctx.data_dir),
        # E2E must never read credentials/data from the developer's .env.
        "MAP_LLM_API_KEY": "e2e-fake-key",
        "MAP_LLM_BASE_URL": "http://fake-llm:9999/v1",
        "MAP_LLM_MODEL": "fake-e2e-model",
    }
    ctx.bff = f"http://127.0.0.1:{ctx.ports['MAP_BFF_PORT']}"
    ctx.frontend = f"http://127.0.0.1:{ctx.ports['MAP_FRONTEND_PORT']}"
    ctx.jaeger = f"http://127.0.0.1:{ctx.ports['MAP_JAEGER_UI_PORT']}"
    ctx.fake_llm = f"http://127.0.0.1:{ctx.ports['E2E_FAKE_LLM_PORT']}"
    ctx.fake_llm_container = f"{ctx.prefix}-fake-llm"

    log(f"project={ctx.project} bff={ctx.bff} jaeger_port={ctx.ports['MAP_JAEGER_UI_PORT']}")
    compose(ctx, ["up", "-d", "--build", *SERVICES], timeout=1800.0)

    def _all_ready():
        services = compose_ps(ctx)
        for name in HEALTHY_REQUIRED:
            info = services.get(name) or {}
            if info.get("state") != "running" or info.get("health") != "healthy":
                return False
        for name in RUNNING_REQUIRED - HEALTHY_REQUIRED:
            info = services.get(name) or {}
            if info.get("state") != "running":
                return False
        return True

    poll_until(_all_ready, timeout_s=HEALTH_TIMEOUT_S, what="all services healthy")
    log("stack is healthy")

    # frontend-service has no docker healthcheck (vite dev server); the
    # container also runs npm ci on a fresh volume, so poll HTTP instead.
    def _frontend_serving():
        status, _ = http_json("GET", f"{ctx.frontend}/", timeout=10.0)
        return status == 200

    poll_until(
        _frontend_serving,
        timeout_s=FRONTEND_READY_TIMEOUT_S,
        what="frontend vite dev server serving /",
    )
    log(f"frontend is serving at {ctx.frontend}")


def stop_stack(ctx: Ctx) -> None:
    try:
        compose(ctx, ["down", "-v", "--remove-orphans", "--timeout", "30"], check=False, timeout=600.0)
    finally:
        shutil.rmtree(ctx.data_dir, ignore_errors=True)


def dump_failure_logs(ctx: Ctx) -> str | None:
    """On failure, persist compose logs BEFORE the stack is torn down so CI
    can upload them together with the report and trace IDs."""
    logs_path = REPO_ROOT / "e2e" / "tmp" / f"logs-{ctx.project}.txt"
    try:
        result = compose(ctx, ["logs", "--no-color", "--tail", "500"], check=False, timeout=600.0)
        logs_path.parent.mkdir(parents=True, exist_ok=True)
        logs_path.write_text(
            f"# compose logs for {ctx.project}\n{result.stdout}\n{result.stderr}",
            encoding="utf-8",
        )
        log(f"failure logs written to {logs_path}")
        return str(logs_path)
    except Exception as exc:  # noqa: BLE001 - best effort
        log(f"failed to dump compose logs: {exc!r}")
        return None


def print_report(ctx: Ctx, success: bool, failure: str | None) -> None:
    ctx.report["duration_s"] = round(time.time() - ctx.started, 1)
    ctx.report["result"] = "PASS" if success else "FAIL"
    if failure:
        ctx.report["failure"] = failure
    report_path = REPO_ROOT / "e2e" / "tmp" / f"report-{ctx.project}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(ctx.report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"E2E REPORT — {ctx.report['result']}  (project={ctx.project})")
    print("=" * 72)
    print(json.dumps(ctx.report, ensure_ascii=False, indent=2))
    print("=" * 72)
    print(f"report file: {report_path}")
    if failure:
        print(f"FAILURE: {failure}")


def run_pr_suite(ctx: Ctx) -> None:
    """Stable CI subset: browser happy path + identity boundary."""
    scenario_versions(ctx)
    scenario_model_center_redirect(ctx)
    ctx.report["scenarios"]["model_center_redirect"] = "PASS"
    happy = scenario_happy_path(ctx)
    ctx.report["request_ids"]["happy"] = happy["request_id"]
    ctx.report["happy_ids"] = {
        key: happy[key]
        for key in ("conversation_id", "message_id", "request_id", "session_id", "trace_id")
    }
    ctx.report["final_states"]["happy_message"] = "completed"
    ctx.report["scenarios"]["happy_path"] = "PASS"
    log(f"happy path OK (request_id={happy['request_id']}, trace_id={happy['trace_id']})")

    scenario_browser(ctx)
    ctx.report["scenarios"]["browser"] = "PASS"
    log("browser E2E OK (create/stream/reload/stop/feedback/withdraw)")

    scenario_identity_boundary(ctx)
    ctx.report["scenarios"]["identity_boundary"] = "PASS"
    log("secure identity boundary OK")


def run_full_suite(ctx: Ctx) -> None:
    run_pr_suite(ctx)
    run_fault_and_legacy_scenarios(ctx)


def run_fault_and_legacy_scenarios(ctx: Ctx) -> None:
    """Full-suite additions: fault matrix + the original HTTP scenarios."""
    happy = ctx.report["happy_ids"]

    scenario_duplicate_request(ctx, happy)
    ctx.report["scenarios"]["duplicate_request_replay"] = "PASS"
    log("duplicate request_id replay OK")

    stopped = scenario_stop_mid_stream(ctx)
    ctx.report["request_ids"]["stop"] = stopped["request_id"]
    ctx.report["final_states"]["stop_message"] = "stopped"
    ctx.report["scenarios"]["stop_mid_stream"] = "PASS"
    log("stop/abort mid-stream OK")

    scenario_bff_restart(ctx, happy)
    ctx.report["scenarios"]["bff_restart_recovery"] = "PASS"
    log("BFF restart recovery OK")

    scenario_pg_interruption(ctx)
    ctx.report["scenarios"]["pg_interruption_recovery"] = "PASS"
    log("PostgreSQL interruption + recovery OK")

    scenario_worker_kill_takeover(ctx, happy)
    ctx.report["scenarios"]["worker_kill_lease_takeover"] = "PASS"
    log("worker kill + lease takeover OK")

    restarted = scenario_core_restart(ctx)
    ctx.report["request_ids"]["restart"] = restarted["request_id"]
    ctx.report["scenarios"]["core_restart_recovery"] = "PASS"
    log("core restart recovery OK")

    scenario_feedback_and_audit(ctx, happy)
    ctx.report["scenarios"]["feedback_withdraw_audit"] = "PASS"
    log("feedback + outbox worker + audit chain OK")

    scenario_worker_reconcile(ctx, happy)
    ctx.report["scenarios"]["worker_reconcile"] = "PASS"
    log("worker claim + message_reconcile OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="MAP Compose E2E runner")
    parser.add_argument(
        "--suite",
        choices=["full", "pr"],
        default="full",
        help="pr = browser happy path + identity boundary (CI); "
        "full = everything incl. the fault matrix (default)",
    )
    args = parser.parse_args()

    ctx = Ctx()
    ctx.report["suite"] = args.suite
    failure: str | None = None
    try:
        start_stack(ctx)
        if args.suite == "pr":
            run_pr_suite(ctx)
        else:
            run_full_suite(ctx)
        collect_db_counts(ctx)
    except E2EFailure as exc:
        failure = str(exc)
        log(f"FAILED: {exc}")
    except Exception as exc:  # noqa: BLE001 - report anything, then clean up
        failure = f"unexpected error: {exc!r}"
        log(f"FAILED (unexpected): {exc!r}")
    finally:
        if failure is not None:
            ctx.report["compose_logs"] = dump_failure_logs(ctx)
        log("cleaning up (docker compose down -v --remove-orphans)...")
        stop_stack(ctx)

    print_report(ctx, failure is None, failure)
    return 0 if failure is None else 1


if __name__ == "__main__":
    sys.exit(main())
