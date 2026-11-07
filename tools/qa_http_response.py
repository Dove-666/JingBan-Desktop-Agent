"""QA-only candidate: do not expose an empty/partial final HTTP header to Chrome.

This helper is not connected to production and cannot inspect CONNECT/TLS.
It buffers bounded interim+final headers, or sends an explicit 502 on header
failure. A 502 still means the remote effect is unknown, never undone/successful.
"""
from __future__ import annotations
import asyncio
import re
import time

GATEWAY_UNKNOWN = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_FIELD = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


async def relay_final_http_response(reader, writer, body_pipe, *, timeout=2.0, max_headers=32768):
    deadline = time.monotonic() + timeout
    blocks, total = [], 0
    try:
        for _ in range(9):
            block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), max(0, deadline - time.monotonic()))
            total += len(block)
            if total > max_headers: raise ValueError("QA_RESPONSE_HEADERS_TOO_LARGE")
            lines = block[:-4].split(b"\r\n")
            match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?", lines[0])
            if not match: raise ValueError("QA_RESPONSE_STATUS_INVALID")
            code = int(match[1])
            lengths = []
            for line in lines[1:]:
                name, separator, value = line.partition(b":")
                if not separator or not _FIELD.fullmatch(name) or any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                    raise ValueError("QA_RESPONSE_HEADER_INVALID")
                if name.lower() == b"content-length": lengths.append(value.strip())
            if len(lengths) > 1 or any(not value.isdigit() for value in lengths):
                raise ValueError("QA_RESPONSE_LENGTH_INVALID")
            blocks.append(block)
            if code in {100, 102, 103}: continue
            if not 200 <= code <= 599: raise ValueError("QA_RESPONSE_FINAL_HEADER_MISSING")
            break
        else: raise ValueError("QA_RESPONSE_INTERIM_LIMIT")
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.write(GATEWAY_UNKNOWN)
        await writer.drain()
        return False
    writer.write(b"".join(blocks))
    await writer.drain()
    await body_pipe(reader, writer)
    return True
