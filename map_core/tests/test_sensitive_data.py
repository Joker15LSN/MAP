"""Sensitive-data redaction tests (R-07): canary secrets never survive."""

from __future__ import annotations

from map_core.utils.sensitive_data import redact_mapping, redact_text

# Contains "fake-" so the unified credential scanner (scripts/security_scan.py)
# never treats the test fixture itself as a leaked secret.
CANARY = "sk-fake-canary-secret-0123456789abcdef"


def test_redact_mapping_strips_sensitive_keys_recursively() -> None:
    payload = {
        "api_key": CANARY,
        "query": "hello",
        "nested": {"token": CANARY, "ok": "kept"},
        "list": [{"authorization": "Bearer " + CANARY}],
    }
    redacted = redact_mapping(payload)
    serialized = str(redacted)
    assert CANARY not in serialized
    assert redacted["api_key"] == "<redacted>"
    assert redacted["nested"]["token"] == "<redacted>"
    assert redacted["list"][0]["authorization"] == "<redacted>"
    assert redacted["query"] == "hello"


def test_redact_text_covers_json_pairs_bearer_and_uri() -> None:
    text = (
        'payload {"api_key": "%s", "query": "hi"} '
        "auth: Bearer %s "
        "dsn mongodb://root:fake-topsecret-1@host/db" % (CANARY, CANARY)
    )
    redacted = redact_text(text)
    assert CANARY not in redacted
    assert "topsecret-1" not in redacted
    assert "<redacted>" in redacted


def test_redact_text_keeps_benign_content() -> None:
    text = '{"query": "hello world", "count": 3}'
    assert redact_text(text) == text


def test_redact_mapping_handles_non_mappings() -> None:
    assert redact_mapping("plain") == "plain"
    assert redact_mapping(3) == 3
