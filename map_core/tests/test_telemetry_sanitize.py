"""P1 acceptance tests: OTel log export must redact sensitive values.

Regression for the review finding that the native log exporter bypassed
redaction entirely, and that quoted JSON fields / key-blacklisted extras
leaked secrets. Both the native-log path (``loguru_record_to_logging_record``)
and the span-event path (``_loguru_record_to_span_attributes``) must share
the same redaction entrypoint.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator

from map_core.observability import telemetry


def _fake_record(message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": "map_core.test",
        "level": SimpleNamespace(no=20, name="INFO"),
        "file": SimpleNamespace(path="/tmp/test.py"),
        "line": 10,
        "function": "test_fn",
        "message": message,
        "extra": extra or {},
    }


def _both_paths_redacted(
    message: str, extra: dict[str, Any] | None = None
) -> Iterator[tuple[str, dict[str, Any]]]:
    record = _fake_record(message, extra)

    log_record = telemetry.loguru_record_to_logging_record(record)
    yield log_record.getMessage(), dict(
        (key, getattr(log_record, key))
        for key in dir(log_record)
        if key.startswith("map.")
    )

    attributes = telemetry._loguru_record_to_span_attributes(record)
    yield attributes["log.body"], {
        key: value for key, value in attributes.items() if key.startswith("map.")
    }


def test_json_quoted_api_key_redacted() -> None:
    message = 'request payload {"api_key": "sk-abc123secret", "query": "hello"}'
    for body, _ in _both_paths_redacted(message):
        assert "sk-abc123secret" not in body
        assert '"api_key": "<redacted>"' in body
        assert '"query": "hello"' in body


def test_authorization_header_pair_redacted() -> None:
    message = "upstream response headers: Authorization: Bearer eyJhbGciOiJI.secret-token"
    for body, _ in _both_paths_redacted(message):
        assert "eyJhbGciOiJI.secret-token" not in body
        assert "Bearer eyJhbGciOiJI" not in body


def test_url_query_token_redacted() -> None:
    message = "GET https://svc.internal/api?user=bob&token=deadbeef1234&x=1"
    for body, _ in _both_paths_redacted(message):
        assert "deadbeef1234" not in body
        assert "user=bob" in body


def test_bare_bearer_and_sk_token_redacted() -> None:
    message = "retry with bearer abc.def.ghi and key sk-proj-XYZ_012"
    for body, _ in _both_paths_redacted(message):
        assert "abc.def.ghi" not in body
        assert "sk-proj-XYZ_012" not in body


def test_extra_key_blacklist_redacted_on_both_paths() -> None:
    extra = {"password": "p@ssw0rd!", "api-key": "sk-leaked", "latency_ms": 42}
    record = _fake_record("plain message", extra)

    log_record = telemetry.loguru_record_to_logging_record(record)
    assert getattr(log_record, "map.password") == "<redacted>"
    assert getattr(log_record, "map.api-key") == "<redacted>"
    assert getattr(log_record, "map.latency_ms") == 42

    attributes = telemetry._loguru_record_to_span_attributes(record)
    assert attributes["map.password"] == "<redacted>"
    assert attributes["map.api-key"] == "<redacted>"
    assert attributes["map.latency_ms"] == 42


def test_extra_value_content_also_sanitized() -> None:
    extra = {"request_body": '{"authorization": "secret-value-42"}'}
    record = _fake_record("sending request", extra)

    log_record = telemetry.loguru_record_to_logging_record(record)
    assert "secret-value-42" not in str(getattr(log_record, "map.request_body"))

    attributes = telemetry._loguru_record_to_span_attributes(record)
    assert "secret-value-42" not in str(attributes["map.request_body"])


def test_plain_message_unchanged() -> None:
    message = "flow node n1 finished in 1.2s"
    for body, _ in _both_paths_redacted(message):
        assert body == message
