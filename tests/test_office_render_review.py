"""Independent no-process regressions over the actual render cleanup boundary.

No COM, renderer, desktop or model is called. Publication/cleanup boundaries
are exercised without replacing the wrapper's execution-state implementation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_backend.nanobot import office_render


class OwnedFiles:
    def __init__(self, root, publication_error=False):
        self.root, self.publication_error = root, publication_error
        self.source = root / "owned.docx"
        self.source.write_bytes(b"owned synthetic source")
        self.publication_attempted = False

    def read_bytes(self, path):
        raw = self.source.read_bytes()
        return self.source, raw, hashlib.sha256(raw).hexdigest()

    def resolve(self, value, **_):
        return Path(value)

    def _version_for_write(self, *args):
        pass

    def write_bytes(self, *args):
        self.publication_attempted = True
        raise office_render.OfficeRenderError("OFFICE_PDF_INVALID")


class OwnedHelper:
    def __init__(self, timeout):
        self.returncode = None
        self.timeout = timeout

    async def communicate(self, raw):
        directory = Path(json.loads(raw)["directory"])
        (directory / "owned.json").write_text(json.dumps({"pid": 4321, "image": "C:/Synthetic/WINWORD.EXE", "created": 7}))
        if self.timeout:
            raise asyncio.TimeoutError
        (directory / "output.pdf").write_bytes(b"synthetic PDF bytes; parser is controlled")
        self.returncode = 0
        return json.dumps({"ok": True, "engine": "Word.Application", "native_text": "Owned text",
                           "native_text_truncated": False, "cleanup_confirmed": True}).encode(), b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class OfficeRenderIndependentReviewTests(unittest.IsolatedAsyncioTestCase):
    async def execute_failure(self, *, timeout):
        with tempfile.TemporaryDirectory(prefix="aura-office-review-") as owned:
            files = OwnedFiles(Path(owned), publication_error=not timeout)
            helper = OwnedHelper(timeout)
            alive = {4321: {"pid": 4321, "image": "C:/Synthetic/WINWORD.EXE", "created": 7}}
            fake_pdf = SimpleNamespace(PdfReader=lambda *args, **kwargs: SimpleNamespace(
                pages=[SimpleNamespace(extract_text=lambda: "Owned text")]))
            with patch.object(office_render, "inspect_package", return_value=None), \
                    patch.object(office_render.importlib.util, "find_spec", return_value=object()), \
                    patch.object(office_render.asyncio, "create_subprocess_exec", AsyncMock(return_value=helper)), \
                    patch.object(office_render.asyncio, "sleep", AsyncMock()), \
                    patch.object(office_render, "_owned_startup_dialog", return_value=False), \
                    patch.object(office_render, "_processes", return_value=alive), \
                    patch.dict("sys.modules", {"pypdf": fake_pdf}):
                with self.assertRaises(office_render.OfficeRenderError) as caught:
                    await office_render.render_document(files, str(files.source), timeout_seconds=5)
            return caught.exception, helper.returncode, bool(alive), files.publication_attempted

    async def test_timeout_must_not_prove_all_private_processes_settled_from_helper_exit_alone(self):
        error, helper_returncode, office_still_observed, publication = await self.execute_failure(timeout=True)
        self.assertIsNotNone(helper_returncode)
        self.assertTrue(office_still_observed)
        self.assertFalse(publication)
        # Helper exit alone provides no Office process-handle confirmation.
        self.assertFalse(getattr(error, "safe_no_publication", False))

    async def test_exact_process_wait_timeout_or_failure_closes_every_handle_and_com(self):
        for response in (258, 0xFFFFFFFF, RuntimeError("synthetic wait failure")):
            with self.subTest(response=type(response).__name__ if isinstance(response, Exception) else response):
                owner = office_render._OwnedOffice("Word.Application", "WINWORD.EXE")
                handle, job = MagicMock(), MagicMock()
                owner.process, owner.job, owner.assigned, owner.com_initialized = handle, job, True, True
                wait = MagicMock(side_effect=response if isinstance(response, Exception) else None,
                                 return_value=response if not isinstance(response, Exception) else None)
                uninitialize = MagicMock()
                fake_event = SimpleNamespace(WaitForSingleObject=wait, WAIT_OBJECT_0=0)
                with patch.object(office_render, "_processes", return_value={}), \
                        patch.dict("sys.modules", {"win32event": fake_event,
                                                  "pythoncom": SimpleNamespace(CoUninitialize=uninitialize)}):
                    with self.assertRaisesRegex(office_render.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED"):
                        owner.close()
                    wait.assert_called_once_with(handle, 5000)
                    job.Close.assert_called_once()
                    handle.Close.assert_called_once()
                    uninitialize.assert_called_once()
                    self.assertIsNone(owner.process)
                    self.assertIsNone(owner.job)
                    self.assertFalse(owner.com_initialized)
                    with self.assertRaisesRegex(office_render.OfficeRenderError, "OFFICE_CLEANUP_UNCONFIRMED"):
                        owner.close()  # Closing empty handles cannot erase the failed proof.

    async def test_signalled_exact_handle_confirms_cleanup_without_pid_guessing(self):
        owner = office_render._OwnedOffice("Word.Application", "WINWORD.EXE")
        handle, job = MagicMock(), MagicMock()
        owner.process, owner.job, owner.assigned = handle, job, True
        with patch.object(office_render, "_processes", return_value={}), \
                patch.dict("sys.modules", {"win32event": SimpleNamespace(WaitForSingleObject=MagicMock(return_value=0), WAIT_OBJECT_0=0)}):
            self.assertTrue(owner.close())
        self.assertTrue(owner.cleanup_confirmed)
        handle.Close.assert_called_once()

    async def test_helper_failure_without_typed_cleanup_confirmation_cannot_be_optional(self):
        for proof in (None, False, "true"):
            with self.subTest(proof=proof), tempfile.TemporaryDirectory(prefix="aura-cleanup-proof-") as owned:
                files = OwnedFiles(Path(owned))
                helper = OwnedHelper(False)
                async def failed(_):
                    helper.returncode = 1
                    result = {"ok": False, "code": "OFFICE_BUSY"}
                    if proof is not None:
                        result["cleanup_confirmed"] = proof
                    return json.dumps(result).encode(), b""
                helper.communicate = failed
                with patch.object(office_render, "inspect_package", return_value=None), \
                        patch.object(office_render.importlib.util, "find_spec", return_value=object()), \
                        patch.object(office_render.asyncio, "create_subprocess_exec", AsyncMock(return_value=helper)):
                    with self.assertRaises(office_render.OfficeRenderError) as caught:
                        await office_render.render_document(files, str(files.source), timeout_seconds=5)
                self.assertFalse(getattr(caught.exception, "safe_no_publication", False))
                self.assertFalse(files.publication_attempted)

    async def test_error_after_publication_begins_never_gets_optional_failure_flag(self):
        error, helper_returncode, _, publication = await self.execute_failure(timeout=False)
        self.assertEqual(helper_returncode, 0)
        self.assertTrue(publication)
        self.assertFalse(getattr(error, "safe_no_publication", False))


if __name__ == "__main__":
    unittest.main()
