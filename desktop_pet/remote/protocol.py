"""Small, bounded JSON frames shared by the encrypted remote peers.

No identities or authorization decisions are taken from the JSON payload. The
transport supplies the identity authenticated by the QUIC connection instead.
"""

from __future__ import annotations

import json
import re
import struct
from typing import Any
from uuid import UUID

ALPN = b"aura-remote/1"
MAX_FRAME_BYTES = 8 * 1024 * 1024
UNPAIRED_MAX_FRAME_BYTES = 16 * 1024
REQUEST_TIMEOUT = 30.0
MAX_CONCURRENT_REQUESTS = 4
_METHOD = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")


class ProtocolError(ValueError):
    """An error whose code and message are safe to return to a remote peer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_response(request_id: str | None, code: str, message: str) -> dict:
    return {"id": request_id, "ok": False,
            "error": {"code": code, "message": message}}


def validate_request(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "method", "params"}:
        raise ProtocolError("invalid_request", "Expected id, method and params.")
    request_id = value["id"]
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProtocolError("invalid_request", "A canonical UUID request id is required.") from None
    method = value["method"]
    if not isinstance(method, str) or len(method) > 64 or not _METHOD.fullmatch(method):
        raise ProtocolError("invalid_request", "Invalid request method.")
    if not isinstance(value["params"], dict):
        raise ProtocolError("invalid_request", "Request params must be an object.")
    return value


def encode_frame(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_frame", "A JSON object is required.")
    try:
        payload = bytearray()
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for fragment in encoder.iterencode(value):
            encoded = fragment.encode("utf-8")
            if len(payload) + len(encoded) > MAX_FRAME_BYTES:
                raise ProtocolError("frame_too_large", "Frame exceeds the permitted size; reduce the page or attachment size.")
            payload.extend(encoded)
    except ProtocolError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_frame", "Could not encode JSON object.") from None
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "Frame exceeds the permitted size.")
    return struct.pack(">I", len(payload)) + bytes(payload)


def _object_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def decode_payload(payload: bytes) -> dict:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "Frame exceeds the permitted size.")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_object_pairs,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_frame", "Invalid UTF-8 JSON object.") from None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_frame", "A JSON object is required.")
    return value


async def read_frame(recv: Any, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> dict:
    """Read a single frame; reject its length before allocating the payload."""
    header = await recv.read_exact(4)
    if len(header) != 4:
        raise ProtocolError("invalid_frame", "Incomplete frame header.")
    size = struct.unpack(">I", header)[0]
    if size == 0 or size > min(max_frame_bytes, MAX_FRAME_BYTES):
        raise ProtocolError("frame_too_large", "Frame exceeds the permitted size.")
    payload = await recv.read_exact(size)
    if len(payload) != size:
        raise ProtocolError("invalid_frame", "Incomplete frame payload.")
    return decode_payload(payload)


async def write_frame(send: Any, value: dict) -> None:
    await send.write_all(encode_frame(value))
    await send.finish()
