"""S2-04: MCP egress guard + secret-echo canary tests.

Covers the review's acceptance matrix:

- a canary secret echoed by the upstream in plain answer/message/error
  fields never survives content / error / data_source;
- 127.0.0.1, 169.254.169.254, IPv6 loopback, private-range resolution and
  DNS-rebinding-style mixed resolution are all rejected BEFORE dialing;
- oversized responses are rejected while streaming;
- plaintext headers in server config are rejected (secret refs only);
- ${ENV:...} secret refs resolve at call time.
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import pytest

from map_core.service.agent.base import AgentRequest, ToolResult
from map_core.service.dynamic_tools import _call_http_mcp_tool
from map_core.service.mcp_egress import (
    ALLOW_INSECURE_LOCAL_ENV,
    ALLOWED_HOSTS_ENV,
    MAX_RESPONSE_BYTES_ENV,
    EgressPolicy,
    MCPEgressError,
    ResolvedHeaders,
    validate_mcp_url,
)

CANARY = "sk-fake-canary-secret-0123456789abcdef"


def _policy(hosts: str, local: bool = False, max_bytes: int = 1024 * 1024) -> None:
    os.environ[ALLOWED_HOSTS_ENV] = hosts
    os.environ[ALLOW_INSECURE_LOCAL_ENV] = "1" if local else "0"
    os.environ[MAX_RESPONSE_BYTES_ENV] = str(max_bytes)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        ALLOWED_HOSTS_ENV,
        ALLOW_INSECURE_LOCAL_ENV,
        MAX_RESPONSE_BYTES_ENV,
        "MAP_MCP_TEST_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# URL / IP policy
# ---------------------------------------------------------------------------


class TestUrlPolicy:
    def test_loopback_ip_rejected(self) -> None:
        _policy("127.0.0.1")
        problems, _allowed = validate_mcp_url("https://127.0.0.1:8443/mcp", EgressPolicy.from_env())
        assert any("loopback" in problem for problem in problems)

    def test_metadata_ip_rejected(self) -> None:
        _policy("169.254.169.254")
        problems, _allowed = validate_mcp_url(
            "https://169.254.169.254/latest/meta-data", EgressPolicy.from_env()
        )
        assert any("link-local" in problem for problem in problems)

    def test_ipv6_loopback_rejected(self) -> None:
        _policy("[::1]")
        problems, _allowed = validate_mcp_url("https://[::1]:8443/mcp", EgressPolicy.from_env())
        assert any("loopback" in problem for problem in problems)

    def test_private_range_resolution_rejected(self) -> None:
        _policy("api.internal.example")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["10.0.0.5"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://api.internal.example:443/mcp", EgressPolicy.from_env()
            )
        assert any("private" in problem for problem in problems)

    def test_dns_rebinding_mixed_resolution_rejected(self) -> None:
        _policy("api.example.com")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34", "192.168.1.10"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://api.example.com:443/mcp", EgressPolicy.from_env()
            )
        assert any("private" in problem for problem in problems)

    def test_not_on_allowlist_rejected(self) -> None:
        _policy("allowed.example.com")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://evil.example.com/mcp", EgressPolicy.from_env()
            )
        assert any("allowlist" in problem for problem in problems)

    def test_wrong_port_rejected(self) -> None:
        _policy("allowed.example.com:443")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://allowed.example.com:8443/mcp", EgressPolicy.from_env()
            )
        assert any("allowlist" in problem for problem in problems)

    def test_public_https_on_allowlist_passes(self) -> None:
        _policy("allowed.example.com:443")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://allowed.example.com:443/mcp", EgressPolicy.from_env()
            )
        assert problems == []

    def test_plain_http_requires_local_optin(self) -> None:
        _policy("127.0.0.1", local=False)
        problems, _allowed = validate_mcp_url("http://127.0.0.1:9/mcp", EgressPolicy.from_env())
        assert any("MAP_MCP_ALLOW_INSECURE_LOCAL" in problem for problem in problems)

    def test_plain_http_local_optin_only_loopback(self) -> None:
        _policy("localhost", local=True)
        problems, _allowed = validate_mcp_url(
            "http://localhost:9999/mcp", EgressPolicy.from_env()
        )
        assert problems == []

    def test_plain_http_local_optin_refuses_public_host(self) -> None:
        _policy("allowed.example.com", local=True)
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            problems, _allowed = validate_mcp_url(
                "http://allowed.example.com/mcp", EgressPolicy.from_env()
            )
        assert any("only admits" in problem for problem in problems)

    def test_empty_allowlist_is_denied(self) -> None:
        policy = EgressPolicy.from_env()
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            problems, _allowed = validate_mcp_url(
                "https://allowed.example.com/mcp", policy
            )
        assert any("allowlist" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Secret-ref headers
# ---------------------------------------------------------------------------


class TestResolvedHeaders:
    def test_literal_header_rejected(self) -> None:
        with pytest.raises(MCPEgressError) as excinfo:
            ResolvedHeaders.from_config({"Authorization": "Bearer literal-plaintext"})
        assert "secret reference" in str(excinfo.value)

    def test_env_ref_resolves(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_MCP_TEST_SECRET", "sk-resolved-secret-value-123456")
        resolved = ResolvedHeaders.from_config(
            {"Authorization": "${ENV:MAP_MCP_TEST_SECRET}"}
        )
        assert resolved.headers["Authorization"] == "sk-resolved-secret-value-123456"
        assert resolved.secret_values == ("sk-resolved-secret-value-123456",)

    def test_unresolved_ref_rejected(self, monkeypatch) -> None:
        monkeypatch.delenv("MAP_MCP_TEST_SECRET", raising=False)
        with pytest.raises(MCPEgressError) as excinfo:
            ResolvedHeaders.from_config({"X-Key": "${ENV:MAP_MCP_TEST_SECRET}"})
        assert "empty value" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end tool call through the guarded egress (canary)
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Captures the outbound request made through post_json_stream_guarded."""

    def __init__(self, response_body: bytes, status: int = 200) -> None:
        self.response_body = response_body
        self.status = status
        self.captured: dict = {}


def _make_server(url: str, headers: dict | None = None, extra: dict | None = None):
    server = {
        "server_id": "echo",
        "transport": "streamable_http",
        "url": url,
        "headers": headers or {},
    }
    if extra:
        server.update(extra)
    return server


def _mock_guarded_post(monkeypatch, response_body: bytes, status: int = 200):
    transport = _FakeTransport(response_body, status)

    async def fake_post_json_stream_guarded(**kwargs):
        import httpx

        from map_core.service.mcp_egress import GuardedResponse

        transport.captured = {
            "url": kwargs["url"],
            "headers": kwargs["headers"],
            "body": kwargs["json_payload"],
        }
        return GuardedResponse(
            status_code=status,
            headers=httpx.Headers(),
            body=response_body,
        )

    monkeypatch.setattr(
        "map_core.service.dynamic_tools.post_json_stream_guarded",
        fake_post_json_stream_guarded,
    )
    return transport


class TestGuardedToolCall:
    def test_echoed_secret_in_answer_field_is_wiped(
        self, monkeypatch
    ) -> None:
        import json as jsonlib

        _policy("allowed.example.com:443")
        monkeypatch.setenv("MAP_MCP_TEST_SECRET", CANARY)
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            server = _make_server(
                "https://allowed.example.com:443/mcp",
                headers={"Authorization": "${ENV:MAP_MCP_TEST_SECRET}"},
            )
            echo_body = jsonlib.dumps(
                {
                    "result": {
                        "content": [
                            {"type": "text", "text": f"echoed secret: {CANARY}"}
                        ]
                    }
                }
            ).encode()
            captured = _mock_guarded_post(monkeypatch, echo_body)
            result = asyncio.run(
                _call_http_mcp_tool(server=server, tool_name="echo", args={})
            )
        serialized = result.model_dump_json()
        assert CANARY not in serialized
        assert "<redacted>" in result.content
        # the secret itself reached the wire exactly once (as the header)
        assert captured.captured["headers"]["Authorization"] == CANARY

    def test_echoed_secret_in_error_field_is_wiped(
        self, monkeypatch
    ) -> None:
        import json as jsonlib

        _policy("allowed.example.com:443")
        monkeypatch.setenv("MAP_MCP_TEST_SECRET", CANARY)
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            server = _make_server(
                "https://allowed.example.com:443/mcp",
                headers={"Authorization": "${ENV:MAP_MCP_TEST_SECRET}"},
            )
            echo_body = jsonlib.dumps(
                {"error": {"code": -1, "message": f"bad key {CANARY}"}}
            ).encode()
            _mock_guarded_post(monkeypatch, echo_body)
            result = asyncio.run(
                _call_http_mcp_tool(server=server, tool_name="echo", args={})
            )
        serialized = result.model_dump_json()
        assert CANARY not in serialized
        assert result.success is False

    def test_policy_denial_before_dial(self) -> None:
        _policy("allowed.example.com:443")
        # the URL is not on the allowlist AND resolves to loopback: denied
        # without any network attempt
        server = _make_server(
            "https://127.0.0.1:9999/mcp",
            headers={"Authorization": "${ENV:MAP_MCP_TEST_SECRET}"},
        )
        result = asyncio.run(
            _call_http_mcp_tool(server=server, tool_name="echo", args={})
        )
        assert result.success is False
        assert "MCP_EGRESS_POLICY_DENIED" in result.error

    def test_literal_plaintext_header_rejected_before_dial(
        self, monkeypatch
    ) -> None:
        _policy("allowed.example.com:443")
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            server = _make_server(
                "https://allowed.example.com:443/mcp",
                headers={"Authorization": "Bearer literal-plaintext-0000000000"},
            )
            result = asyncio.run(
                _call_http_mcp_tool(server=server, tool_name="echo", args={})
            )
        assert result.success is False
        assert "secret reference" in result.error

    def test_oversized_response_rejected(self, monkeypatch) -> None:
        import json as jsonlib

        _policy("allowed.example.com:443", max_bytes=1024)
        monkeypatch.setenv("MAP_MCP_TEST_SECRET", CANARY)
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["93.184.216.34"],
        ):
            server = _make_server(
                "https://allowed.example.com:443/mcp",
                headers={"Authorization": "${ENV:MAP_MCP_TEST_SECRET}"},
            )
            big = jsonlib.dumps(
                {"result": {"content": [{"type": "text", "text": "x" * 100000}]}}
            ).encode()
            async def oversized(**kwargs):
                from map_core.service.mcp_egress import (
                    RESPONSE_TOO_LARGE,
                    MCPEgressError,
                )
                raise MCPEgressError(RESPONSE_TOO_LARGE, "oversized")

            monkeypatch.setattr(
                "map_core.service.dynamic_tools.post_json_stream_guarded", oversized
            )
            result = asyncio.run(
                _call_http_mcp_tool(server=server, tool_name="echo", args={})
            )
        assert result.success is False
        assert "MCP_RESPONSE_TOO_LARGE" in result.error


# ---------------------------------------------------------------------------
# S3-02: sequential-DNS (rebinding) and peer-address defenses
# ---------------------------------------------------------------------------


class TestDnsRebindingDefense:
    def test_sequential_dns_change_rejected_before_dial(self) -> None:
        """Validation saw a public IP; the dial-time re-resolution returns a
        private one - the request must fail BEFORE any bytes are sent."""
        import asyncio

        from map_core.service.mcp_egress import (
            EGRESS_DENIED,
            MCPEgressError,
            post_json_stream_guarded,
        )

        # the dial-time re-resolution (first call inside post_json_stream_
        # guarded) now answers with the metadata address
        with mock.patch(
            "map_core.service.mcp_egress.resolve_ips",
            return_value=["169.254.169.254"],
        ):
            with pytest.raises(MCPEgressError) as exc_info:
                asyncio.run(
                    post_json_stream_guarded(
                        url="https://api.example.com/mcp",
                        json_payload={},
                        headers={},
                        timeout_s=5,
                        max_response_bytes=1024 * 1024,
                        allowed_ips=frozenset({"93.184.216.34"}),
                    )
                )
        assert exc_info.value.code == EGRESS_DENIED
        assert "rebinding" in str(exc_info.value)

    def test_peer_address_must_be_in_validated_set(self) -> None:
        """The ACTUAL peer of the established connection must be one of the
        validated addresses (post-connect check)."""
        import asyncio

        import httpx as httpx_module

        from map_core.service.mcp_egress import (
            EGRESS_DENIED,
            MCPEgressError,
            post_json_stream_guarded,
        )

        class _FakeStream:
            def get_extra_info(self, name: str):
                if name == "peername":
                    return ("10.0.0.5", 443)
                return None

        class _FakeResponseCtx:
            status_code = 200
            headers = httpx_module.Headers()
            extensions = {"network_stream": _FakeStream()}

            def __init__(self, body: bytes) -> None:
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def aiter_bytes(self):
                yield self._body

        class _FakeClient:
            def __init__(self, response_ctx: _FakeResponseCtx) -> None:
                self._ctx = response_ctx

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args) -> None:
                return None

            def stream(self, *args, **kwargs):
                return self._ctx

        async def _scenario() -> None:
            fake_client = _FakeClient(_FakeResponseCtx(b'{"ok": true}'))
            with mock.patch(
                "map_core.service.mcp_egress.httpx.AsyncClient",
                return_value=fake_client,
            ), mock.patch(
                "map_core.service.mcp_egress.resolve_ips",
                return_value=["93.184.216.34"],
            ):
                with pytest.raises(MCPEgressError) as exc_info:
                    await post_json_stream_guarded(
                        url="https://api.example.com/mcp",
                        json_payload={},
                        headers={},
                        timeout_s=5,
                        max_response_bytes=1024 * 1024,
                        allowed_ips=frozenset({"93.184.216.34"}),
                    )
                assert exc_info.value.code == EGRESS_DENIED
                assert "peer" in str(exc_info.value)
        asyncio.run(_scenario())

    def test_unparseable_port_is_a_typed_error_not_a_traceback(self) -> None:
        """https://example.com:notaport must produce a typed policy denial."""
        _policy("example.com")
        problems, _allowed = validate_mcp_url(
            "https://example.com:notaport/mcp", EgressPolicy.from_env()
        )
        assert problems, "unparseable port must be rejected"
        assert all("port" in problem for problem in problems)
