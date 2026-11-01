"""Isolated diagnostics preserve framing while exposing only byte counts."""

import asyncio
from contextlib import redirect_stderr
import io
import os
import struct
import unittest
from unittest.mock import patch

from desktop_pet.remote import transport
from desktop_pet.remote.protocol import ProtocolError, encode_frame, read_frame
from tools.qa_remote_frame_probe import _IncrementalReader, install_frame_probe


class Reader:
    def __init__(self, data, piece_size=301):
        self.data = data
        self.piece_size = piece_size
        self.exact_sizes = []
        self.read_sizes = []

    async def read_exact(self, size):
        self.exact_sizes.append(size)
        part, self.data = self.data[:size], self.data[size:]
        return part

    async def read(self, size):
        self.read_sizes.append(size)
        count = min(size, self.piece_size)
        part, self.data = self.data[:count], self.data[count:]
        return part


class FrameProbeTests(unittest.TestCase):
    def test_payload_is_incremental_and_diagnostics_contain_counts_only(self):
        value = {"private": "never-log-this" * 800}
        frame = encode_frame(value)
        reader = Reader(frame)
        events = []
        proxy = _IncrementalReader(reader, 1, lambda *args, **kwargs: events.append((args, kwargs)))
        self.assertEqual(asyncio.run(read_frame(proxy)), value)
        self.assertEqual(reader.exact_sizes, [4])
        self.assertTrue(reader.read_sizes)
        self.assertLessEqual(max(reader.read_sizes), 1024)
        self.assertEqual(events[0], ((1, "header_declared"), {"declared": len(frame) - 4}))
        self.assertEqual(events[-1], ((1, "payload_complete"),
                                     {"declared": len(frame) - 4, "received": len(frame) - 4}))
        self.assertNotIn("never-log-this", repr(events))

    def test_length_rejection_still_precedes_payload_read(self):
        reader = Reader(struct.pack(">I", 50000))
        proxy = _IncrementalReader(reader, 1, lambda *args, **kwargs: None)
        with self.assertRaises(ProtocolError) as error:
            asyncio.run(read_frame(proxy, max_frame_bytes=16384))
        self.assertEqual(error.exception.code, "frame_too_large")
        self.assertEqual(reader.read_sizes, [])

    def test_truncated_body_still_fails_and_reports_actual_received_count(self):
        frame = encode_frame({"private": "not-logged"})
        reader = Reader(frame[:-3])
        events = []
        proxy = _IncrementalReader(reader, 1, lambda *args, **kwargs: events.append((args, kwargs)))
        with self.assertRaises(ProtocolError) as error:
            asyncio.run(read_frame(proxy))
        self.assertEqual(error.exception.code, "invalid_frame")
        self.assertEqual(events[-1], ((1, "payload_eof"),
                                     {"declared": len(frame) - 4, "received": len(frame) - 7}))

    def test_cancellation_is_preserved_after_partial_body(self):
        class CancelReader(Reader):
            async def read(self, size):
                if self.read_sizes:
                    raise asyncio.CancelledError()
                return await super().read(size)
        reader = CancelReader(encode_frame({"private": "x" * 2000}))
        events = []
        proxy = _IncrementalReader(reader, 1, lambda *args, **kwargs: events.append((args, kwargs)))
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(read_frame(proxy))
        self.assertEqual(events[-1][0], (1, "payload_interrupted"))
        self.assertEqual(events[-1][1]["received"], 301)

    def test_install_is_disabled_by_default_and_idempotent_when_opted_in(self):
        original = transport.read_frame
        self.addCleanup(setattr, transport, "read_frame", original)
        with patch.dict(os.environ, {"AURA_REMOTE_FRAME_PROBE": "0"}):
            self.assertFalse(install_frame_probe())
            self.assertIs(transport.read_frame, original)
        output = io.StringIO()
        with patch.dict(os.environ, {"AURA_REMOTE_FRAME_PROBE": "1"}), redirect_stderr(output):
            self.assertTrue(install_frame_probe())
            installed = transport.read_frame
            self.assertTrue(install_frame_probe())
            self.assertIs(transport.read_frame, installed)
            self.assertEqual(asyncio.run(transport.read_frame(Reader(encode_frame({"secret": "not-logged"})))),
                             {"secret": "not-logged"})
        self.assertIn("stage=header_declared", output.getvalue())
        self.assertIn("stage=payload_complete", output.getvalue())
        self.assertNotIn("not-logged", output.getvalue())


if __name__ == "__main__":
    unittest.main()
