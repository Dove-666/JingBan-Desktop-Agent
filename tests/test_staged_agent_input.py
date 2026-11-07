"""Actual owned-process JSONL staging faults, no model or user process."""
from contextlib import contextmanager
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from desktop_pet.agents.staged_input import StagedRunInput, StagedInputError


class StagedAgentInputTests(unittest.TestCase):
    def spawn(self, body):
        process = subprocess.Popen([sys.executable, "-I", "-u", "-c", body],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if sys.platform == "win32" else 0, bufsize=0)
        self.addCleanup(self.close_process, process)
        self.assertEqual(process.stdout.readline().rstrip(b"\r\n"), b"READY")
        return process

    @staticmethod
    def close_process(process):
        if process.poll() is None: process.kill()
        process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout): pipe.close()

    def test_actual_reader_cannot_observe_complete_command_before_commit_lf(self):
        with tempfile.TemporaryDirectory(prefix="aura-stage-input-") as owned:
            target = Path(owned) / "received.bin"
            payload = b'{"ok":true}'
            process = self.spawn("import sys\nfrom pathlib import Path\nprint('READY',flush=True)\n"
                f"prefix=sys.stdin.buffer.read({len(payload)})\nprint('STAGED',flush=True)\n"
                f"Path({str(target)!r}).write_bytes(prefix+sys.stdin.buffer.readline())\n")
            writer = StagedRunInput(process.stdin, stop_owned=process.kill, cancellation_event=threading.Event())
            writer.start()
            try:
                writer.stage(payload)
                self.assertEqual(process.stdout.readline().rstrip(b"\r\n"), b"STAGED")
                self.assertFalse(target.exists())
                writer.commit()
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertEqual(target.read_bytes(), payload + b"\n")
            finally: self.assertTrue(writer.close())

    def test_actual_ready_process_that_never_reads_input_has_bounded_stage_and_is_reaped(self):
        process = self.spawn("import threading\nprint('READY',flush=True)\nthreading.Event().wait(30)\n")
        writer = StagedRunInput(process.stdin, stop_owned=process.kill, cancellation_event=threading.Event(), stage_timeout=.15)
        writer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(StagedInputError, "WORKER_INPUT_STAGE_TIMEOUT"):
                writer.stage(b"x" * (4 * 1024 * 1024))
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNotNone(process.wait(timeout=3))
            with self.assertRaises(StagedInputError): writer.commit()
        finally: self.assertTrue(writer.close())

    def test_cancellation_interrupts_actual_blocked_owned_input_without_committing(self):
        process = self.spawn("import threading\nprint('READY',flush=True)\nthreading.Event().wait(30)\n")
        entered, cancel = threading.Event(), threading.Event()
        class ObservedPipe:
            def write(self, data): entered.set(); return process.stdin.write(data)
            def flush(self): return process.stdin.flush()
        writer = StagedRunInput(ObservedPipe(), stop_owned=process.kill, cancellation_event=cancel)
        writer.start()
        failures = []
        def write():
            try: writer.stage(b"x" * (4 * 1024 * 1024))
            except StagedInputError as exc: failures.append(exc.code)
        thread = threading.Thread(target=write)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancel.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, ["WORKER_INPUT_CANCELLED"])
            self.assertIsNotNone(process.wait(timeout=3))
        finally:
            process.kill() if process.poll() is None else None
            thread.join(3)
            self.assertTrue(writer.close())

    def test_final_single_byte_deadline_and_no_stage_replay(self):
        stopped, committed = threading.Event(), []
        class Pipe:
            def write(self, data):
                raw = bytes(data)
                if raw == b'\n':
                    committed.append(raw)
                    stopped.wait(2)
                    raise OSError('owned deadline fixture')
                return len(raw)
            def flush(self): pass
        writer = StagedRunInput(Pipe(), stop_owned=stopped.set, cancellation_event=threading.Event(), commit_timeout=.1)
        writer.start()
        try:
            writer.stage(b'{}')
            with self.assertRaisesRegex(StagedInputError, "WORKER_INPUT_COMMIT_TIMEOUT"): writer.commit()
            with self.assertRaises(StagedInputError): writer.commit()
            self.assertEqual(committed, [b'\n'])
        finally: self.assertTrue(writer.close())

    def test_embedded_cr_or_lf_cannot_bypass_staged_commit(self):
        for payload in (b'{}\n', b'{}\r', b'{}\r\n', b''):
            writer = StagedRunInput(io.BytesIO(), stop_owned=lambda: None, cancellation_event=threading.Event())
            writer.start()
            try:
                with self.assertRaisesRegex(StagedInputError, "WORKER_INPUT_FRAME_INVALID"): writer.stage(payload)
            finally: self.assertTrue(writer.close())


if __name__ == "__main__": unittest.main()
