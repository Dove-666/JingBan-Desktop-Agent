"""Close-error epistemic boundary; Python's closed property is not OS proof."""
from __future__ import annotations

import threading
from types import SimpleNamespace
import unittest

from desktop_pet.agents.guardian_worker import _TrackedPipe, _CloseAuditedNative


class PipeCloseFailureTests(unittest.TestCase):
    def test_failed_legacy_native_close_is_sticky_and_not_replayed_from_scalar_handle(self):
        calls = []
        base = SimpleNamespace(k=SimpleNamespace(CloseHandle=lambda handle: calls.append(handle) or 0), a=None, c=None)
        native = _CloseAuditedNative(base)
        self.assertFalse(native.close(71))
        self.assertTrue(native.unconfirmed_close)
        self.assertFalse(native.close(71))
        self.assertEqual(calls, [71])

    def test_closed_property_after_close_exception_never_becomes_confirmed_cleanup(self):
        class UncertainFile:
            closed = False
            calls = 0
            def close(self):
                self.calls += 1
                self.closed = True
                if self.calls == 1:
                    raise OSError("fixed synthetic close failure")
        file = UncertainFile()
        pipe = object.__new__(_TrackedPipe)
        pipe._lock = threading.Lock()
        pipe._active = 0
        pipe._closing = False
        pipe._close_failure = False
        pipe._raw_handle, pipe._descriptor, pipe._file = 0, None, file
        pipe._native = SimpleNamespace()
        self.assertFalse(pipe.close_checked())
        self.assertTrue(file.closed)
        self.assertFalse(pipe.close_checked(), "A second no-op close cannot confirm the first failed OS close")
        self.assertEqual(file.calls, 1, "Unknown close must not be replayed")


if __name__ == "__main__": unittest.main()
