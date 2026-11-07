"""No Office or personal document is opened by these contract tests."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_tools import DocumentTools
from office_render import OfficeRenderError, _OwnedOffice, _stop_helper, inspect_package, render_document
from safe_files import WorkspaceFiles


def package(suffix=".docx", extra=None, body="<root/>"):
    stream = io.BytesIO()
    main = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[suffix]
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(main, body)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


class PackageSafetyTests(unittest.TestCase):
    def test_all_three_passive_packages_accepted(self):
        for suffix in (".docx", ".xlsx", ".pptx"):
            inspect_package(package(suffix), suffix)

    def test_missing_main_document_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_UNSAFE_PACKAGE"):
            inspect_package(package(".xlsx"), ".docx")

    def test_active_binary_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_ACTIVE_CONTENT_REJECTED"):
            inspect_package(package(extra={"word/vbaProject.bin": b"synthetic"}), ".docx")

    def test_web_extension_or_macro_content_type_rejected(self):
        for extra in ({"word/webextensions/webextension1.xml": "<root/>"}, {"docProps/macro.xml": '<Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(OfficeRenderError, "OFFICE_ACTIVE_CONTENT_REJECTED"):
                inspect_package(package(extra=extra), ".docx")

    def test_external_relationship_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_EXTERNAL_LINK_REJECTED"):
            inspect_package(package(extra={"word/_rels/document.xml.rels": '<Relationships><Relationship TargetMode="External" Target="https://example.invalid/test"/></Relationships>'}), ".docx")

    def test_plain_official_web_reference_is_inert_and_allowed(self):
        inspect_package(package(extra={"word/_rels/document.xml.rels": '<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" TargetMode="External" Target="https://learn.microsoft.com/en-us/office/vba/api/word.document.exportasfixedformat"/></Relationships>'}), ".docx")
        inspect_package(package(body='<root><fldSimple instr="HYPERLINK &quot;https://learn.microsoft.com/en-us/office/&quot;"/></root>'), ".docx")
        inspect_package(package(body='<root><fldSimple instr="HYPERLINK &quot;https://learn.microsoft.com/DDE/reference&quot;"/></root>'), ".docx")

    def test_standard_pptx_passive_printer_settings_are_not_macros(self):
        inspect_package(package(".pptx", extra={"ppt/printerSettings/printerSettings1.bin": b"passive printer settings"}), ".pptx")

    def test_external_image_and_credential_hyperlink_remain_rejected(self):
        for relation, target in (("image", "https://example.invalid/image.png"), ("hyperlink", "https://user:password@example.invalid/"), ("hyperlink", "file:///private")):
            with self.subTest(relation=relation, target=target), self.assertRaisesRegex(OfficeRenderError, "OFFICE_EXTERNAL_LINK_REJECTED"):
                inspect_package(package(extra={"word/_rels/document.xml.rels": f'<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relation}" TargetMode="External" Target="{target}"/></Relationships>'}), ".docx")

    def test_split_word_field_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_EXTERNAL_LINK_REJECTED"):
            inspect_package(package(body='<root><instrText> INCLU</instrText><instrText>DETEXT "file:///private"</instrText></root>'), ".docx")

    def test_simple_word_field_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_EXTERNAL_LINK_REJECTED"):
            inspect_package(package(body='<root><fldSimple instr="DDEAUTO dangerous"/></root>'), ".docx")

    def test_pure_excel_formula_accepted(self):
        inspect_package(package(".xlsx", body="<root><f>SUM(A1:A2)+ROUND(1.5,0)</f></root>"), ".xlsx")

    def test_excel_network_or_unicode_addin_rejected(self):
        for formula in ('WEBSERVICE("https://example.invalid")', "不安全函数(A1)", "REGISTER.ID(A1)"):
            with self.subTest(formula=formula), self.assertRaisesRegex(OfficeRenderError, "OFFICE_UNSUPPORTED_FORMULA"):
                inspect_package(package(".xlsx", body=f"<root><f>{formula}</f></root>"), ".xlsx")

    def test_excel_defined_name_cannot_hide_xlm_execution(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_UNSUPPORTED_FORMULA"):
            inspect_package(package(".xlsx", body='<root><definedName name="Auto_Open">EXEC("no")</definedName></root>'), ".xlsx")

    def test_xml_entity_rejected(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_UNSAFE_PACKAGE"):
            inspect_package(package(body='<!DOCTYPE root [<!ENTITY x "no">]><root>&x;</root>'), ".docx")


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.server = Path("C:/Program Files/Microsoft Office/WINWORD.EXE")
        self.old = {"pid": 20, "image": str(self.server), "created": 100}
        self.new = {"pid": 21, "image": str(self.server), "created": 200}

    def test_existing_instance_does_not_block_proven_new_instance(self):
        owner = _OwnedOffice._choose_owner({20: self.old}, {20: self.old, 21: self.new}, 21, self.server)
        self.assertEqual(owner, self.new)

    def test_reused_existing_com_server_refused(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_BUSY"):
            _OwnedOffice._choose_owner({20: self.old}, {20: self.old, 21: self.new}, 20, self.server)

    def test_unknown_window_with_existing_instance_refused(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_BUSY"):
            _OwnedOffice._choose_owner({20: self.old}, {20: self.old, 21: self.new}, None, self.server)

    def test_windowless_unique_new_server_without_existing_instance_allowed(self):
        self.assertEqual(_OwnedOffice._choose_owner({}, {21: self.new}, None, self.server), self.new)

    def test_multiple_windowless_new_servers_not_guessed(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_OWNERSHIP_UNCONFIRMED"):
            _OwnedOffice._choose_owner({}, {20: self.old, 21: self.new}, None, self.server)

    def test_unknown_app_pid_never_owns_an_unrelated_process(self):
        with self.assertRaisesRegex(OfficeRenderError, "OFFICE_BUSY"):
            _OwnedOffice._choose_owner({}, {21: self.new}, 22, self.server)

    def test_unbound_application_is_never_quit_or_reconfigured(self):
        office = _OwnedOffice("Word.Application", "WINWORD.EXE")
        app = office.app = MagicMock(); office.owner = self.new; office.saved = {"AutomationSecurity": 1}
        with patch("office_render._processes", return_value={21: self.new}):
            office.close()
        app.Quit.assert_not_called()
        self.assertNotIn("AutomationSecurity", app.__dict__)

    def test_stale_pid_never_quits_application_even_after_binding(self):
        office = _OwnedOffice("Word.Application", "WINWORD.EXE")
        app = office.app = MagicMock(); office.owner = self.new; office.bound = True
        with patch("office_render._processes", return_value={21: {**self.new, "created": 999}}):
            office.close()
        app.Quit.assert_not_called()

    def test_job_assignment_failure_terminates_only_our_createprocess_handle(self):
        office = _OwnedOffice("Word.Application", "WINWORD.EXE")
        handle = office.process = MagicMock(); office.assigned = False
        with patch("office_render._processes", return_value={}), patch("win32process.TerminateProcess") as terminate, \
                patch("win32event.WaitForSingleObject", return_value=0):
            office.close()
        terminate.assert_called_once_with(handle, 1)
        handle.Close.assert_called_once()


class RenderContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-office-contract-")
        self.root = Path(self.temp.name)
        self.files = WorkspaceFiles(self.root)
        self.files.write_bytes("input.docx", package())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_outside_destination_refused_before_helper_started(self):
        with patch("office_render.asyncio.create_subprocess_exec", new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
                await render_document(self.files, "input.docx", str(self.root.parent / "not-granted.pdf"))
            start.assert_not_awaited()

    async def test_existing_destination_requires_accepted_version_before_start(self):
        self.files.write_bytes("input.rendered.pdf", b"original")
        with patch("office_render.asyncio.create_subprocess_exec", new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(FileExistsError, "FILE_EXISTS"):
                await render_document(self.files, "input.docx")
            start.assert_not_awaited()
        self.assertEqual((self.root / "input.rendered.pdf").read_bytes(), b"original")

    async def test_timeout_terminates_only_the_owned_helper(self):
        class Helper:
            returncode = None
            killed = False
            async def communicate(self, message): return b"", None
            def kill(self): self.killed = True
            async def wait(self): self.returncode = -1
        helper = Helper()
        async def expired(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError
        with patch("office_render.asyncio.create_subprocess_exec", new=AsyncMock(return_value=helper)), patch("office_render.asyncio.wait_for", side_effect=expired):
            with self.assertRaisesRegex(OfficeRenderError, "OFFICE_TIMEOUT"):
                await render_document(self.files, "input.docx")
        self.assertTrue(helper.killed)
        self.assertFalse((self.root / "input.rendered.pdf").exists())

    async def test_cancellation_terminates_only_owned_helper_and_propagates(self):
        started = asyncio.Event()
        class Helper:
            returncode = None
            killed = False
            async def communicate(self, message):
                started.set(); await asyncio.Event().wait()
            def kill(self): self.killed = True
            async def wait(self): self.returncode = -1
        helper = Helper()
        with patch("office_render.asyncio.create_subprocess_exec", new=AsyncMock(return_value=helper)):
            task = asyncio.create_task(render_document(self.files, "input.docx"))
            await started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(helper.killed)
        self.assertFalse((self.root / "input.rendered.pdf").exists())

    async def test_helper_failure_code_is_bounded_and_publishes_nothing(self):
        class Helper:
            returncode = 1
            async def communicate(self, message): return b'{"ok":false,"code":"OFFICE_BUSY","cleanup_confirmed":true}', None
        with patch("office_render.asyncio.create_subprocess_exec", new=AsyncMock(return_value=Helper())):
            with self.assertRaisesRegex(OfficeRenderError, "OFFICE_BUSY"):
                await render_document(self.files, "input.docx")
        self.assertFalse((self.root / "input.rendered.pdf").exists())

    async def test_source_race_prevents_publish(self):
        from pypdf import PdfWriter
        output = io.BytesIO(); writer = PdfWriter(); writer.add_blank_page(50, 50); writer.write(output)
        root = self.root
        class Helper:
            returncode = 0
            async def communicate(self, message):
                directory = Path(json.loads(message)["directory"])
                (directory / "output.pdf").write_bytes(output.getvalue())
                (root / "input.docx").write_bytes(package(body="<changed/>"))
                return json.dumps({"ok": True, "cleanup_confirmed": True}).encode(), None
        with patch("office_render.asyncio.create_subprocess_exec", new=AsyncMock(return_value=Helper())):
            with self.assertRaisesRegex(OfficeRenderError, "OFFICE_SOURCE_CHANGED"):
                await render_document(self.files, "input.docx")
        self.assertFalse((self.root / "input.rendered.pdf").exists())

    async def test_successful_private_helper_output_uses_atomic_artifact_contract(self):
        from pypdf import PdfWriter
        output = io.BytesIO(); writer = PdfWriter(); writer.add_blank_page(50, 50); writer.write(output)
        class Helper:
            returncode = 0
            async def communicate(self, message):
                directory = Path(json.loads(message)["directory"])
                (directory / "output.pdf").write_bytes(output.getvalue())
                return json.dumps({"ok": True, "engine": "synthetic_contract_fixture", "native_text": "readback 42", "native_text_truncated": False, "cleanup_confirmed": True}).encode(), None
        with patch("office_render.asyncio.create_subprocess_exec", new=AsyncMock(return_value=Helper())):
            result = json.loads(await render_document(self.files, "input.docx"))
        self.assertEqual(result["status"], "rendered")
        self.assertEqual(result["pages"], 1)
        self.assertEqual((self.root / result["artifact"]["path"]).read_bytes(), output.getvalue())
        self.assertIn("semantic_and_visual_layout_require_separate_checks", result["scope"])

    async def test_invalid_typed_options_do_not_start_office(self):
        with patch("office_render.asyncio.create_subprocess_exec", new_callable=AsyncMock) as start:
            for options in ({"overwrite": "true"}, {"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": float("nan")}):
                with self.subTest(options=options), self.assertRaisesRegex(OfficeRenderError, "OFFICE_INVALID_ARGUMENT"):
                    await render_document(self.files, "input.docx", **options)
            start.assert_not_awaited()

    async def test_helper_exit_race_does_not_mask_cancel_or_timeout_cleanup(self):
        class Helper:
            returncode = None
            def kill(self): raise ProcessLookupError()
            async def wait(self): self.returncode = 1
        helper = Helper()
        await _stop_helper(helper)
        self.assertEqual(helper.returncode, 1)


class RealHelperProtocolTests(unittest.TestCase):
    def test_real_isolated_interpreter_rejects_non_typed_json_without_office(self):
        script = Path(__file__).resolve().parents[1] / "office_render.py"
        for message in (b"invalid json", b'{"directory":"C:/not-owned","suffix":".docx","code":"arbitrary"}'):
            with self.subTest(message=message):
                result = subprocess.run([sys.executable, "-I", "-u", str(script), "--office-helper"], input=message, capture_output=True, timeout=10, creationflags=0x08000000)
                body = json.loads(result.stdout)
                self.assertFalse(body["ok"])
                self.assertIn(body["code"], {"OFFICE_RENDER_FAILED", "OFFICE_INVALID_ARGUMENT"})
                self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
