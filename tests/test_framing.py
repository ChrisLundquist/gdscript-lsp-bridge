"""LSP framing: exact byte counts, tolerant input, strict output.

The failure this guards against is silent. A framing bug does not raise where
it happens -- it desynchronizes the stream and surfaces as a garbled message
some time later, so the tests here work in bytes and check boundaries rather
than round-tripping convenient strings.
"""

from __future__ import annotations

import json
import unittest

from gdscript_lsp_bridge import framing


def chunked_source(data: bytes, chunk: int = 3):
    """Returns a recv-alike that hands ``data`` back in small pieces.

    Small chunks are the point: a reader that happens to work when every
    message arrives in one recv can still be wrong at a buffer boundary, which
    is exactly what a real socket produces under load.
    """
    position = 0

    def recv(count: int) -> bytes:
        nonlocal position
        take = min(count, chunk, len(data) - position)
        if take <= 0:
            return b""
        piece = data[position : position + take]
        position += take
        return piece

    return recv


class EncodeTest(unittest.TestCase):
    """Output framing is exact and always uses CRLF."""

    def test_header_counts_bytes_not_characters(self) -> None:
        body = "héllo ünicode 日本語".encode("utf-8")
        message = framing.encode_body(body)
        header, _, payload = message.partition(b"\r\n\r\n")
        self.assertEqual(header, b"Content-Length: " + str(len(body)).encode())
        self.assertEqual(payload, body)
        self.assertNotEqual(len(body), len(body.decode("utf-8")))

    def test_encode_uses_crlf_terminated_headers(self) -> None:
        message = framing.encode({"jsonrpc": "2.0", "id": 1})
        self.assertIn(b"\r\n\r\n", message)
        self.assertNotIn(b"\n\n", message.split(b"\r\n\r\n")[0])

    def test_empty_body_is_representable(self) -> None:
        self.assertEqual(framing.encode_body(b""), b"Content-Length: 0\r\n\r\n")


class ReadTest(unittest.TestCase):
    """Input framing survives chunking, extra headers and lenient terminators."""

    def test_round_trip_through_a_chunked_stream(self) -> None:
        payload = {"jsonrpc": "2.0", "id": 7, "method": "textDocument/hover"}
        stream = framing.encode(payload)
        reader = framing.MessageReader(chunked_source(stream))
        self.assertEqual(json.loads(reader.read_message().decode("utf-8")), payload)
        self.assertIsNone(reader.read_message())

    def test_several_messages_in_one_buffer_stay_separated(self) -> None:
        first = {"id": 1, "method": "a"}
        second = {"id": 2, "method": "b"}
        third = {"id": 3, "method": "c"}
        stream = b"".join(framing.encode(p) for p in (first, second, third))
        reader = framing.MessageReader(chunked_source(stream, chunk=8192))
        self.assertEqual(json.loads(reader.read_message()), first)
        self.assertEqual(json.loads(reader.read_message()), second)
        self.assertEqual(json.loads(reader.read_message()), third)
        self.assertIsNone(reader.read_message())

    def test_unicode_body_survives_a_one_byte_at_a_time_stream(self) -> None:
        payload = {"text": "class_name Ünicøde\n\tfunc ずっと() -> void:\n\t\tpass"}
        stream = framing.encode(payload)
        reader = framing.MessageReader(chunked_source(stream, chunk=1))
        self.assertEqual(json.loads(reader.read_message().decode("utf-8")), payload)

    def test_body_bytes_are_returned_verbatim(self) -> None:
        # Forwarding must not reshape traffic: odd spacing and key order in,
        # the same bytes out.
        body = b'{  "z":1,\n  "a":  2 }'
        reader = framing.MessageReader(chunked_source(framing.encode_body(body)))
        self.assertEqual(reader.read_message(), body)

    def test_additional_headers_are_ignored(self) -> None:
        body = b'{"ok":true}'
        stream = (
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n" + body
        )
        reader = framing.MessageReader(chunked_source(stream))
        self.assertEqual(reader.read_message(), body)

    def test_header_name_matching_is_case_insensitive(self) -> None:
        body = b'{"ok":true}'
        stream = b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        reader = framing.MessageReader(chunked_source(stream))
        self.assertEqual(reader.read_message(), body)

    def test_bare_lf_terminator_is_accepted(self) -> None:
        body = b'{"ok":true}'
        stream = b"Content-Length: " + str(len(body)).encode() + b"\n\n" + body
        reader = framing.MessageReader(chunked_source(stream))
        self.assertEqual(reader.read_message(), body)

    def test_a_body_containing_the_terminator_is_not_split(self) -> None:
        # The reader must trust Content-Length, not scan for a delimiter.
        body = b'{"text":"line\\r\\n\\r\\nmore"}'
        stream = framing.encode_body(body) + framing.encode_body(b'{"second":true}')
        reader = framing.MessageReader(chunked_source(stream, chunk=5))
        self.assertEqual(reader.read_message(), body)
        self.assertEqual(reader.read_message(), b'{"second":true}')

    def test_clean_eof_on_a_boundary_returns_none(self) -> None:
        reader = framing.MessageReader(chunked_source(b""))
        self.assertIsNone(reader.read_message())

    def test_truncated_body_raises(self) -> None:
        body = b'{"incomplete":true}'
        stream = framing.encode_body(body)[:-4]
        reader = framing.MessageReader(chunked_source(stream))
        with self.assertRaises(framing.FramingError):
            reader.read_message()

    def test_truncated_header_raises(self) -> None:
        reader = framing.MessageReader(chunked_source(b"Content-Length: 12"))
        with self.assertRaises(framing.FramingError):
            reader.read_message()

    def test_missing_content_length_raises(self) -> None:
        reader = framing.MessageReader(chunked_source(b"X-Other: 1\r\n\r\n{}"))
        with self.assertRaises(framing.FramingError):
            reader.read_message()

    def test_non_numeric_content_length_raises(self) -> None:
        reader = framing.MessageReader(chunked_source(b"Content-Length: abc\r\n\r\n{}"))
        with self.assertRaises(framing.FramingError):
            reader.read_message()

    def test_implausible_content_length_is_refused_without_allocating(self) -> None:
        stream = b"Content-Length: 999999999999\r\n\r\n"
        reader = framing.MessageReader(chunked_source(stream))
        with self.assertRaises(framing.FramingError):
            reader.read_message()


if __name__ == "__main__":
    unittest.main()
