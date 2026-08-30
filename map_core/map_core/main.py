import asyncio
import json
import os
import re
import sys
import uuid
from argparse import ArgumentParser
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config.base_config import LOG_DIR
from .utils.global_context import request_id_ctx, session_id_ctx

_PACKAGE_DIR = str(Path(__file__).parent.absolute())
_PROJECT_ROOT = str(Path(__file__).parents[1].absolute())
MAX_LOGGED_REQUEST_BODY_CHARS = 13000
MAX_LOGGED_JSON_FIELD_CHARS = 5000
LOG_REDACTED_VALUE = "***REDACTED***"

# Shared ID contract with the routers and the BFF (F-04): non-empty,
# <=128 chars, restricted charset; illegal/missing request ids are replaced
# with uuid4().hex, missing session ids stay None.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _validated_id_header(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned and _ID_PATTERN.fullmatch(cleaned):
        return cleaned
    return None
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "secret",
    "password",
    "passwd",
    "access_key",
    "private_key",
    "credential",
)

for _path in (_PACKAGE_DIR, _PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _resolve_env() -> str:
    """S3-04: ONE frozen environment variable across services - MAP_ENV.

    ENV is accepted as a fallback for pre-unification deployments, then
    both names are kept in sync so legacy readers (config, routers) see
    the same value.
    """
    env = os.environ.get("MAP_ENV") or os.environ.get("ENV") or "dev"
    os.environ["MAP_ENV"] = env
    os.environ["ENV"] = env
    return env


def _ensure_env() -> str:
    env = _resolve_env()
    if not os.environ.get("MAP_ENV"):
        print(
            "[main] MAP_ENV 未设置，默认使用 'dev'。如需指定环境: `MAP_ENV=prod`",
            file=sys.stderr,
        )
    return env


def _truncate_top_level_json_fields(body_json: object) -> object:
    def _is_sensitive_key(raw_key: str) -> bool:
        key = raw_key.strip().lower().replace("-", "_")
        if key.endswith("_tokens"):
            return False
        if key in {
            "token_usage",
            "token_total",
            "max_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "top_logprobs",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        }:
            return False
        return any(keyword in key for keyword in SENSITIVE_KEYWORDS)

    def _sanitize_value(value: object, current_key: str | None = None) -> object:
        if current_key is not None and _is_sensitive_key(current_key):
            return LOG_REDACTED_VALUE

        if isinstance(value, str):
            return value[:MAX_LOGGED_JSON_FIELD_CHARS]

        if isinstance(value, list):
            sanitized_list = [
                _sanitize_value(item, current_key=current_key) for item in value
            ]
            value_text = json.dumps(sanitized_list, ensure_ascii=False)
            if len(value_text) > MAX_LOGGED_JSON_FIELD_CHARS:
                return {
                    "_truncated": True,
                    "_type": "list",
                    "_original_chars": len(value_text),
                    "_preview": value_text[:MAX_LOGGED_JSON_FIELD_CHARS],
                }
            return sanitized_list

        if isinstance(value, dict):
            sanitized_dict = {
                str(k): _sanitize_value(v, current_key=str(k))
                for k, v in value.items()
            }
            value_text = json.dumps(sanitized_dict, ensure_ascii=False)
            if len(value_text) > MAX_LOGGED_JSON_FIELD_CHARS:
                return {
                    "_truncated": True,
                    "_type": "dict",
                    "_original_chars": len(value_text),
                    "_preview": value_text[:MAX_LOGGED_JSON_FIELD_CHARS],
                }
            return sanitized_dict

        return value

    if not isinstance(body_json, dict):
        return _sanitize_value(body_json)

    truncated_body: dict[str, object] = {}
    for key, value in body_json.items():
        truncated_body[str(key)] = _sanitize_value(value, current_key=str(key))

    return truncated_body


def _sanitize_headers_for_logging(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"authorization", "cookie", "x-api-key"}:
            sanitized[key] = LOG_REDACTED_VALUE
            continue
        sanitized[key] = value
    return sanitized


def load_config():
    _ensure_env()
    cfg = import_module("map_core.config")
    cfg.load_actual_config()
    return cfg


def _resolve_default_workers() -> int:
    env = _ensure_env()
    module_name = {
        "dev": "map_core.config.dev",
        "test": "map_core.config.test",
        "pre": "map_core.config.pre",
        "prod": "map_core.config.prod",
    }.get(env)
    if module_name is None:
        raise NotImplementedError(
            f"未实现针对环境 {env} 的配置。请检查 map_core/config 目录下是否有对应文件。"
        )

    env_module = import_module(module_name)
    return int(getattr(env_module, "NUM_WORK", 1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = _ensure_env()
    cfg = load_config()
    app.state.cfg = cfg

    from .observability import configure_telemetry, shutdown_telemetry

    configure_telemetry(
        service_name="map-core",
        service_version=app.version,
        deployment_environment=env,
    )

    from .database.mongodb import setup_mongodb
    from .database.postgre import setup_postgres

    # FastAPI >= 0.141 removed `add_event_handler`, so startup connectivity
    # verification and shutdown cleanup are driven explicitly from lifespan.
    pg_client = setup_postgres(app, config=cfg.POSTGRES_CONFIG)
    await pg_client.verify_startup()

    # Step 8 PR-K8: Mongo is optional at boot.  The only remaining core
    # Mongo consumer is agent memory; without a configured URI (or when the
    # ping fails) the app must still boot, and Mongo-backed adapters simply
    # degrade per-request.
    mongo_client = setup_mongodb(app, config=cfg.MONGODB_CONFIG)
    if mongo_client is not None and not await mongo_client.verify_startup():
        delattr(app.state, "mongodb_client")
        mongo_client = None

    from .utils.map_logger import init_logger

    app.state.logger = init_logger(path=LOG_DIR)
    logger.info(f"[PID: {os.getpid()}] application start. ENV='{env}'")

    from .routers.execution_router import execution_router
    from .routers.flow_domain_router import flow_domain_router
    from .routers.global_domain_router import global_domain_router
    from .routers.master_pipeline_router import master_pipeline_router
    from .routers.openapi_router import openapi_router
    from .routers.sandbox_router import sandbox_router
    from .routers.system_router import system_router
    from .service import sandbox_tools

    app.include_router(system_router)
    app.include_router(global_domain_router)
    app.include_router(flow_domain_router)
    app.include_router(master_pipeline_router)
    app.include_router(openapi_router)
    app.include_router(sandbox_router)
    app.include_router(execution_router)

    # S5-01: the durable OpenSandbox reconciler converges crashed
    # non-terminal invocations (owner died between a remote create/execute
    # and the ledger write). It runs in every Core process; the takeover
    # CAS in the ledger guarantees exactly one process drives each row.
    # Started best-effort: without POSTGRES_DSN it stays a no-op and never
    # blocks startup.
    sandbox_reconciler = sandbox_tools.create_sandbox_reconciler()
    sandbox_reconciler_stop: asyncio.Event | None = None
    sandbox_reconciler_task: asyncio.Task | None = None
    if sandbox_reconciler is not None:
        sandbox_reconciler_stop = asyncio.Event()
        sandbox_reconciler_task = asyncio.create_task(
            sandbox_reconciler.run_forever(sandbox_reconciler_stop)
        )
        app.state.sandbox_reconciler_task = sandbox_reconciler_task
        app.state.sandbox_reconciler_stop = sandbox_reconciler_stop
        logger.info(
            "[PID: {}] SandboxInvocation reconciler started.", os.getpid()
        )

    try:
        yield
    finally:
        # S5-01: stop the reconciler first, then close the sandbox ledger
        # pool so a stopping process never leaks pooled connections.
        if sandbox_reconciler_task is not None:
            if sandbox_reconciler_stop is not None:
                sandbox_reconciler_stop.set()
            sandbox_reconciler_task.cancel()
            try:
                await sandbox_reconciler_task
            except asyncio.CancelledError:
                pass
        await sandbox_tools.close_sandbox_ledger()

        pg_client = getattr(app.state, "postgres_client", None)
        if pg_client is not None:
            await pg_client.close()
        mongo_client = getattr(app.state, "mongodb_client", None)
        if mongo_client is not None:
            await mongo_client.close()

        shutdown_telemetry()

        # Ensure loguru queued sinks (`enqueue=True`) flush and release IPC resources.
        app_logger = getattr(app.state, "logger", None)
        if app_logger is not None:
            app_logger.complete()
            app_logger.remove()


def build_cors_kwargs(env: str | None = None) -> dict:
    """S2-07: build the CORS middleware kwargs from the SHARED policy.

    The same contract as the BFF (MAP_CORS_ORIGINS / MAP_CORS_ALLOW_
    CREDENTIALS): malformed origins and production wildcard+credentials
    fail closed here, at startup, before any request can be served.
    """
    from .utils.cors_policy import load_cors_policy

    policy = load_cors_policy(env)
    return {
        "allow_origins": list(policy.origins),
        "allow_credentials": policy.allow_credentials,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


app = FastAPI(
    title="MAP 2.0",
    description="MAP 2.0 Service",
    version="0.0.1",
    lifespan=lifespan,
)


class RequestContextMiddleware:
    """Attach request/session ids without Starlette BaseHTTPMiddleware.

    BaseHTTPMiddleware can turn downstream streaming/disconnect failures into
    RuntimeError("No response returned."), hiding the original exception.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        rid = _validated_id_header(headers.get("X-Request-ID")) or uuid.uuid4().hex
        sid = _validated_id_header(headers.get("X-Session-ID"))
        path = str(scope.get("path") or "")
        request_log_enabled = path != "/health"
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        state["session_id"] = sid
        state["client_disconnected"] = False
        state["_response_started"] = False
        state["_response_completed"] = False
        state["_stream_logically_completed"] = False
        state["_client_disconnect_before_response_start"] = False

        def mark_client_disconnected() -> None:
            state["client_disconnected"] = True
            if not state["_response_started"]:
                state["_client_disconnect_before_response_start"] = True

        def log_client_disconnected_if_needed() -> None:
            if not state["client_disconnected"]:
                return
            if state["_client_disconnect_before_response_start"]:
                should_log = True
            else:
                should_log = not (
                    state["_response_completed"]
                    or state["_stream_logically_completed"]
                )
            if not should_log:
                return
            logger.info(
                "HTTP client disconnected | method={} path={} request_id={} session_id={}",
                scope.get("method", ""),
                path,
                rid,
                sid,
            )

        replay_receive = receive
        if request_log_enabled:
            body, disconnected = await self._read_request_body(receive)
            replay_receive = self._replay_request_body(body, receive, disconnected)
            if disconnected:
                mark_client_disconnected()
            request_body_text = body.decode("utf-8", errors="replace")
            try:
                body_json = json.loads(request_body_text)
                request_body_text = json.dumps(
                    _truncate_top_level_json_fields(body_json), ensure_ascii=False
                )
            except json.JSONDecodeError:
                request_body_text = request_body_text[
                    :MAX_LOGGED_REQUEST_BODY_CHARS
                ]
            logger.info(
                "Incoming request | method={} path={} query={} headers={} body={}",
                scope.get("method", ""),
                path,
                scope.get("query_string", b"").decode("latin-1"),
                _sanitize_headers_for_logging(dict(headers)),
                request_body_text,
            )

        request_id_token = request_id_ctx.set(rid)
        session_id_token = session_id_ctx.set(sid)

        async def receive_with_disconnect_tracking() -> Message:
            message = await replay_receive()
            if message["type"] == "http.disconnect":
                mark_client_disconnected()
            return message

        async def send_with_request_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["_response_started"] = True
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = rid
                if sid:
                    response_headers["X-Session-ID"] = sid
            elif message["type"] == "http.response.body" and not message.get(
                "more_body",
                False,
            ):
                state["_response_completed"] = True
            await send(message)

        try:
            await self.app(
                scope,
                receive_with_disconnect_tracking,
                send_with_request_headers,
            )
        finally:
            log_client_disconnected_if_needed()
            state.pop("_response_started", None)
            state.pop("_response_completed", None)
            state.pop("_stream_logically_completed", None)
            state.pop("_client_disconnect_before_response_start", None)
            request_id_ctx.reset(request_id_token)
            session_id_ctx.reset(session_id_token)

    @staticmethod
    async def _read_request_body(receive: Receive) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks), disconnected

    @staticmethod
    def _replay_request_body(
        body: bytes,
        receive: Receive,
        disconnected: bool,
    ) -> Receive:
        body_sent = False
        disconnect_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent, disconnect_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnected and not disconnect_sent:
                disconnect_sent = True
                return {"type": "http.disconnect"}
            return await receive()

        return replay_receive


app.add_middleware(
    CORSMiddleware,  # S2-07: shared policy via build_cors_kwargs (fail-closed)
    **build_cors_kwargs(_ensure_env()),
)
app.add_middleware(RequestContextMiddleware)
# OTel SERVER span middleware wraps the whole request lifecycle (outermost).
from .observability import OpenTelemetryASGIMiddleware  # noqa: E402

app.add_middleware(OpenTelemetryASGIMiddleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or request_id_ctx.get(None)
    sid = getattr(request.state, "session_id", None) or session_id_ctx.get(None)
    headers = {}
    if rid:
        headers["X-Request-ID"] = rid
    if sid:
        headers["X-Session-ID"] = sid
    if exc.headers:
        headers.update(exc.headers)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception", exc_info=exc)
    rid = getattr(request.state, "request_id", None) or request_id_ctx.get(None)
    sid = getattr(request.state, "session_id", None) or session_id_ctx.get(None)
    headers = {}
    if rid:
        headers["X-Request-ID"] = rid
    if sid:
        headers["X-Session-ID"] = sid
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
        headers=headers,
    )


def cli_main(argv: list[str] | None) -> None:
    """
    Preferred:
        `ENV=dev uvicorn map_core.main:app --host 0.0.0.0 --port 10000 --workers 2`

    Also supported:
        `python -m map_core.main --env dev --port 10000 --workers 2`
    """

    parser = ArgumentParser(description="运行 MAP 2.0 应用服务。")
    parser.add_argument(
        "--env", type=str, help="指定运行环境 (例如: dev, test, pre, prod)。"
    )
    parser.add_argument(
        "--port", type=int, default=10000, help="指定服务运行端口。默认为 10000。"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="指定服务运行主机。默认为 0.0.0.0。"
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="指定工作进程数。如果不指定，将使用配置文件中的 NUM_WORK。",
    )
    args = parser.parse_args(argv)

    if args.env:
        os.environ["MAP_ENV"] = args.env
        os.environ["ENV"] = args.env

    default_workers = _resolve_default_workers()
    workers = args.workers if args.workers is not None else default_workers

    import uvicorn

    # Use import-string so `--workers > 1` works reliably.
    uvicorn.run(
        "map_core.main:app",
        host=str(args.host),
        port=int(args.port),
        workers=int(workers),
        timeout_keep_alive=200,
        access_log=False,
    )


if __name__ == "__main__":
    cli_main(sys.argv[1:])
