"""Independent fixed process/pipe review. No models or physical desktop.

Synthetic readers self-limit. Faulty pipe/watchdog callbacks are always released
in finally; only exact locally created Popen/job handles may be stopped.
"""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as host
from desktop_pet.agents.staged_input import StagedRunInput, StagedInputError
from tests import test_agent_runtime as runtime_support


READY = {"type": "ready", "protocol": 1, "sdk": "nanobot-ai", "sdk_version": "0.4.0"}


def spawn_sleep():
    process = subprocess.Popen([sys.executable, "-I", "-u", "-c", "import time; print('QA_READY',flush=True);time.sleep(15)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        creationflags=0x08000000 if sys.platform == "win32" else 0)
    if process.stdout.readline().strip() != b"QA_READY":
        process.kill(); process.wait(timeout=2)
        raise AssertionError("Fixed owned reader failed")
    return process


def close_process(process):
    if process.poll() is None: process.kill()
    process.wait(timeout=3)
    for pipe in (process.stdin, process.stdout):
        if not pipe.closed: pipe.close()


class StagedPipeReviewTests(unittest.TestCase):
    def bind(self, process):
        service, cancel = AgentService(), threading.Event()
        handle = service._worker_lifecycle.register(process, "qa-fixed-staged-run", str(uuid4()))
        tree = host._ProcessTree(process)
        service._worker_lifecycle.attach_tree(handle, tree)
        service._worker_handles[process.pid] = handle
        service._processes[process.pid] = cancel, tree
        self.addCleanup(service.shutdown)
        return service, cancel, handle

    def test_partial_io_failure_is_terminal_and_cannot_append_or_commit(self):
        accepted, calls = bytearray(), []
        class Pipe:
            def write(self, raw):
                calls.append(bytes(raw))
                if len(calls) == 1:
                    accepted.extend(bytes(raw)[:3]); return 3
                raise OSError("QA_PRIVATE_WRITER_EXCEPTION")
            def flush(self): pass
        writer = StagedRunInput(Pipe(), stop_owned=lambda: None, cancellation_event=threading.Event())
        writer.start()
        try:
            with self.assertRaisesRegex(StagedInputError, "WORKER_INPUT_WRITE_FAILED") as failed:
                writer.stage(b'{"qa":true}')
            self.assertNotIn("QA_PRIVATE", str(failed.exception))
            count = len(calls)
            with self.assertRaises(StagedInputError): writer.stage(b'"suffix":1}')
            with self.assertRaises(StagedInputError): writer.commit()
            self.assertEqual(len(calls), count)
            self.assertNotIn(b"\n", accepted)
        finally:
            self.assertTrue(writer.close())

    @unittest.skipUnless(sys.platform == "win32", "exact native owned-job proof")
    def test_active_noncooperating_writer_cannot_mint_receipt_or_free_registry(self):
        process = spawn_sleep(); self.addCleanup(close_process, process)
        service, cancel, handle = self.bind(process)
        entered, release, failures = threading.Event(), threading.Event(), []
        class Pipe:
            def write(self, raw): entered.set(); release.wait(5); return len(raw)
            def flush(self): pass
        writer = StagedRunInput(Pipe(), stop_owned=lambda: service._worker_lifecycle.request_stop(handle), cancellation_event=cancel, stage_timeout=.05)
        service._worker_input_writers[handle] = writer
        writer.start()
        def write():
            try: writer.stage(b'{"qa":true}')
            except StagedInputError as failure: failures.append(failure.code)
        thread = threading.Thread(target=write, daemon=True); thread.start()
        try:
            self.assertTrue(entered.wait(1)); process.wait(timeout=2)
            self.assertTrue(thread.is_alive())
            self.assertIsNone(service._settle_worker(handle, timeout_seconds=.05))
            self.assertIsNone(service.worker_closure(handle))
            self.assertIn(process.pid, service._processes)
            self.assertIn(handle, service._worker_input_writers)
            with self.assertRaises(AgentError) as busy:
                service.execute(AgentSettings(), "unused", AgentRequest("Fixed QA task", "agent"), {})
            self.assertEqual(busy.exception.code, "busy")
        finally:
            release.set(); thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(writer.close())
            receipts = service.reconcile_worker_closures(timeout_seconds=.2)
            self.assertEqual(len(receipts), 1)
            self.assertTrue(service.validate_worker_closure(handle, receipts[0]))
            self.assertFalse(service._processes)
        self.assertEqual(failures, ["WORKER_INPUT_STAGE_TIMEOUT"])

    @unittest.skipUnless(sys.platform == "win32", "exact native owned-job proof")
    def test_unjoined_watchdog_retains_registry_even_after_parent_reaping(self):
        process = spawn_sleep(); self.addCleanup(close_process, process)
        service, cancel, handle = self.bind(process)
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set(); release.wait(5)
            service._worker_lifecycle.request_stop(handle)
        writer = StagedRunInput(io.BytesIO(), stop_owned=stop, cancellation_event=cancel)
        service._worker_input_writers[handle] = writer
        writer.start(); writer.stage(b'{}'); cancel.set()
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNone(service._settle_worker(handle, timeout_seconds=.02))
            self.assertIn(process.pid, service._processes)
            self.assertIn(handle, service._worker_input_writers)
            self.assertIsNone(service.worker_closure(handle))
        finally:
            release.set(); self.assertTrue(writer.close())
            receipts = service.reconcile_worker_closures(timeout_seconds=.2)
            self.assertEqual(len(receipts), 1)
            self.assertTrue(service.validate_worker_closure(handle, receipts[0]))

    @unittest.skipUnless(sys.platform == "win32", "exact native owned-job proof")
    def test_watchdog_stops_only_its_exact_owned_tree(self):
        owned, control = spawn_sleep(), spawn_sleep()
        self.addCleanup(close_process, owned); self.addCleanup(close_process, control)
        service, cancel, handle = self.bind(owned)
        writer = StagedRunInput(owned.stdin, stop_owned=lambda: service._worker_lifecycle.request_stop(handle), cancellation_event=cancel, stage_timeout=.08)
        writer.start()
        try:
            with self.assertRaisesRegex(StagedInputError, "WORKER_INPUT_STAGE_TIMEOUT"):
                writer.stage(b'x' * (8 * 1024 * 1024))
            owned.wait(timeout=2)
            self.assertIsNone(control.poll())
            receipt = service._settle_worker(handle, timeout_seconds=.2)
            self.assertTrue(service.validate_worker_closure(handle, receipt))
        finally:
            self.assertTrue(writer.close())


class ServiceGuardReviewTests(unittest.TestCase):
    def fixture(self, body, *, checkpoint=False, reader=True):
        fixture = runtime_support.AgentRuntimeTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        if checkpoint:
            fixture.service.shutdown()
            fixture.service = AgentService(checkpoint_path=fixture.root / "checkpoint.sqlite3")
            fixture.context["conversation_id"] = str(uuid4())
        header = "import sys,json,time,os,threading\n_timer=threading.Timer(15,lambda:os._exit(91));_timer.daemon=True;_timer.start()\n"
        header += "sys.stdin.reconfigure(encoding='utf-8',errors='strict');sys.stdout.reconfigure(encoding='utf-8',errors='strict')\n"
        header += "print(" + repr(json.dumps(READY)) + ",flush=True)\n"
        if reader:
            header += "request=json.loads(sys.stdin.readline());task_id=request['id']\n"
            header += "def send(frame):\n frame.setdefault('id',task_id);print(json.dumps(frame),flush=True)\n"
        fixture.worker.write_text(header + body, encoding="utf-8")
        return fixture

    def test_successful_guard_commits_exactly_one_lf_and_then_reader_executes(self):
        fixture = self.fixture("")
        marker = fixture.root / "accepted-command"
        fixture.worker.write_text(fixture.worker.read_text() + f"open({str(marker)!r},'w').write('QA')\nsend({{'type':'final','reply':'Fixed staged result','status':'completed'}})\n")
        state, writes, handles = {"held": False, "handle": None}, [], []
        original = StagedRunInput
        class Observed(original):
            def __init__(self, pipe, **options):
                class Pipe:
                    def write(self, raw):
                        data = bytes(raw)
                        writes.append((data, state["held"]))
                        if state["held"]:
                            if data != b"\n": raise AssertionError("Only LF inside authority guard")
                            if not fixture.service._worker_lifecycle._get(state["handle"]).dispatched:
                                raise AssertionError("Commit intent must precede LF")
                        return pipe.write(raw)
                    def flush(self): return pipe.flush()
                super().__init__(Pipe(), **options)
        @contextmanager
        def guard(handle):
            self.assertFalse(marker.exists())
            state.update(held=True, handle=handle)
            try: yield
            finally: state["held"] = False
        with patch.object(host, "StagedRunInput", Observed):
            result = fixture.execute(worker_dispatch_guard=guard, worker_lifecycle_callback=handles.append)
        self.assertEqual(result.reply, "Fixed staged result")
        self.assertEqual(marker.read_text(), "QA")
        self.assertEqual([data for data, held in writes if held], [b"\n"])
        staged = b"".join(data for data, held in writes if not held)
        self.assertNotIn(b"\n", staged)
        self.assertEqual(json.loads(staged)["type"], "run")
        receipt = fixture.service.worker_closure(handles[0])
        self.assertTrue(fixture.service.validate_worker_closure(handles[0], receipt))

    def test_no_lf_before_guard_and_original_authority_error_survives_unknown_cleanup(self):
        fixture = self.fixture("")
        marker = fixture.root / "must-not-execute"
        fixture.worker.write_text(fixture.worker.read_text() + f"open({str(marker)!r},'w').write('QA')\n")
        original = StagedRunInput; writes, guarded, handles = [], [], []
        class Observed(original):
            def __init__(self, pipe, **options):
                class Pipe:
                    def write(self, raw): writes.append((bytes(raw), bool(guarded))); return pipe.write(raw)
                    def flush(self): return pipe.flush()
                super().__init__(Pipe(), **options)
        failure = AgentError("cancelled", "Fixed host authorization revoked")
        @contextmanager
        def deny(handle):
            guarded.append(handle)
            raise failure
            yield
        with patch.object(host, "StagedRunInput", Observed), patch.object(host._ProcessTree, "confirm_closed", return_value=False):
            with self.assertRaises(AgentError) as caught:
                fixture.execute(worker_dispatch_guard=deny, worker_lifecycle_callback=handles.append)
        self.assertIs(caught.exception, failure)
        self.assertFalse(marker.exists())
        self.assertEqual(len(guarded), 1); self.assertEqual(len(handles), 1)
        self.assertTrue(writes and all(not held and b"\n" not in data for data, held in writes))
        self.assertIsNone(fixture.service.worker_closure(handles[0]))
        self.assertTrue(fixture.service._processes)
        receipts = fixture.service.reconcile_worker_closures(timeout_seconds=.2)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(fixture.service.validate_worker_closure(handles[0], receipts[0]))

    def test_checkpoint_ack_is_after_sqlite_commit_and_rechecks_current_guard(self):
        fixture = self.fixture("", checkpoint=True)
        first, second = fixture.root / "first-ack", fixture.root / "second-ack"
        body = "send({'type':'session','message':{'role':'user','content':request['task']}})\n"
        body += "send({'type':'checkpoint','barrier_id':'checkpoint-1'})\nack=json.loads(sys.stdin.readline())\n"
        body += f"open({str(first)!r},'w').write(ack['barrier_id'])\n"
        body += "send({'type':'checkpoint','barrier_id':'checkpoint-2'})\nack=json.loads(sys.stdin.readline())\n"
        body += f"open({str(second)!r},'w').write(ack['barrier_id'])\n"
        fixture.worker.write_text(fixture.worker.read_text() + body)
        guard_calls, committed = [], []
        failure = AgentError("cancelled", "Fixed checkpoint authorization revoked")
        @contextmanager
        def guard(handle):
            guard_calls.append(handle)
            if len(guard_calls) > 1:
                with sqlite3.connect(fixture.root / "checkpoint.sqlite3") as connection:
                    value = connection.execute("SELECT last_barrier FROM agent_checkpoints").fetchone()[0]
                committed.append(value)
            if len(guard_calls) == 3:
                self.assertTrue(first.exists()); raise failure
            yield
        original_control = fixture.service._write_control
        with patch.object(fixture.service, "_write_control", wraps=original_control) as controls:
            with self.assertRaises(AgentError) as caught: fixture.execute(worker_dispatch_guard=guard)
        self.assertIs(caught.exception, failure, {"code": caught.exception.code, "guard_calls": len(guard_calls), "committed": committed})
        self.assertEqual(len(guard_calls), 3)
        self.assertEqual(committed, [1, 2])
        self.assertEqual([call.args[1]["barrier_id"] for call in controls.call_args_list], ["checkpoint-1"])
        self.assertEqual(first.read_text(), "checkpoint-1"); self.assertFalse(second.exists())

    def test_wrong_ready_version_never_constructs_a_writer_or_enters_guard(self):
        fixture = self.fixture("time.sleep(15)\n", reader=False)
        fixture.worker.write_text(fixture.worker.read_text().replace("0.4.0", "0.4.999"))
        called = []
        @contextmanager
        def guard(handle): called.append(handle); yield
        with patch.object(host, "StagedRunInput", side_effect=AssertionError("No first write before valid ready")) as writer:
            with self.assertRaises(AgentError) as caught: fixture.execute(worker_dispatch_guard=guard)
        self.assertEqual(caught.exception.code, "protocol")
        writer.assert_not_called(); self.assertEqual(called, [])

    def test_qt_revoke_lock_remains_available_during_eight_mib_first_write(self):
        from PySide6.QtCore import QCoreApplication, QTimer
        app = QCoreApplication.instance() or QCoreApplication([])
        fixture = self.fixture("time.sleep(15)\n", reader=False)
        fixture.context["messages"] = [{"role": "user", "content": "x" * (8 * 1024 * 1024)}]
        lock, entered, cancel = threading.Lock(), threading.Event(), threading.Event()
        guard_calls, outcomes, ticks, revocation = [], [], [], []
        original = StagedRunInput
        class Observed(original):
            def __init__(self, pipe, **options):
                class Pipe:
                    def write(self, raw):
                        if len(raw) >= 8 * 1024 * 1024: entered.set()
                        return pipe.write(raw)
                    def flush(self): return pipe.flush()
                super().__init__(Pipe(), **options)
        @contextmanager
        def guard(handle):
            with lock: guard_calls.append(handle); yield
        def run():
            try: fixture.execute(cancellation_event=cancel, worker_dispatch_guard=guard)
            except AgentError as error: outcomes.append(error.code)
        timer = QTimer(); timer.setInterval(1); timer.timeout.connect(lambda: ticks.append(True)); timer.start()
        with patch.object(host, "StagedRunInput", Observed):
            thread = threading.Thread(target=run, daemon=True); thread.start()
            try:
                self.assertTrue(entered.wait(3))
                def revoke():
                    started = time.monotonic()
                    acquired = lock.acquire(timeout=.05)
                    revocation.append((acquired, time.monotonic() - started))
                    if acquired:
                        cancel.set(); lock.release()
                    fixture.service.cancel_request(cancel)
                QTimer.singleShot(0, revoke)
                deadline = time.monotonic() + 3
                while thread.is_alive() and time.monotonic() < deadline:
                    app.processEvents(); time.sleep(.001)
                thread.join(.2)
                self.assertFalse(thread.is_alive())
            finally:
                timer.stop(); cancel.set(); fixture.service.cancel_request(cancel); thread.join(3)
        self.assertTrue(ticks)
        self.assertTrue(revocation and revocation[0][0]); self.assertLess(revocation[0][1], .05)
        self.assertEqual(guard_calls, [])
        self.assertEqual(outcomes, ["cancelled"])
        self.assertEqual(len(fixture.spawned), 1)


class ProductionReaderReviewTests(unittest.TestCase):
    @contextmanager
    def fixed_main(self):
        root = host._ROOT / "agent_workspace"; root.mkdir(exist_ok=True)
        backend = host._ROOT / "agent_backend" / "nanobot"
        with tempfile.TemporaryDirectory(prefix="staged-eof-reader-review-", dir=root) as directory:
            owned = Path(directory); marker, wrapper = owned / "entered-run", owned / "fixed-reader.py"
            wrapper.write_text("import sys,asyncio,threading,os\nfrom pathlib import Path\n"
                "_timer=threading.Timer(15,lambda:os._exit(91));_timer.daemon=True;_timer.start()\n"
                + "sys.path.insert(0," + repr(str(backend)) + ")\nimport worker\n"
                + "async def fixed(request):\n Path(" + repr(str(marker)) + ").write_text('QA')\n"
                + " return {'reply':'fixed','emotion':'neutral','status':'completed','tools_used':[]}\n"
                + "worker.run_agent=fixed\nasyncio.run(worker.main())\nos._exit(0)\n", encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-I", "-u", str(wrapper)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
            try:
                self.assertEqual(json.loads(process.stdout.readline())["type"], "ready")
                value = {"type": "run", "id": "qa-eof-is-not-commit", "kind": "tools", "task": "Fixed owned reader boundary canary", "context": "",
                    "config": {"base_url": "http://127.0.0.1:1/v1", "model": "qa-fixed-only", "api_key": "qa-synthetic-only"},
                    "options": {"workspace": str(owned), "max_steps": 1, "timeout_seconds": 5, "tools_enabled": True, "computer_enabled": False}}
                yield process, marker, json.dumps(value).encode("utf-8")
            finally:
                close_process(process)

    def test_eof_without_commit_lf_never_enters_actual_worker_main_run(self):
        with self.fixed_main() as (process, marker, raw):
            self.assertNotIn(b"\n", raw)
            process.stdin.write(raw); process.stdin.flush()
            time.sleep(.03); self.assertFalse(marker.exists())
            process.stdin.close()  # EOF must not stand in for the missing LF.
            process.wait(timeout=3)
            self.assertFalse(marker.exists())

    def test_standard_lf_is_a_positive_control_for_actual_worker_main(self):
        with self.fixed_main() as (process, marker, raw):
            process.stdin.write(raw + b"\n"); process.stdin.flush()
            frame = json.loads(process.stdout.readline())
            self.assertEqual(frame["type"], "final")
            self.assertEqual(frame["reply"], "fixed")
            self.assertEqual(marker.read_text(), "QA")
            self.assertEqual(process.wait(timeout=3), 0)


if __name__ == "__main__": unittest.main()
