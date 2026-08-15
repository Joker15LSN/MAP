"""S2-04: guarded HTTP egress for MCP tool servers.

The algorithm process may only reach an HTTP MCP server through this
module. It enforces, BEFORE any bytes leave and while reading the reply:

- HTTPS only, except an explicit opt-in that only ever admits
  http://127.0.0.1 / http://localhost / http://[::1] for local tests
  (MAP_MCP_ALLOW_INSECURE_LOCAL=1);
- a host (and optional port) allowlist (MAP_MCP_ALLOWED_HOSTS);
- post-resolution IP policy: every address the hostname resolves to must be
  public - loopback/link-local/private/reserved/multicast/unspecified are
  rejected (blocks 127.0.0.1, 169.254.169.254, IPv6 loopback, private-range
  resolution and DNS-rebinding answers that mix public + private records);
- no redirects (follow_redirects=False);
- a hard response-size cap read while streaming (MAP_MCP_MAX_RESPONSE_BYTES,
  default 1 MiB) - oversized replies are cut and rejected;
- request headers must come from secret references (``${ENV:VAR}``), never
  from literal plaintext in server config.

Errors are typed ``MCPEgressError`` with stable codes and never carry the
secret material itself.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx

EGRESS_DENIED: Final[str] = "MCP_EGRESS_POLICY_DENIED"
EGRESS_CONFIG: Final[str] = "MCP_EGRESS_CONFIG_INVALID"
RESPONSE_TOO_LARGE: Final[str] = "MCP_RESPONSE_TOO_LARGE"

ALLOWED_HOSTS_ENV: Final[str] = "MAP_MCP_ALLOWED_HOSTS"
ALLOW_INSECURE_LOCAL_ENV: Final[str] = "MAP_MCP_ALLOW_INSECURE_LOCAL"
MAX_RESPONSE_BYTES_ENV: Final[str] = "MAP_MCP_MAX_RESPONSE_BYTES"
DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024

_SECRET_REF = re.compile(r"^\$\{(ENV):([A-Za-z0-9_.-]+)\}$")
_LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class MCPEgressError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class AllowedHost:
    """One allowlist entry: hostname and an optional required port."""

    host: str
    port: int | None = None

    @classmethod
    def parse(cls, raw: str) -> "AllowedHost":
        entry = raw.strip().lower()
        if not entry:
            raise MCPEgressError(EGRESS_CONFIG, "empty allowlist entry")
        if entry.startswith("["):  # bracketed IPv6 host
            host, _, port = entry.partition("]")
            host = host[1:]
            if port:
                if not port.startswith(":"):
                    raise MCPEgressError(
                        EGRESS_CONFIG, f"invalid allowlist entry {raw!r}"
                    )
                return cls(host=host, port=_parse_port(port[1:], raw))
            return cls(host=host)
        if entry.count(":") == 1:  # host:port (IPv4 or hostname)
            host, port = entry.split(":")
            return cls(host=host, port=_parse_port(port, raw))
        # bare hostname or bare IPv6 address
        return cls(host=entry)


def _parse_port(port: str, raw: str) -> int:
    try:
        value = int(port)
    except ValueError:
        raise MCPEgressError(EGRESS_CONFIG, f"invalid port in {raw!r}") from None
    if not 1 <= value <= 65535:
        raise MCPEgressError(EGRESS_CONFIG, f"invalid port in {raw!r}")
    return value


@dataclass(frozen=True)
class EgressPolicy:
    """Loaded once per process; recomputed from env on each call so tests
    can flip the configuration without restarting."""

    allowed_hosts: tuple[AllowedHost, ...] = ()
    allow_insecure_local: bool = False
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    @classmethod
    def from_env(cls) -> "EgressPolicy":
        problems: list[str] = []
        allowed: list[AllowedHost] = []
        for entry in (os.getenv(ALLOWED_HOSTS_ENV) or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                allowed.append(AllowedHost.parse(entry))
            except MCPEgressError as exc:
                problems.append(str(exc))
        if not allowed and os.getenv(ALLOWED_HOSTS_ENV):
            problems.append(
                f"{ALLOWED_HOSTS_ENV} is set but contains no valid entries"
            )
        allow_local = os.getenv(ALLOW_INSECURE_LOCAL_ENV) == "1"
        max_bytes = DEFAULT_MAX_RESPONSE_BYTES
        raw_max = os.getenv(MAX_RESPONSE_BYTES_ENV)
        if raw_max:
            try:
                max_bytes = int(raw_max)
            except ValueError:
                problems.append(f"{MAX_RESPONSE_BYTES_ENV} is not an integer")
            else:
                if max_bytes < 1024:
                    problems.append(f"{MAX_RESPONSE_BYTES_ENV} must be >= 1024")
        if problems:
            raise MCPEgressError(EGRESS_CONFIG, "; ".join(problems))
        return cls(
            allowed_hosts=tuple(allowed),
            allow_insecure_local=allow_local,
            max_response_bytes=max_bytes,
        )


def _matches_allowed(host: str, port: int | None, allowed: AllowedHost) -> bool:
    if host != allowed.host:
        return False
    if allowed.port is not None and port != allowed.port:
        return False
    return True


def _forbidden_ip_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        return "private"
    if ip.is_unspecified:
        return "unspecified"
    return None


def resolve_ips(host: str) -> list[str]:
    """Resolve every A/AAAA record for the host (0 = both families)."""
    try:
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise MCPEgressError(
            EGRESS_DENIED, f"host {host!r} could not be resolved: {exc}"
        ) from exc
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    if not seen:
        raise MCPEgressError(EGRESS_DENIED, f"host {host!r} resolved to no addresses")
    return seen


def validate_mcp_url(url: str, policy: EgressPolicy) -> tuple[list[str], frozenset[str]]:
    """Return (problems, allowed_ips).

    ``allowed_ips`` is the set of addresses the hostname resolved to at
    validation time and which passed the IP policy - the ONLY addresses the
    connection may later use (see post_json_stream_guarded).
    """
    problems: list[str] = []
    allowed_ips: set[str] = set()
    try:
        parts = urlsplit(url)
        # S3-02: an unparseable port raises ValueError inside .port - turn
        # it into a typed policy denial instead of a traceback.
        try:
            parts.port  # noqa: B018 - property access performs the parse
        except ValueError as exc:
            problems.append(f"unparseable port in URL: {exc}")
            return problems, frozenset()
    except ValueError as exc:
        return [f"unparseable URL: {exc}"], frozenset()
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port

    if scheme not in {"https", "http"}:
        problems.append(f"scheme {scheme!r} is not https")
        return problems, frozenset()

    if scheme == "http":
        if not policy.allow_insecure_local:
            problems.append(
                "plain http requires MAP_MCP_ALLOW_INSECURE_LOCAL=1 "
                "(local tests only)"
            )
        elif host not in _LOCAL_HTTP_HOSTS:
            problems.append(
                "plain http with the local opt-in only admits "
                "127.0.0.1/localhost/::1"
            )

    if not policy.allowed_hosts:
        problems.append(
            f"no egress allowlist configured ({ALLOWED_HOSTS_ENV} is empty)"
        )
    elif not any(_matches_allowed(host, port, entry) for entry in policy.allowed_hosts):
        problems.append(
            f"host:port {host}:{port or '-'} is not on the egress allowlist"
        )

    if not host:
        problems.append("URL has no hostname")
        return problems, frozenset()

    # Post-resolution IP policy: every resolved address must be public.
    # The local http opt-in (MAP_MCP_ALLOW_INSECURE_LOCAL=1) admits loopback
    # addresses ONLY, and nothing else.
    local_http_optin = scheme == "http" and policy.allow_insecure_local
    try:
        ips = resolve_ips(host)
    except MCPEgressError as exc:
        problems.append(str(exc))
        return problems, frozenset()
    for raw_ip in ips:
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            problems.append(f"resolved address {raw_ip!r} is not an IP")
            continue
        reason = _forbidden_ip_reason(ip)
        if reason:
            if local_http_optin and ip.is_loopback:
                allowed_ips.add(raw_ip)
                continue
            problems.append(
                f"host {host!r} resolves to {raw_ip} ({reason}); "
                "non-public egress is forbidden"
            )
            continue
        allowed_ips.add(raw_ip)

    if local_http_optin and not problems:
        # local opt-in: only loopback resolution is acceptable
        if not all(
            ipaddress.ip_address(raw).is_loopback for raw in ips
        ):
            problems.append("local http opt-in requires loopback resolution only")
            return problems, frozenset()
    if problems:
        return problems, frozenset()
    return problems, frozenset(allowed_ips)


@dataclass
class ResolvedHeaders:
    """Headers resolved from secret references + their secret values.

    ``secret_values`` is used to wipe exact values from any downstream
    result/error/log so an upstream echo can never leak them.
    """

    headers: dict[str, str] = field(default_factory=dict)
    secret_values: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, configured: dict[str, Any]) -> "ResolvedHeaders":
        """S2-04: every request header must come from a secret reference.

        Literal header values in server config are rejected (they would be
        plaintext secrets living in ordinary configuration); ``${ENV:VAR}``
        resolves from the process environment at call time.
        """
        problems: list[str] = []
        headers: dict[str, str] = {}
        secret_values: list[str] = []
        for raw_key, raw_value in (configured or {}).items():
            key = str(raw_key).strip()
            value = str(raw_value or "")
            if not key:
                continue
            match = _SECRET_REF.fullmatch(value.strip())
            if not match:
                problems.append(
                    f"header {key!r} must be a secret reference "
                    "${{ENV:VAR}}, not a literal value"
                )
                continue
            resolved = os.environ.get(match.group(2), "")
            if not resolved:
                problems.append(
                    f"header {key!r}: secret reference "
                    f"${{ENV:{match.group(2)}}} resolved to an empty value"
                )
                continue
            headers[key] = resolved
            secret_values.append(resolved)
        if problems:
            raise MCPEgressError(EGRESS_CONFIG, "; ".join(problems))
        return cls(headers=headers, secret_values=tuple(secret_values))


@dataclass(frozen=True)
class GuardedResponse:
    """A fully-buffered, size-checked HTTP response (no redirects allowed)."""

    status_code: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def _normalize_host(host: str) -> str:
    """Lowercase a dial host and strip IPv6 brackets for comparison."""
    return host.strip().strip("[]").lower()


def _stream_peer_ip(stream: httpcore.AsyncNetworkStream) -> str | None:
    """Return the remote IP of a freshly-connected stream, if determinable."""
    server_addr = stream.get_extra_info("server_addr")
    if server_addr is not None:
        try:
            return str(server_addr[0])
        except (IndexError, TypeError):
            pass
    sock = stream.get_extra_info("socket")
    if sock is not None:
        try:
            return str(sock.getpeername()[0])
        except (OSError, IndexError, TypeError):
            return None
    return None


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dial ONLY the verified addresses for the validated host.

    connect_tcp is the only way httpcore reaches the network. It verifies
    that httpcore is dialing the host we validated, then connects DIRECTLY to
    each candidate IP (the OS resolver never sees the hostname again). Before
    the stream is returned - i.e. before any TLS handshake or HTTP byte - the
    actual peer address is confirmed to be in allowed_ips; a mismatch is a
    typed denial and closes the socket.
    """

    def __init__(
        self,
        expected_host: str,
        allowed_ips: frozenset[str],
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._expected_host = _normalize_host(expected_host)
        self._allowed_ips = frozenset(allowed_ips)
        self._candidate_ips = sorted(self._allowed_ips)
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if _normalize_host(host) != self._expected_host:
            raise MCPEgressError(
                EGRESS_DENIED,
                f"transport dialed {host!r}, expected {self._expected_host!r}",
            )
        if not self._candidate_ips:
            raise MCPEgressError(EGRESS_DENIED, "no verified addresses to dial")

        last_error: Exception | None = None
        for ip in self._candidate_ips:
            try:
                stream = await self._inner.connect_tcp(
                    host=ip,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
                continue

            peer_ip = _stream_peer_ip(stream)
            if peer_ip is None:
                await stream.aclose()
                raise MCPEgressError(
                    EGRESS_DENIED, "could not confirm the connection peer address"
                )
            if peer_ip not in self._allowed_ips:
                await stream.aclose()
                raise MCPEgressError(
                    EGRESS_DENIED,
                    f"connection bound to {peer_ip}, which is not in the "
                    f"verified address set {sorted(self._allowed_ips)}",
                )
            return stream

        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("no verified addresses to dial")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed for MCP egress")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


class _PinnedEgressTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose pool dials the pinned backend directly.

    Inherits httpx.AsyncHTTPTransport's request/response conversion and
    httpcore-exception mapping, but replaces the pool with one that never
    performs DNS resolution (the _PinnedNetworkBackend) and never keeps a
    connection alive for reuse across calls.
    """

    def __init__(
        self,
        *,
        expected_host: str,
        allowed_ips: frozenset[str],
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        backend = (
            network_backend
            if network_backend is not None
            else _PinnedNetworkBackend(expected_host, allowed_ips)
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(
                verify=True, cert=None, trust_env=True
            ),
            max_connections=10,
            max_keepalive_connections=0,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


async def post_json_stream_guarded(
    *,
    url: str,
    json_payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: int,
    max_response_bytes: int,
    allowed_ips: frozenset[str],
) -> GuardedResponse:
    """POST with no redirects, a hard streaming response-size cap and
    DNS-rebinding protection.

    allowed_ips is mandatory and is the address set the URL was validated
    against (validate_mcp_url). The hostname is re-resolved immediately
    before dialing and must still be a subset of that set; the transport then
    connects DIRECTLY to those verified IPs (never handing the hostname back
    to the OS resolver) while preserving the original Host header and TLS SNI.
    The peer address is confirmed against the verified set before any TLS or
    HTTP bytes are written, so a connect that lands on a different
    link-local/private address is aborted with zero bytes on the wire.

    Raises MCPEgressError(RESPONSE_TOO_LARGE) the moment the body exceeds
    max_response_bytes - the connection is aborted before the full payload is
    ever buffered.
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - parse and validate the port
    except ValueError as exc:
        raise MCPEgressError(
            EGRESS_DENIED, f"unparseable URL/port: {exc}"
        ) from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise MCPEgressError(EGRESS_DENIED, "URL has no hostname")
    if not allowed_ips:
        raise MCPEgressError(EGRESS_DENIED, "no verified addresses to dial")

    # Pre-dial re-resolution: compare the fresh DNS answer against the
    # validated set before anything is sent. Because the transport below pins
    # the dial to allowed_ips, a rebinding answer that introduces a new
    # address is rejected here and never reaches a socket.
    fresh_ips = resolve_ips(host)
    if not set(fresh_ips) <= set(allowed_ips):
        raise MCPEgressError(
            EGRESS_DENIED,
            f"host {host!r} changed its DNS answer between validation and dial "
            f"(possible rebinding): validated {sorted(allowed_ips)}, "
            f"now {sorted(fresh_ips)}",
        )

    transport = _PinnedEgressTransport(expected_host=host, allowed_ips=allowed_ips)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST", url, json=json_payload, headers=headers
            ) as response:
                total = 0
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise MCPEgressError(
                            RESPONSE_TOO_LARGE,
                            f"MCP response exceeded {max_response_bytes} bytes",
                        )
                    chunks.append(chunk)
                return GuardedResponse(
                    status_code=response.status_code,
                    headers=response.headers,
                    body=b"".join(chunks),
                )
    except MCPEgressError:
        raise
    except httpx.TimeoutException as exc:
        raise MCPEgressError(
            EGRESS_DENIED, f"MCP request timed out: {exc}"
        ) from exc
    except httpx.TooManyRedirects as exc:
        raise MCPEgressError(EGRESS_DENIED, f"MCP redirect refused: {exc}") from exc
    except httpx.HTTPError as exc:
        raise MCPEgressError(EGRESS_DENIED, f"MCP request failed: {exc}") from exc
