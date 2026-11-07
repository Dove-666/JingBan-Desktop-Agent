"""Adapter CAS/cancellation tests; fake renders are not LibreOffice evidence."""
import asyncio
import copy
import hashlib
import io
import json
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from agent_backend.nanobot import libreoffice_render as adapter
from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents import libreoffice_renderer as native


def package(extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
        for key, value in (extra or {}).items(): archive.writestr(key, value)
    return stream.getvalue()


class FakeRenderer:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0
        self.issuer = object()

    def evidence(self, identity, digest): return self.issuer, identity, digest

    def validate_cleanup_evidence(self, evidence, *, invocation, source_sha256):
        return (type(evidence) is tuple and len(evidence) == 3 and evidence[0] is self.issuer
            and evidence[1] is invocation and evidence[2] == source_sha256)

    def render(self, raw, suffix, *, expected_source_sha256, cancel, timeout_seconds, execution_identity):
        self.calls += 1
        assert hashlib.sha256(raw).hexdigest() == expected_source_sha256
        result = SimpleNamespace(pdf=b"%PDF-1.7\nSYNTHETIC_UNIT_FIXTURE", text="AURA adapter synthetic fixture",
            pages=1, source_sha256=expected_source_sha256,
            pdf_sha256=hashlib.sha256(b"%PDF-1.7\nSYNTHETIC_UNIT_FIXTURE").hexdigest(),
            cleanup_evidence=self.evidence(execution_identity, expected_source_sha256))
        if self.callback: return self.callback(result, cancel)
        return result


class LibreOfficeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aura-lo-adapter-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.docx"
        self.source.write_bytes(package())
        self.files = WorkspaceFiles(self.root)
        self.engine = FakeRenderer()
        self.bound = adapter.LibreOfficeRenderAdapter(self.engine)

    def tearDown(self):
        with adapter._retained_lock:
            adapter._retained.discard(self.bound)

    async def invoke(self, **kwargs):
        return await adapter.render_document(self.files, "source.docx", renderer=self.bound, **kwargs)

    async def started(self, event):
        for _ in range(400):
            if event.is_set(): return
            await asyncio.sleep(.005)
        self.fail("Owned fake thread did not start")

    async def capture_cancel(self, facts):
        try: return await self.invoke()
        except asyncio.CancelledError as error:
            # Task itself replaces CancelledError while propagating outside
            # this coroutine; no external caller may trust attached fields.
            facts.update(safe=getattr(error, "safe_no_publication", False),
                effect_state=getattr(error, "effect_state", None))
            raise

    async def test_success_publishes_only_pdf_and_does_not_claim_microsoft_or_visual_verification(self):
        before = self.source.read_bytes()
        output = json.loads(await self.invoke())
        self.assertEqual({"source.docx", "source.rendered.pdf"}, {p.name for p in self.root.iterdir()})
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual("application/pdf", output["artifact"]["mime"])
        self.assertEqual(1, output["pages"])
        self.assertFalse(output["microsoft_office_rendered"])
        self.assertFalse(output["visual_layout_verified"])
        self.assertFalse(output["previews_published"])
        self.assertFalse(self.bound.host_status()["runtime_probe_performed"])

    async def test_source_changed_during_conversion_never_publishes(self):
        def changed(result, cancel): self.source.write_bytes(package({"custom.xml": "changed"})); return result
        self.engine.callback = changed
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_SOURCE_CHANGED") as caught: await self.invoke()
        self.assertTrue(caught.exception.safe_no_publication)
        self.assertFalse((self.root / "source.rendered.pdf").exists())

    async def test_existing_target_cas_conflict_does_not_overwrite_new_content(self):
        target = self.root / "output.pdf"; target.write_bytes(b"ORIGINAL")
        digest = hashlib.sha256(b"ORIGINAL").hexdigest()
        self.engine.callback = lambda result, _: (target.write_bytes(b"CHANGED"), result)[1]
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_SOURCE_CHANGED") as caught:
            await self.invoke(output_path="output.pdf", overwrite=True, expected_hash=digest)
        self.assertEqual(b"CHANGED", target.read_bytes())
        self.assertTrue(caught.exception.safe_no_publication)

    async def test_initially_absent_target_cannot_be_overwritten_even_if_observed_later(self):
        target = self.root / "output.pdf"
        def changed(result, _):
            target.write_bytes(b"OTHER_OWNER_OUTPUT")
            self.files.read_bytes("output.pdf")
            return result
        self.engine.callback = changed
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_SOURCE_CHANGED"):
            await self.invoke(output_path="output.pdf", overwrite=True)
        self.assertEqual(b"OTHER_OWNER_OUTPUT", target.read_bytes())

    async def test_invalid_scope_and_output_preflight_never_start_renderer(self):
        for kwargs in ({"output_path": "../outside.pdf"}, {"output_path": "source.docx"},
                       {"output_path": "preview.png"}, {"overwrite": 1}, {"timeout_seconds": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(adapter.OfficeRenderError) as caught:
                await self.invoke(**kwargs)
            self.assertTrue(caught.exception.safe_no_publication)
        self.assertEqual(0, self.engine.calls)

    async def test_granted_source_does_not_grant_an_external_output(self):
        with tempfile.TemporaryDirectory(prefix="aura-lo-external-grant-") as other:
            source = Path(other) / "granted.docx"; source.write_bytes(package())
            self.files.add_grant({"path": str(source), "access": "read", "directory": False})
            with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_INVALID_ARGUMENT"):
                await adapter.render_document(self.files, str(source), renderer=self.bound)
            self.assertFalse(source.with_suffix(".rendered.pdf").exists())
        self.assertEqual(0, self.engine.calls)

    async def test_macros_and_external_resources_are_rejected_before_renderer(self):
        for extra in ({"word/vbaProject.bin": b"FIXTURE"}, {"word/_rels/document.xml.rels":
            '<Relationships><Relationship TargetMode="External" Type="image" Target="https://example.invalid/image.png"/></Relationships>'}):
            self.source.write_bytes(package(extra))
            with self.assertRaises(adapter.OfficeRenderError) as caught: await self.invoke()
            self.assertTrue(caught.exception.safe_no_publication)
        self.assertEqual(0, self.engine.calls)

    async def test_host_injection_is_required_and_constructor_does_no_io(self):
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_RUNTIME_UNAVAILABLE"):
            await adapter.render_document(self.files, "source.docx")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No construction IO")):
            bound = adapter.LibreOfficeRenderAdapter(FakeRenderer())
            self.assertTrue(bound.host_status()["configured"])

    async def test_missing_or_forged_receipt_blocks_publication_and_next_run(self):
        for value in (None, True, {"cleanup_confirmed": True}, (object(), object(), "0" * 64)):
            with self.subTest(value=value):
                bound = adapter.LibreOfficeRenderAdapter(FakeRenderer(lambda result, _: (setattr(result, "cleanup_evidence", value), result)[1]))
                try:
                    with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED") as caught:
                        await adapter.render_document(self.files, "source.docx", renderer=bound)
                    self.assertFalse(caught.exception.safe_no_publication)
                    self.assertEqual("unknown", caught.exception.effect_state)
                    with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED"):
                        await adapter.render_document(self.files, "source.docx", renderer=bound)
                    self.assertEqual(1, bound._renderer.calls)
                finally:
                    with adapter._retained_lock: adapter._retained.discard(bound)
        self.assertFalse((self.root / "source.rendered.pdf").exists())

    async def test_previous_call_receipt_cannot_authorize_a_new_invocation(self):
        previous = []
        def replay(result, _):
            if previous: result.cleanup_evidence = previous[0]
            else: previous.append(result.cleanup_evidence)
            return result
        self.engine.callback = replay
        await self.invoke(output_path="first.pdf")
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED"):
            await self.invoke(output_path="second.pdf")
        self.assertTrue((self.root / "first.pdf").exists())
        self.assertFalse((self.root / "second.pdf").exists())

    async def test_error_strings_never_prove_cleanup_or_leak_private_details(self):
        def failed(*_): raise RuntimeError("SYNTHETIC_PRIVATE_KEY_AND_PATH_NOT_FOR_LOGS")
        self.engine.callback = failed
        reconciled = {"ready": False}
        self.bound._reconciler = lambda **scope: self.engine.evidence(scope["invocation"], scope["source_sha256"]) if reconciled["ready"] else None
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_RENDER_FAILED") as caught: await self.invoke()
        self.assertEqual("OFFICE_RENDER_FAILED", str(caught.exception))
        self.assertEqual("unknown", caught.exception.effect_state)
        self.assertIn(self.bound, adapter._retained)
        self.assertFalse(self.bound.reconcile_cleanup())
        self.bound._reconciler = lambda **scope: True
        self.assertFalse(self.bound.reconcile_cleanup())  # A host callback bool is still not evidence.
        self.bound._reconciler = lambda **scope: self.engine.evidence(scope["invocation"], scope["source_sha256"]) if reconciled["ready"] else None
        reconciled["ready"] = True
        self.assertTrue(self.bound.reconcile_cleanup())
        self.engine.callback = None
        await self.invoke()
        self.assertEqual(2, self.engine.calls)

    async def test_confirmed_renderer_error_has_safe_no_publication_and_can_continue(self):
        def failed(result, _):
            error = native.LibreOfficeError("LIBREOFFICE_TIMEOUT")
            error.cleanup_evidence = result.cleanup_evidence
            raise error
        self.engine.callback = failed
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_TIMEOUT") as caught: await self.invoke()
        self.assertTrue(caught.exception.safe_no_publication)
        self.assertFalse(self.bound.host_status()["cleanup_unknown"])
        self.engine.callback = None
        await self.invoke()

    async def test_cleanup_unknown_is_not_upgraded_by_a_contradictory_receipt(self):
        def failed(result, _):
            error = native.LibreOfficeError("LIBREOFFICE_CLEANUP_UNCONFIRMED")
            error.cleanup_evidence = result.cleanup_evidence
            raise error
        self.engine.callback = failed
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED") as caught:
            await self.invoke()
        self.assertEqual("unknown", caught.exception.effect_state)
        self.assertFalse(caught.exception.safe_no_publication)
        self.assertTrue(self.bound.host_status()["cleanup_unknown"])

    async def test_executor_release_failure_is_retained_with_fixed_error_until_host_reconcile(self):
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1)
        original = executor.shutdown
        failures = {"fail": True}
        def shutdown(*args, **kwargs):
            if failures["fail"]: raise OSError("SYNTHETIC_PRIVATE_THREAD_DETAIL")
            return original(*args, **kwargs)
        self.bound._reconciler = lambda **scope: True  # Retained genuine closure proof is used instead.
        try:
            with patch.object(adapter, "ThreadPoolExecutor", return_value=executor), patch.object(executor, "shutdown", side_effect=shutdown):
                with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED") as caught:
                    await self.invoke()
                self.assertEqual("unknown", caught.exception.effect_state)
                self.assertIs(executor, self.bound._executor)
                self.assertFalse(self.bound.reconcile_cleanup())
                failures["fail"] = False
                self.assertTrue(self.bound.reconcile_cleanup())
                self.assertIsNone(self.bound._executor)
        finally: original(wait=True)

    async def test_repeated_cancel_waits_for_actual_thread_then_allows_next_request(self):
        entered, stopped, release = threading.Event(), threading.Event(), threading.Event()
        def stop(result, event):
            entered.set()
            if not event.wait(4): raise AssertionError("Stop was not delivered")
            stopped.set()
            if not release.wait(4): raise AssertionError("Fixture was not released")
            error = native.LibreOfficeError("LIBREOFFICE_CANCELLED")
            error.cleanup_evidence = result.cleanup_evidence
            raise error
        self.engine.callback = stop
        facts = {}
        task = asyncio.create_task(self.capture_cancel(facts))
        try:
            await self.started(entered); task.cancel(); await self.started(stopped)
            task.cancel(); task.cancel(); await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertTrue(self.bound.host_status()["busy"])
            self.assertFalse(self.bound.reconcile_cleanup())
        finally: release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(facts["safe"])
        self.assertFalse(self.bound.host_status()["cleanup_unknown"])
        self.assertFalse((self.root / "source.rendered.pdf").exists())
        self.engine.callback = None
        await self.invoke()
        self.assertEqual(2, self.engine.calls)

    async def test_cancel_result_race_never_publishes_a_late_success(self):
        entered, release = threading.Event(), threading.Event()
        def result_after_stop(result, _): entered.set(); release.wait(4); return result
        self.engine.callback = result_after_stop
        facts = {}
        task = asyncio.create_task(self.capture_cancel(facts))
        await self.started(entered); task.cancel(); await asyncio.sleep(.02); release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(facts["safe"])
        self.assertFalse((self.root / "source.rendered.pdf").exists())

    async def test_cancel_without_exact_receipt_retains_unknown_and_never_replays(self):
        entered = threading.Event()
        def stop(_, event): entered.set(); event.wait(4); raise native.LibreOfficeError("LIBREOFFICE_CANCELLED")
        self.engine.callback = stop
        facts = {}
        task = asyncio.create_task(self.capture_cancel(facts)); await self.started(entered); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual("unknown", facts["effect_state"])
        self.assertFalse(facts["safe"])
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED"): await self.invoke()
        self.assertEqual(1, self.engine.calls)

    async def test_concurrent_call_is_busy_and_does_not_submit_second_renderer(self):
        entered, release = threading.Event(), threading.Event()
        def held(result, _): entered.set(); release.wait(4); return result
        self.engine.callback = held
        task = asyncio.create_task(self.invoke()); await self.started(entered)
        try:
            with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_BUSY"): await self.invoke()
            self.assertEqual(1, self.engine.calls)
        finally: release.set()
        await task

    async def test_invalid_pdf_is_never_published_even_after_confirmed_cleanup(self):
        self.engine.callback = lambda result, _: (setattr(result, "pdf", b"NOT_A_PDF"), result)[1]
        with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_PDF_INVALID") as caught: await self.invoke()
        self.assertTrue(caught.exception.safe_no_publication)
        self.assertFalse((self.root / "source.rendered.pdf").exists())

    async def test_write_then_raise_is_uncertain_and_never_called_safe_no_publication(self):
        original = self.files.write_bytes
        def uncertain(*args, **kwargs): original(*args, **kwargs); raise OSError("PRIVATE_DETAIL")
        with patch.object(self.files, "write_bytes", side_effect=uncertain):
            with self.assertRaisesRegex(adapter.OfficeRenderError, "OFFICE_RENDER_FAILED") as caught: await self.invoke()
        self.assertFalse(caught.exception.safe_no_publication)
        self.assertEqual("unknown", caught.exception.effect_state)
        self.assertTrue((self.root / "source.rendered.pdf").exists())
        self.assertEqual(1, self.engine.calls)


class NativeCleanupEvidenceTests(unittest.TestCase):
    def engine(self):
        return native.LibreOfficeRenderer(runtime="E:/fixed/runtime", python="E:/fixed/python.exe",
            pdftoppm="E:/fixed/pdf.exe", scratch_parent="E:/fixed/scratch")

    def test_prelaunch_receipt_is_opaque_and_bound_to_owner_invocation_and_snapshot(self):
        engine = self.engine(); identity = object(); raw = package(); digest = hashlib.sha256(raw).hexdigest()
        with self.assertRaises(native.LibreOfficeError) as caught:
            engine.render(raw, ".docx", expected_source_sha256=digest, execution_identity=identity)
        evidence = caught.exception.cleanup_evidence
        self.assertTrue(engine.validate_cleanup_evidence(evidence, invocation=identity, source_sha256=digest))
        for owner, token, checksum in ((self.engine(), identity, digest), (engine, object(), digest), (engine, identity, "0" * 64)):
            self.assertFalse(owner.validate_cleanup_evidence(evidence, invocation=token, source_sha256=checksum))
        for fake in (True, {}, object.__new__(native.CleanupEvidence)):
            self.assertFalse(engine.validate_cleanup_evidence(fake, invocation=identity, source_sha256=digest))
        for action in (lambda: native.CleanupEvidence(), lambda: setattr(evidence, "_owner", engine),
                       lambda: copy.copy(evidence), lambda: copy.deepcopy(evidence), lambda: pickle.dumps(evidence)):
            with self.assertRaises(TypeError): action()

    def test_entered_native_without_exact_cleanup_cannot_issue_receipt(self):
        engine = self.engine(); identity = object()
        def unknown(*args, **kwargs):
            kwargs["_execution"]["native_entered"] = True
            raise native.LibreOfficeError("LIBREOFFICE_CANCELLED")
        with patch.object(engine, "_render", side_effect=unknown):
            with self.assertRaises(native.LibreOfficeError) as caught:
                engine.render(b"synthetic", ".docx", expected_source_sha256="0" * 64, execution_identity=identity)
        self.assertFalse(hasattr(caught.exception, "cleanup_evidence"))

    def test_unknown_cleanup_gate_never_issues_prelaunch_receipt(self):
        engine = self.engine()
        with patch("desktop_pet.agents.guardian_native.has_pending_cleanup", return_value=True):
            with self.assertRaises(native.LibreOfficeError) as caught:
                engine.render(package(), ".docx", expected_source_sha256=hashlib.sha256(package()).hexdigest(), execution_identity=object())
        self.assertFalse(hasattr(caught.exception, "cleanup_evidence"))

    def test_isolated_worker_import_uses_the_same_office_error_class(self):
        here = Path(__file__).resolve().parents[1] / "agent_backend/nanobot"
        code = "import sys; sys.path.insert(0,sys.argv[1]); import libreoffice_render,office_render; assert libreoffice_render.OfficeRenderError is office_render.OfficeRenderError; print('ISOLATED_IMPORT_OK')"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(here)], capture_output=True,
            timeout=10, creationflags=0x08000000)
        self.assertEqual(0, result.returncode)
        self.assertEqual(b"ISOLATED_IMPORT_OK", result.stdout.strip())

    def test_empty_global_registry_cannot_mint_deferred_proof(self):
        engine = self.engine()
        with patch("desktop_pet.agents.guardian_native.reconcile_cleanup", return_value=True):
            self.assertIsNone(engine.reconcile_cleanup_evidence(invocation=object(), source_sha256="0" * 64))

    def test_deferred_proof_requires_that_exact_retained_run_to_really_cleanup(self):
        engine = self.engine(); identity = object(); digest = "a" * 64
        with tempfile.TemporaryDirectory(prefix="aura-lo-deferred-proof-") as parent:
            directory = Path(parent) / "libreoffice-owned-proof"; directory.mkdir()
            state = {"closed": False}
            api = SimpleNamespace(signaled=lambda _: True, confirm_closed=lambda *args, **kwargs: state["closed"],
                k=SimpleNamespace(CloseHandle=lambda _: True))
            run = native._OwnedRun(api, SimpleNamespace(handle=33, terminate=lambda: None),
                SimpleNamespace(handle=11, thread=22), directory, evidence_owner=engine,
                execution_identity=identity, source_sha256=digest)
            native._pending.append(run)
            try:
                with patch("desktop_pet.agents.guardian_native.reconcile_cleanup", return_value=True):
                    self.assertIsNone(engine.reconcile_cleanup_evidence(invocation=identity, source_sha256=digest))
                    self.assertIn(run, native._pending)
                    state["closed"] = True
                    self.assertIsNone(engine.reconcile_cleanup_evidence(invocation=object(), source_sha256=digest))
                    self.assertNotIn(run, native._pending)
                    self.assertIsNone(engine.reconcile_cleanup_evidence(invocation=identity, source_sha256="b" * 64))
                    evidence = engine.reconcile_cleanup_evidence(invocation=identity, source_sha256=digest)
                    self.assertTrue(engine.validate_cleanup_evidence(evidence, invocation=identity, source_sha256=digest))
                    self.assertIsNone(engine.reconcile_cleanup_evidence(invocation=identity, source_sha256=digest))
            finally:
                if run in native._pending: native._pending.remove(run)


if __name__ == "__main__": unittest.main()
