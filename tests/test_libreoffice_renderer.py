"""Prototype admission/cleanup failures; no actual Office or user documents."""
from contextlib import ExitStack, contextmanager
import ctypes as C
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from desktop_pet.agents import libreoffice_renderer as renderer


def package(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
        for name, value in (extra or {}).items(): archive.writestr(name, value)
    return output.getvalue()


class LibreOfficeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.raw = package()
        self.engine = renderer.LibreOfficeRenderer(runtime="E:/owned/runtime", python="E:/owned/python.exe",
            pdftoppm="E:/owned/pdftoppm.exe", scratch_parent="E:/owned/private", enabled=False)

    def invoke(self, raw=None, suffix=".docx", **options):
        raw = self.raw if raw is None else raw
        return self.engine.render(raw, suffix, expected_source_sha256=hashlib.sha256(raw).hexdigest(), **options)

    def test_construction_does_not_create_directory_process_or_read_config(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("No construction IO")), \
             patch.object(Path, "read_text", side_effect=AssertionError("No construction config")), \
             patch("subprocess.Popen", side_effect=AssertionError("No process")):
            renderer.LibreOfficeRenderer(runtime="E:/fixed/runtime", python="E:/fixed/python.exe",
                pdftoppm="E:/fixed/pdf.exe", scratch_parent="E:/fixed/scratch")

    def test_default_disabled_refuses_valid_package_without_native_process(self):
        with patch("desktop_pet.agents.guardian_native.Native", side_effect=AssertionError("No process")):
            with self.assertRaisesRegex(renderer.LibreOfficeError, "LIBREOFFICE_PROTOTYPE_DISABLED"): self.invoke()

    def test_invalid_hash_format_and_nonfinite_deadline_are_rejected_before_launch(self):
        with self.assertRaisesRegex(renderer.LibreOfficeError, "INPUT_INVALID"):
            self.engine.render(self.raw, ".docx", expected_source_sha256="0" * 64)
        for suffix in (".doc", ".xlsm", ".ppt", ".pdf", ".DOCX"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(renderer.LibreOfficeError, "INPUT_INVALID"):
                self.invoke(suffix=suffix)
        for timeout in (True, float("nan"), float("inf"), 0, 121):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(renderer.LibreOfficeError, "INPUT_INVALID"):
                self.invoke(timeout_seconds=timeout)

    def test_macro_and_external_resource_rejection_is_not_weakened_by_another_renderer(self):
        self.engine.enabled = True
        cases = [({"word/vbaProject.bin": b"SYNTHETIC"}, "OFFICE_ACTIVE_CONTENT_REJECTED"),
            ({"word/_rels/document.xml.rels": '<Relationships><Relationship TargetMode="External" Target="https://example.invalid/image.png" Type="image"/></Relationships>'}, "OFFICE_EXTERNAL_LINK_REJECTED")]
        with patch.object(self.engine, "_validate_runtime", side_effect=AssertionError("Input must fail first")):
            for additions, code in cases:
                with self.subTest(code=code), self.assertRaisesRegex(renderer.LibreOfficeError, code): self.invoke(package(additions))

    def test_pre_cancel_and_unresolved_launch_or_cleanup_cannot_start_a_new_process(self):
        self.engine.enabled = True
        import threading
        cancelled = threading.Event(); cancelled.set()
        with patch.object(self.engine, "_validate_runtime"), \
             patch("desktop_pet.agents.guardian_native.Native", side_effect=AssertionError("No process")):
            with self.assertRaisesRegex(renderer.LibreOfficeError, "CANCELLED"): self.invoke(cancel=cancelled)
        with patch("desktop_pet.agents.guardian_native.has_pending_cleanup", return_value=True), \
             patch.object(self.engine, "_validate_runtime", side_effect=AssertionError("Unknown cannot be bypassed")):
            with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"): self.invoke()

    def test_runtime_entrypoint_hash_change_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="aura-render-runtime-test-") as owned:
            root = Path(owned); (root / "runtime/program").mkdir(parents=True)
            entries = []
            for name in ("soffice.com", "soffice.bin", "soffice.exe"):
                (root / "runtime/program" / name).write_bytes(b"owned")
                entries.append({"file": "program/" + name, "sha256": hashlib.sha256(b"owned").hexdigest()})
            import json
            (root / "runtime-manifest.json").write_text(json.dumps({"schema": "aura.libreoffice.extracted_runtime.v1",
                "version": "26.8.2", "package_sha256": renderer.PACKAGE_SHA256, "signature": {"status": "Valid"}, "files": entries}))
            self.engine.runtime = root / "runtime"; self.engine.enabled = True
            (root / "runtime/program/soffice.bin").write_bytes(b"changed")
            with self.assertRaisesRegex(renderer.LibreOfficeError, "RUNTIME_CHANGED"): self.engine._validate_runtime()

    def test_private_dependency_error_is_not_exposed_as_a_raw_path_or_exception(self):
        self.engine.enabled = True
        with patch.object(self.engine, "_validate_runtime", side_effect=OSError("SYNTHETIC_PRIVATE_PATH_DO_NOT_LOG")):
            with self.assertRaisesRegex(renderer.LibreOfficeError, "LIBREOFFICE_RENDER_FAILED") as caught: self.invoke()
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_host_result_reads_have_a_hard_memory_bound(self):
        with tempfile.TemporaryDirectory(prefix="aura-render-read-limit-") as owned:
            path = Path(owned) / "controlled-output"; path.write_bytes(b"X" * 4097)
            with self.assertRaisesRegex(renderer.LibreOfficeError, "RESULT_LIMIT"):
                renderer._read_bounded(path, 4096)


class LibreOfficeCleanupTests(unittest.TestCase):
    def make_run(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-render-cleanup-test-")
        self.addCleanup(owned.cleanup)
        directory = Path(owned.name) / "libreoffice-owned-fixture"; directory.mkdir()
        state = {"confirmed": True, "close_failure": False, "query_calls": 0, "closed": []}
        def confirmed(*args, **kwargs): state["query_calls"] += 1; return state["confirmed"]
        def close(handle):
            if state["close_failure"]: return False
            state["closed"].append(handle); return True
        native = SimpleNamespace(signaled=lambda _: True, confirm_closed=confirmed,
            k=SimpleNamespace(CloseHandle=close))
        job = SimpleNamespace(handle=33, terminate=lambda: None)
        process = SimpleNamespace(handle=11, thread=22)
        return renderer._OwnedRun(native, job, process, directory, extra_handles=[44]), state

    def test_unconfirmed_exact_tree_keeps_every_handle_and_directory_until_explicit_recovery(self):
        run, state = self.make_run(); state["confirmed"] = False
        self.assertFalse(renderer._cleanup(run)); self.assertFalse(run.effects_closed)
        self.assertEqual([], state["closed"]); self.assertTrue(run.directory.is_dir())
        state["confirmed"] = True
        self.assertTrue(renderer._cleanup(run)); self.assertTrue(run.effects_closed)
        self.assertEqual([22, 11, 33, 44], state["closed"]); self.assertFalse(run.directory.exists())

    def test_failed_handle_close_does_not_get_erased_or_turn_into_a_later_fake_confirmation(self):
        run, state = self.make_run(); state["close_failure"] = True
        self.assertFalse(renderer._cleanup(run)); self.assertTrue(run.effects_closed)
        self.assertEqual(22, run.process.thread); self.assertEqual(11, run.process.handle)
        self.assertEqual(1, state["query_calls"]); self.assertTrue(run.directory.exists())
        state["close_failure"] = False
        self.assertTrue(renderer._cleanup(run))
        self.assertEqual(1, state["query_calls"])  # Retained genuine proof, not zero-handle inference.

    def test_racing_second_stop_failure_still_requires_both_exact_closure_observations(self):
        run, state = self.make_run()
        run.native.signaled = lambda _: False
        run.native.k.TerminateProcess = lambda *_: 0
        state["confirmed"] = False
        self.assertFalse(renderer._cleanup(run)); self.assertFalse(run.effects_closed)
        self.assertEqual([], state["closed"])
        state["confirmed"] = True
        self.assertTrue(renderer._cleanup(run)); self.assertTrue(run.effects_closed)

    def test_windows_readonly_known_folder_is_removed_only_inside_owned_scratch(self):
        run, _ = self.make_run()
        documents = run.directory / "Documents"; documents.mkdir(); os.chmod(documents, stat.S_IREAD)
        self.assertTrue(renderer._cleanup(run)); self.assertFalse(run.directory.exists())

    def test_nonowned_recursive_delete_is_rejected_and_directory_remains(self):
        with tempfile.TemporaryDirectory(prefix="aura-not-render-owned-") as owned:
            with self.assertRaisesRegex(renderer.LibreOfficeError, "SCOPE_INVALID"):
                renderer._remove_owned_directory(Path(owned))
            self.assertTrue(Path(owned).is_dir())

    def test_reparse_descendant_is_rejected_before_any_recursive_delete(self):
        run, _ = self.make_run()
        (run.directory / "untrusted-link").mkdir()
        real_lstat = os.lstat
        def attributes(path, *args, **kwargs):
            if Path(path).name == "untrusted-link": return SimpleNamespace(st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with patch("desktop_pet.agents.libreoffice_renderer.os.lstat", side_effect=attributes):
            with self.assertRaisesRegex(renderer.LibreOfficeError, "SCOPE_INVALID"):
                renderer._remove_owned_directory(run.directory)
        self.assertTrue(run.directory.exists())


class LibreOfficeBeforeLaunchCleanupTests(unittest.TestCase):
    @contextmanager
    def failed_launch(self, *, job_close_failure=False, directory_failure=False):
        from desktop_pet.agents.guardian_native import ExtendedLimits, GuardianError
        with tempfile.TemporaryDirectory(prefix="aura-render-prelaunch-test-") as owned:
            state = {"memory": 0, "job_close_failure": job_close_failure, "closed": []}
            def create_directory(path, *_): Path(path).mkdir(); return 1
            def query(_job, _kind, pointer, *_):
                value = C.cast(pointer, C.POINTER(ExtendedLimits)).contents
                value.basic.flags = 0x2008 | (0x200 if state["memory"] else 0)
                value.basic.active_limit = 8; value.job_memory = state["memory"]
                return 1
            def limits(_job, _kind, pointer, *_):
                state["memory"] = C.cast(pointer, C.POINTER(ExtendedLimits)).contents.job_memory
                return 1
            def close(handle):
                if handle == 50 and state["job_close_failure"]: return False
                state["closed"].append(handle); return True
            def check(value, code):
                if not value: raise GuardianError(code)
                return value
            native = SimpleNamespace(descriptor=lambda: 123, owner_only=lambda _: True, check=check,
                file_info=lambda _: SimpleNamespace(attributes=0x10),
                k=SimpleNamespace(CreateDirectoryW=create_directory, LocalFree=lambda _: None,
                    CreateFileW=lambda path, *_: 40 if path == "NUL" else 30, CloseHandle=close,
                    QueryInformationJobObject=query, SetInformationJobObject=limits))
            engine = renderer.LibreOfficeRenderer(runtime="E:/fixed/runtime", python="E:/fixed/python.exe",
                pdftoppm="E:/fixed/pdf.exe", scratch_parent=owned, enabled=True)
            with ExitStack() as stack:
                stack.enter_context(patch.object(engine, "_validate_runtime"))
                stack.enter_context(patch("desktop_pet.agents.guardian_native.Native", return_value=native))
                stack.enter_context(patch("desktop_pet.agents.guardian_native.OwnedJob", return_value=SimpleNamespace(handle=50)))
                stack.enter_context(patch("desktop_pet.agents.guardian_native.OwnedProcess.create", side_effect=GuardianError("GUARDIAN_CREATE_FAILED")))
                stack.enter_context(patch("desktop_pet.agents.guardian_native.has_pending_cleanup", return_value=False))
                if directory_failure:
                    stack.enter_context(patch.object(renderer, "_remove_owned_directory", side_effect=PermissionError("Synthetic cleanup denial")))
                yield engine, native, state

    def invoke(self, engine):
        raw = package()
        return engine.render(raw, ".docx", expected_source_sha256=hashlib.sha256(raw).hexdigest())

    def test_empty_job_close_failure_is_retained_and_blocks_new_render_until_checked_release(self):
        try:
            with self.failed_launch(job_close_failure=True) as (engine, native, state):
                with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"): self.invoke(engine)
                self.assertIn((native, 50), renderer._unbound_handles)
                with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"): self.invoke(engine)
                state["job_close_failure"] = False
                self.assertTrue(renderer.reconcile_cleanup())
                self.assertIn(50, state["closed"])
        finally: renderer._unbound_handles.clear()

    def test_unlaunched_directory_cleanup_error_is_fixed_and_retained_not_raw_oserror(self):
        try:
            with self.failed_launch(directory_failure=True) as (engine, _native, _state):
                with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"): self.invoke(engine)
                self.assertEqual(1, len(renderer._unbound_directories))
                retained = renderer._unbound_directories[0]
                self.assertTrue(retained.is_dir())
            # TemporaryDirectory removed this owned fake-run root. Discarding
            # a missing directory is file cleanup, never worker closure proof.
            self.assertTrue(renderer.reconcile_cleanup())
        finally: renderer._unbound_directories.clear()

    def test_scope_rejection_before_launch_retains_directory_and_reconcile_never_throws(self):
        try:
            with self.failed_launch() as (engine, _native, _state), patch.object(
                renderer, "_remove_owned_directory",
                side_effect=renderer.LibreOfficeError("LIBREOFFICE_CLEANUP_SCOPE_INVALID"),
            ):
                with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"):
                    self.invoke(engine)
                self.assertEqual(1, len(renderer._unbound_directories))
                self.assertFalse(renderer.reconcile_cleanup())
                with self.assertRaisesRegex(renderer.LibreOfficeError, "CLEANUP_UNCONFIRMED"):
                    self.invoke(engine)
            self.assertTrue(renderer.reconcile_cleanup())
        finally: renderer._unbound_directories.clear()


if __name__ == "__main__": unittest.main()
