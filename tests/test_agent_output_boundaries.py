"""Resource caps remain separate from short console/model previews.

Small fixtures test byte accounting, unread HTTP tails, and bounded preview work;
no hostile allocation or external service is needed.
"""

from __future__ import annotations

import http.client
import io

import pytest

from pydeno._result import OutputCapture, ResultTooLarge, bounded_result, capture_result
from pydeno.tools.http_fetch import HttpFetch, _Deadline


@pytest.mark.parametrize(
    ("value", "json_bytes"),
    [("é", 4), ("\x00", 8), (b"\x00\xff", 6)],
    ids=["utf8", "json-escape", "base64"],
)
def test_result_limit_charges_encoded_bytes_instead_of_preview_characters(
    value, json_bytes
):
    bounded_result(value, json_bytes)
    with pytest.raises(ResultTooLarge):
        bounded_result(value, json_bytes - 1)


def test_short_console_preview_does_not_allow_an_oversized_result():
    capture = OutputCapture(max_output_bytes=32)
    capture("log", ["preview: ok"])
    result = capture_result(capture, value="\x00\x00", max_result_bytes=8)
    assert result.status == "Failed"
    assert result.error_type == "ResultTooLarge"
    assert result.result is None
    assert result.stdout == "preview: ok\n"


def test_full_stdout_drops_later_values_without_reading_them():
    class UnreadTail(list):
        def __iter__(self):
            raise AssertionError("formatted a value after the output cap")

    capture = OutputCapture(max_output_bytes=4)
    capture("log", ["abcdefgh"])
    for _ in range(100):
        capture("log", [UnreadTail(["must not be read"])])
    capture("error", ["ok"])
    assert capture.stdout == "abcd\n[truncated]\n"
    assert capture.stderr == "ok\n"
    assert capture.truncated


def test_console_chunk_boundaries_do_not_reset_the_retained_byte_budget():
    capture = OutputCapture(max_output_bytes=4)
    for part in ["é", "é", "tail"]:
        capture("log", [part])
    assert capture.stdout == "é\n[truncated]\n"
    assert capture.truncated


class _MemorySocket:
    """Replace only HTTP's transport; use the real response parser and read1."""

    def __init__(self, response: bytes):
        self.source = io.BytesIO(response)

    def makefile(self, mode):
        assert mode == "rb"
        return self.source


@pytest.mark.parametrize(
    "headers",
    [b"", b"Content-Length: 1000000\r\n"],
    ids=["length-absent", "untrusted-length"],
)
def test_http_body_stops_at_byte_cap_plus_lookahead_without_draining_tail(headers):
    prefix = b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n"
    socket = _MemorySocket(prefix + b"abcdefgh" + b"unread-tail" * 10)
    response = http.client.HTTPResponse(socket)
    response.begin()
    tool = HttpFetch(["allowed.test"], max_response_bytes=8, response="bytes")
    assert tool._read(response, _Deadline(1)) == (b"abcdefgh", True)
    assert socket.source.tell() == len(prefix) + 9


def test_chunked_http_body_is_bounded_before_assembling_or_decoding_it():
    prefix = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    socket = _MemorySocket(prefix + b"40\r\n" + b"x" * 64 + b"\r\n0\r\n\r\n")
    response = http.client.HTTPResponse(socket)
    response.begin()
    tool = HttpFetch(["allowed.test"], max_response_bytes=8, response="text")
    assert tool._read(response, _Deadline(1)) == (b"xxxxxxxx", True)
    assert socket.source.tell() == len(prefix) + 4 + 9


def test_http_body_exactly_at_utf8_byte_limit_is_not_marked_truncated():
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n" + "éééé".encode()
    response = http.client.HTTPResponse(_MemorySocket(raw))
    response.begin()
    tool = HttpFetch(["allowed.test"], max_response_bytes=8, response="text")
    assert tool._read(response, _Deadline(1)) == ("éééé".encode(), False)


@pytest.mark.needs_pydantic_ai
def test_model_preview_visits_only_the_dictionary_entries_it_shows():
    from pydeno.integrations.pydantic_ai import _preview

    class VisitedDict(dict):
        visits = 0

        def items(self):
            for pair in super().items():
                self.visits += 1
                yield pair

    value = VisitedDict({str(index): index for index in range(20)})
    text = _preview(value)
    assert text == "{'0': 0, '1': 1, '2': 2, '3': 3, '4': 4} ... (20 items)"
    assert value.visits == 5


def test_wire_frame_cap_still_charges_the_full_tool_reply(monkeypatch):
    from pydeno import _wire

    monkeypatch.setattr(_wire, "MAX_FRAME_BYTES", 96)
    value = "x" * 80  # 82 compact JSON bytes: fits the separate result cap.
    assert bounded_result(value, 128) == value
    preview = OutputCapture(max_output_bytes=4)
    preview("log", [value])
    assert preview.truncated
    with pytest.raises(_wire.WireError):
        _wire.dumps({"t": "reply", "cid": 1, "v": _wire.Enc(value)})
    payload = _wire.dumps({"t": "reply", "cid": 1, "v": _wire.Enc("ok")})
    assert _wire.loads(payload)["v"] == "ok"
