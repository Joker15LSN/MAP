"""SSE parser unit tests (FIX-P1-CONV-01).

Byte-level chunking, UTF-8 mid-character splits, CRLF, multi-line data,
half frames, strict decode/JSON errors.
"""

from __future__ import annotations

import pytest

from app.services.sse import SseParseError, SseStreamParser, parse_sse_frames

FULL_STREAM = (
    'event: start\ndata: {"message_id":"m1"}\n\n'
    'event: content_delta\ndata: {"content":"你"}\n\n'
    'event: content_delta\ndata: {"content":"好"}\n\n'
    'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'
).encode()


def _events(parser_result) -> list[tuple[str, str]]:
    return [(f.event, f.data) for f in parser_result.frames]


def test_parse_sse_frames_basic() -> None:
    result = parse_sse_frames(FULL_STREAM.decode("utf-8"))
    events = _events(result)
    assert events[0] == ("start", '{"message_id":"m1"}')
    assert events[-1] == ("done", '{"content":"你好","task_id":"t-1"}')
    assert result.remaining == ""


def test_every_byte_split_produces_identical_result() -> None:
    """Split the stream at every byte offset: final events must match."""
    reference = _events(parse_sse_frames(FULL_STREAM.decode("utf-8")))
    for split_at in range(1, len(FULL_STREAM)):
        parser = SseStreamParser()
        collected: list[tuple[str, str]] = []
        for chunk_start in range(0, len(FULL_STREAM), split_at):
            chunk = FULL_STREAM[chunk_start : chunk_start + split_at]
            result = parser.feed(chunk)
            collected.extend(_events(result))
        result = parser.close()
        collected.extend(_events(result))
        assert result.remaining == ""  # tail may hold nothing
        assert collected == reference, f"mismatch at split {split_at}"


def test_utf8_character_split_across_chunks_no_replacement_char() -> None:
    """A multi-byte character split at every byte must never corrupt text."""
    parser = SseStreamParser()
    collected: list[tuple[str, str]] = []
    for i in range(len(FULL_STREAM)):
        chunk = FULL_STREAM[i : i + 1]
        result = parser.feed(chunk)
        collected.extend(_events(result))
        assert "\ufffd" not in "".join(f.data for f in result.frames)
    collected.extend(_events(parser.close()))
    assert "\ufffd" not in "".join(data for _, data in collected)
    assert collected[-1][1] == '{"content":"你好","task_id":"t-1"}'


def test_crlf_frames_are_supported() -> None:
    stream = (
        b"event: start\r\ndata: {\"a\":1}\r\n\r\n"
        b"event: done\r\ndata: {\"content\":\"x\"}\r\n\r\n"
    )
    parser = SseStreamParser()
    result = parser.feed(stream)
    assert _events(result) == [
        ("start", '{"a":1}'),
        ("done", '{"content":"x"}'),
    ]


def test_multi_line_data_joins_with_newline() -> None:
    stream = b'data: {"content":"line1"\ndata: ,"line2"}\n\n'
    parser = SseStreamParser()
    result = parser.feed(stream)
    assert result.frames[0].data == '{"content":"line1"\n,"line2"}'


def test_half_frame_kept_in_remaining() -> None:
    parser = SseStreamParser()
    first = parser.feed(b'event: start\ndata: {"a":1}\n\n event: par')
    assert len(first.frames) == 1
    assert "par" in first.remaining
    second = parser.feed(b'tial\ndata: {}\n\n')
    assert _events(second)[0] == ("message", "{}")  # no event: -> default
    assert second.remaining == ""


def test_invalid_utf8_raises_stable_error() -> None:
    parser = SseStreamParser()
    with pytest.raises(SseParseError) as exc_info:
        parser.feed(b'event: done\ndata: {"content":"\xff\xfe"}\n\n')
    assert exc_info.value.code == "STREAM_DECODE_ERROR"


def test_truncated_utf8_at_eof_raises() -> None:
    parser = SseStreamParser()
    parser.feed(b'event: content_delta\ndata: {"content":"\xe4\xbd')
    with pytest.raises(SseParseError) as exc_info:
        parser.close()
    assert exc_info.value.code == "STREAM_DECODE_ERROR"


def test_invalid_json_data_raises_stable_error() -> None:
    from app.services.sse import frame_data_json

    parser = SseStreamParser()
    result = parser.feed(b'event: done\ndata: {not-json}\n\n')
    with pytest.raises(SseParseError) as exc_info:
        frame_data_json(result.frames[0])
    assert exc_info.value.code == "STREAM_PARSE_ERROR"


def test_empty_data_is_tolerated() -> None:
    parser = SseStreamParser()
    result = parser.feed(b'event: done\ndata:\n\n')
    assert result.frames[0].data == ""
