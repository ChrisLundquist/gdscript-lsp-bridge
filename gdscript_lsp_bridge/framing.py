"""LSP base-protocol framing: ``Content-Length`` headers over a byte stream.

The wire format is deliberately unforgiving -- a header block terminated by a
blank line, then EXACTLY ``Content-Length`` bytes of UTF-8 JSON. Two mistakes
break it silently and are the reason this module exists rather than a few
inline ``readline`` calls:

* Counting characters instead of bytes. Any non-ASCII identifier in a GDScript
  file makes those two numbers differ, and the stream desynchronizes one
  message later, far from the cause.
* Letting a text-mode stream translate line endings. On the header block that
  turns ``\\r\\n`` into ``\\n`` and the peer's byte count stops matching.

So every path here works in bytes on binary handles. The reader is tolerant on
input (it accepts a bare ``\\n`` header terminator, which some servers emit) and
strict on output (it always writes ``\\r\\n``), which is the usual robustness
posture for a protocol that sits between two implementations it does not own.

The reader hands back the RAW body bytes. A bridge that re-encoded every
message through ``json.loads``/``json.dumps`` would change key order, spacing
and escaping of traffic it has no business editing; forwarding the original
bytes means only messages this bridge deliberately rewrites are ever reshaped.
"""

from __future__ import annotations

import json
from typing import Any, Callable

#: Header block terminators accepted on input, most-specific first.
_TERMINATORS = (b"\r\n\r\n", b"\n\n")

#: Refuse absurd Content-Length values rather than trying to allocate them.
#: An LSP message is JSON describing a source file; 256 MiB is far past any
#: legitimate one and well short of exhausting memory.
MAX_CONTENT_LENGTH = 256 * 1024 * 1024


class FramingError(Exception):
    """Raised when the peer's byte stream is not valid LSP framing."""


def encode(payload: dict[str, Any]) -> bytes:
    """Frames a JSON-RPC ``payload`` as a complete LSP message."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return encode_body(body)


def encode_body(body: bytes) -> bytes:
    """Frames already-serialized ``body`` bytes as a complete LSP message."""
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


class MessageReader:
    """Reads ``Content-Length`` framed messages from a byte source.

    ``recv`` is any callable taking a maximum byte count and returning up to
    that many bytes, with ``b""`` meaning end of stream -- which fits both
    ``socket.recv`` and a binary file object's ``read``.
    """

    def __init__(self, recv: Callable[[int], bytes], chunk_size: int = 65536) -> None:
        self._recv = recv
        self._chunk_size = chunk_size
        self._buffer = bytearray()

    def read_message(self) -> bytes | None:
        """Returns one message's raw body bytes, or None at a clean EOF.

        A clean EOF is one that lands on a message boundary with no partial
        header buffered. EOF mid-message is a truncation and raises.
        """
        header = self._read_header()
        if header is None:
            return None
        length = _content_length(header)
        body = self._read_exactly(length)
        if body is None:
            raise FramingError(
                f"stream ended after {len(self._buffer)} of {length} body bytes"
            )
        return body

    def _read_header(self) -> bytes | None:
        """Consumes bytes through the header terminator; returns the headers."""
        while True:
            index, terminator_length = _find_terminator(self._buffer)
            if index >= 0:
                header = bytes(self._buffer[:index])
                del self._buffer[: index + terminator_length]
                return header
            chunk = self._recv(self._chunk_size)
            if not chunk:
                if not self._buffer:
                    return None
                raise FramingError(
                    f"stream ended mid-header with {len(self._buffer)} bytes buffered"
                )
            self._buffer.extend(chunk)

    def _read_exactly(self, count: int) -> bytes | None:
        """Consumes exactly ``count`` bytes; returns None if the stream ends."""
        while len(self._buffer) < count:
            chunk = self._recv(self._chunk_size)
            if not chunk:
                return None
            self._buffer.extend(chunk)
        body = bytes(self._buffer[:count])
        del self._buffer[:count]
        return body


def _find_terminator(buffer: bytearray) -> tuple[int, int]:
    """Returns (index, length) of the earliest header terminator, or (-1, 0).

    Both accepted terminators are searched and the EARLIEST wins, because
    ``\\r\\n\\r\\n`` contains no ``\\n\\n`` but a stream mixing the two would
    otherwise have its shorter terminator missed.
    """
    best_index = -1
    best_length = 0
    for terminator in _TERMINATORS:
        index = buffer.find(terminator)
        if index >= 0 and (best_index < 0 or index < best_index):
            best_index = index
            best_length = len(terminator)
    return best_index, best_length


def _content_length(header: bytes) -> int:
    """Extracts ``Content-Length`` from a raw header block."""
    for line in header.replace(b"\r\n", b"\n").split(b"\n"):
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        if name.strip().lower() != b"content-length":
            continue
        try:
            length = int(value.strip())
        except ValueError as error:
            raise FramingError(f"malformed Content-Length: {value!r}") from error
        if length < 0 or length > MAX_CONTENT_LENGTH:
            raise FramingError(f"implausible Content-Length: {length}")
        return length
    raise FramingError(f"header block has no Content-Length: {header!r}")
