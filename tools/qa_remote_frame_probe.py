"""Opt-in, isolated-fixture diagnostic for native QUIC payload reads.

Call ``install_frame_probe()`` before starting the QA fixture's endpoint and set
``AURA_REMOTE_FRAME_PROBE=1``. This module is never imported by the desktop app.
The probe changes only this fixture process: the four-byte header still uses
native read_exact, while the body uses incremental native read calls. It logs
only local ordinals, static stages, declared lengths and received byte counts.
Use it exclusively with synthetic QA traffic; no payload or identity is logged.
"""

from __future__ import annotations

import itertools
import os
import struct
import sys

from desktop_pet.remote.protocol import ProtocolError


def _emit(number: int, stage: str, *, declared: int | None = None,
          received: int | None = None) -> None:
    line = f"AURA_REMOTE_FRAME_PROBE frame={number} stage={stage}"
    if declared is not None:
        line += f" declared={declared}"
    if received is not None:
        line += f" received={received}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


class _IncrementalReader:
    def __init__(self, recv, number: int, emit=_emit):
        self.recv = recv
        self.number = number
        self.emit = emit
        self.header_read = False

    async def read_exact(self, size: int) -> bytes:
        if not self.header_read:
            self.header_read = True
            header = await self.recv.read_exact(size)
            if len(header) == 4:
                self.emit(self.number, "header_declared", declared=struct.unpack(">I", header)[0])
            else:
                self.emit(self.number, "header_incomplete", received=len(header))
            return header
        payload = bytearray()
        self.emit(self.number, "payload_begin", declared=size, received=0)
        try:
            while len(payload) < size:
                maximum = min(1024, size - len(payload))
                chunk = await self.recv.read(maximum)
                if not isinstance(chunk, bytes) or len(chunk) > maximum:
                    raise ProtocolError("invalid_frame", "Invalid frame payload read.")
                if not chunk:
                    self.emit(self.number, "payload_eof", declared=size, received=len(payload))
                    return bytes(payload)
                payload.extend(chunk)
                self.emit(self.number, "payload_chunk", declared=size, received=len(payload))
        except BaseException:
            # Cancellation/error text and native exception details may contain
            # sensitive values. Only the progress counter leaves this wrapper.
            self.emit(self.number, "payload_interrupted", declared=size, received=len(payload))
            raise
        self.emit(self.number, "payload_complete", declared=size, received=len(payload))
        return bytes(payload)


def install_frame_probe() -> bool:
    """Install once in an isolated fixture, only with the explicit env switch."""
    if os.environ.get("AURA_REMOTE_FRAME_PROBE") != "1":
        return False
    from desktop_pet.remote import transport
    if getattr(transport.read_frame, "_aura_qa_incremental_probe", False):
        return True
    original = transport.read_frame
    numbers = itertools.count(1)

    async def read_frame(recv, **kwargs):
        return await original(_IncrementalReader(recv, next(numbers)), **kwargs)

    read_frame._aura_qa_incremental_probe = True
    transport.read_frame = read_frame
    return True
