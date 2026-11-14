"""Bounded plain-HTTP final-header relay; CONNECT/TLS is outside this scope.

An upstream failure before final headers receives an explicit 502 instead of an
empty response that a browser may replay. This neither retries the request nor
proves that its remote effect did not happen. All body ownership stays with the
caller's existing bounded pipe and cleanup path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time

GATEWAY_UNKNOWN = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_FIELD = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


@dataclass
class HttpResponseState:
    final_headers_forwarded: bool = False


async def relay_final_http_response(reader, writer, body_pipe, *, state, timeout=15.0, max_headers=32768):
    deadline = time.monotonic() + timeout
    blocks, total = [], 0
    try:
        for _ in range(9):
            block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), max(0, deadline - time.monotonic()))
            total += len(block)
            if total > max_headers: raise ValueError("HTTP_RESPONSE_HEADERS_TOO_LARGE")
            lines = block[:-4].split(b"\r\n")
            match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?", lines[0])
            if not match: raise ValueError("HTTP_RESPONSE_STATUS_INVALID")
            code = int(match[1])
            lengths, transfers = [], []
            for line in lines[1:]:
                name, separator, value = line.partition(b":")
                if not separator or not _FIELD.fullmatch(name) or any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                    raise ValueError("HTTP_RESPONSE_HEADER_INVALID")
                if name.lower() == b"content-length": lengths.append(value.strip())
                if name.lower() == b"transfer-encoding": transfers.append(value.strip())
            if len(lengths) > 1 or any(not value.isdigit() for value in lengths):
                raise ValueError("HTTP_RESPONSE_LENGTH_INVALID")
            # Reject contradictory framing rather than forwarding a length
            # alongside Transfer-Encoding. Informational/204 replies cannot
            # carry a transfer coding or length (RFC 9112 sections 6.1/6.3;
            # RFC 9110 section 8.6).
            if (transfers and lengths) or ((transfers or lengths) and (code < 200 or code == 204)):
                raise ValueError("HTTP_RESPONSE_FRAMING_INVALID")
            blocks.append(block)
            if code in {100, 102, 103}: continue
            if not 200 <= code <= 599: raise ValueError("HTTP_RESPONSE_FINAL_HEADER_MISSING")
            break
        else: raise ValueError("HTTP_RESPONSE_INTERIM_LIMIT")
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        # Set before write/drain: even a partially failed write must never be
        # followed by the outer proxy's 403 response or another remote request.
        state.final_headers_forwarded = True
        writer.write(GATEWAY_UNKNOWN)
        await asyncio.wait_for(writer.drain(), timeout)
        return False
    state.final_headers_forwarded = True
    writer.write(b"".join(blocks))
    await asyncio.wait_for(writer.drain(), timeout)
    await body_pipe(reader, writer)
    return True
