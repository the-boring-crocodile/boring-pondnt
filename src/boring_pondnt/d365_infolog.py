"""Decode the bounded subset of the X++ INFO container observed on D365 10.0.49."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_INFO_BYTES = 1024 * 1024
MAX_DEPTH = 16
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Message:
    kind: int
    text: str
    message_id: str


class _Malformed(ValueError):
    pass


class _Container:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise _Malformed
        result = self.data[self.pos:end]
        self.pos = end
        return result

    def start(self, depth: int) -> None:
        if depth > MAX_DEPTH or not self.data.startswith(b"\x07\xfd", self.pos):
            raise _Malformed
        self.pos += 2

    def ended(self) -> bool:
        if self.data[self.pos:self.pos + 1] == b"\xff":
            self.pos += 1
            return True
        if self.pos == len(self.data):
            raise _Malformed
        return False

    def value(self, depth: int) -> int | str | None:
        start = self.pos
        tag = self.take(1)[0]
        if tag == 7:
            self.skip_container(depth + 1)
            return None
        try:
            if tag == 1:
                return struct.unpack("<i", self.take(4))[0]
            if tag == 0:
                end = self.pos
                while end + 1 < len(self.data):
                    if self.data[end:end + 2] == b"\0\0":
                        result = self.data[self.pos:end].decode("utf-16-le")
                        self.pos = end + 2
                        return result
                    end += 2
            if tag == 0xfb:
                # Sample offset 213: a9 08 00 00 skips 2217 opaque bytes to FF FF FF.
                length = struct.unpack("<I", self.take(4))[0]
                if length <= len(self.data) - self.pos:
                    self.pos += length
                    return None
        except (UnicodeError, _Malformed):
            pass
        self.pos = start
        raise _Malformed

    def skip_container(self, depth: int) -> None:
        self.start(depth)
        while not self.ended():
            self.value(depth)


def decode_info(data: bytes) -> tuple[list[Message], int | None]:
    """Return decoded messages and bytes left at the first fault (None on success)."""
    if not data:
        return [], None
    if len(data) > MAX_INFO_BYTES:
        return [], len(data)
    reader = _Container(data)
    messages: list[Message] = []
    try:
        reader.start(1)
        if reader.ended():
            return [], len(data) - reader.pos or None
        version = reader.pos
        if (reader.take(1) != b"\x07" or not data.startswith(b"\x07\xfd\x01\x01\0\0\0\xff", reader.pos)):
            reader.pos = version
            raise _Malformed
        reader.pos += 8
        while not reader.ended():
            start = reader.pos
            if len(messages) >= MAX_ENTRIES or not data.startswith(b"\x07", start):
                raise _Malformed
            reader.pos += 1
            reader.start(2)
            kind = reader.value(2)
            text = reader.value(2)
            if type(kind) is not int or not isinstance(text, str):
                reader.pos = start
                raise _Malformed
            message_id = ""
            try:
                if reader.ended():
                    continue
                id_pos = reader.pos
                value = reader.value(2)
                if not isinstance(value, str):
                    reader.pos = id_pos
                    raise _Malformed
                message_id = value
                while not reader.ended():
                    reader.value(2)
            finally:
                messages.append(Message(kind, text, message_id))
    except _Malformed:
        return messages, len(data) - reader.pos
    return messages, len(data) - reader.pos or None
