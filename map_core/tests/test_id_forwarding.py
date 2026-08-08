"""F-04 contract tests: unified request/session/workspace id resolution in map_core.

Covers the three map_core routers' ``_apply_runtime_headers`` behavior plus the
MasterPipeline consumption of the resolved ids:

- valid X-Request-ID / X-Session-ID / X-Workspace-ID headers are honored and
  attached to ``request.state``;
- invalid ids (over-long or containing characters outside ``[A-Za-z0-9._:-]``)
  are ignored: request_id falls back to a fresh ``uuid4().hex`` while
  session_id and workspace_id become None;
- missing headers: request_id is generated, session_id/workspace_id stay None;
- existing X-UserId / X-UserName resolution is preserved unchanged.
"""

from __future__ import annotations

import re

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from map_core.routers import flow_domain_router, global_domain_router, master_pipeline_router
from map_core.service.master_pipeline import MasterPipeline

UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")

# Each router must apply the exact same id resolution contract.
ROUTER_APPLY_FN = [
    ("global_domain", global_domain_router._apply_runtime_headers),
    ("master_pipeline", master_pipeline_router._apply_runtime_headers),
    ("flow_domain", flow_domain_router._apply_runtime_headers),
]


def _make_http_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": Headers(headers).raw,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 8000),
        "state": {},
        "app": None,
    }
    return Request(scope)


@pytest.mark.parametrize("name,apply_fn", ROUTER_APPLY_FN)
def test_valid_ids_are_forwarded_to_state(name: str, apply_fn) -> None:
    req = _make_http_request(
        {
            "X-Request-ID": "req-123.abc:def_1",
            "X-Session-ID": "sess_456.xyz:1",
            "X-Workspace-ID": "ws:team-7",
            "X-UserId": "user-1",
            "X-UserName": "zhang-san",
        }
    )
    apply_fn(req, request_token=None)

    assert req.state.request_id == "req-123.abc:def_1"
    assert req.state.session_id == "sess_456.xyz:1"
    assert req.state.workspace_id == "ws:team-7"
    # existing user headers are preserved unchanged.
    assert req.state.x_userid == "user-1"
    assert req.state.x_username == "zhang-san"
    assert req.state.request_token is None


@pytest.mark.parametrize("name,apply_fn", ROUTER_APPLY_FN)
def test_invalid_ids_are_ignored(name: str, apply_fn) -> None:
    req = _make_http_request(
        {
            "X-Request-ID": "bad id!@#$%^",
            "X-Session-ID": "x" * 200,  # over length 128
            "X-Workspace-ID": "workspace id with space",
            "X-UserId": "user-1",
            "X-UserName": "li-si",
        }
    )
    apply_fn(req, request_token=None)

    assert UUID4_HEX.fullmatch(req.state.request_id), req.state.request_id
    assert req.state.session_id is None
    assert req.state.workspace_id is None
    assert req.state.x_userid == "user-1"
    assert req.state.x_username == "li-si"


@pytest.mark.parametrize("name,apply_fn", ROUTER_APPLY_FN)
def test_missing_ids_generate_request_id_only(name: str, apply_fn) -> None:
    req = _make_http_request({})
    apply_fn(req, request_token=None)

    assert UUID4_HEX.fullmatch(req.state.request_id), req.state.request_id
    assert req.state.session_id is None
    assert req.state.workspace_id is None


@pytest.mark.parametrize("name,apply_fn", ROUTER_APPLY_FN)
def test_partial_ids_session_valid_request_generated(name: str, apply_fn) -> None:
    # Only session/workspace provided; request_id must still be generated.
    req = _make_http_request(
        {
            "X-Session-ID": "sess-ok",
            "X-Workspace-ID": "ws-ok",
        }
    )
    apply_fn(req, request_token=None)

    assert UUID4_HEX.fullmatch(req.state.request_id), req.state.request_id
    assert req.state.session_id == "sess-ok"
    assert req.state.workspace_id == "ws-ok"


def test_master_pipeline_consumes_resolved_state_ids() -> None:
    req = _make_http_request(
        {
            "X-Request-ID": "req-1",
            "X-Session-ID": "sess-1",
            "X-Workspace-ID": "ws-1",
        }
    )
    master_pipeline_router._apply_runtime_headers(req, request_token=None)

    master = MasterPipeline(request=None, http_request=req, tool_registry={})
    assert master.request_id == "req-1"
    assert master.session_id == "sess-1"
    assert master.workspace_id == "ws-1"
    assert master.base_state["request_id"] == "req-1"
    assert master.base_state["session_id"] == "sess-1"
    assert master.base_state["workspace_id"] == "ws-1"


def test_master_pipeline_consumes_generated_request_id() -> None:
    req = _make_http_request({})
    master_pipeline_router._apply_runtime_headers(req, request_token=None)

    master = MasterPipeline(request=None, http_request=req, tool_registry={})
    assert UUID4_HEX.fullmatch(master.request_id), master.request_id
    assert master.session_id is None
    assert master.workspace_id is None
    assert master.base_state["workspace_id"] is None
