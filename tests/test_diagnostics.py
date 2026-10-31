"""Read-only diagnostic contracts; no live configuration, keys or providers."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from desktop_pet import diagnostics


def write_png(path, *, broken_payload=False, opaque=False):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    color, row = (2, b"\x00\x20\x40\x60\x80\xa0\xc0") if opaque else (6, b"\x00\x20\x40\x60\x00\x80\xa0\xc0\xff")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, color, 0, 0, 0))
                     + chunk(b"IDAT", b"broken-image" if broken_payload else zlib.compress(row)) + chunk(b"IEND", b""))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-diag-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "test.png"
        write_png(self.image)

    @staticmethod
    def probe(_path):
        return {"qt_version": "6.12.0", "readable": True, "format": "PNG", "width": 2, "height": 1, "alpha_channel": True}

    def report(self, **kwargs):
        return diagnostics.collect_diagnostics(image_path=self.image,
            module_finder=kwargs.pop("module_finder", lambda _name: object()),
            version_getter=lambda _name: "1.2.3", qt_probe=kwargs.pop("qt_probe", self.probe), **kwargs)

    def test_missing_required_dependencies_are_reported_without_qt_import(self):
        def finder(name):
            return None if name in ("PySide6", "openai") else object()
        with patch("desktop_pet.diagnostics._probe_qt", side_effect=AssertionError("Qt must not be imported")):
            report = diagnostics.collect_diagnostics(image_path=self.image, module_finder=finder,
                                                      version_getter=lambda _name: "1.0")
        self.assertFalse(report["required_ok"])
        self.assertIn("PySide6", " ".join(report["errors"]))
        self.assertIn("openai", " ".join(report["errors"]))
        self.assertEqual(report["character_image"]["validation"], "header_only")
        self.assertIsNone(report["character_image"]["readable"])

    def test_missing_optional_memory_does_not_mark_pet_unusable(self):
        report = self.report(module_finder=lambda name: object() if name in ("PySide6", "openai", "keyring") else None)
        self.assertTrue(report["required_ok"])
        self.assertEqual(report["errors"], [])
        self.assertTrue(any("桌宠和普通聊天仍可使用" in item for item in report["warnings"]))
        self.assertFalse(any(report["privacy"].values()))

    def test_missing_and_invalid_images_are_required_issues(self):
        for kind in ("missing", "invalid"):
            with self.subTest(kind=kind):
                self.image.unlink(missing_ok=True)
                if kind == "invalid":
                    self.image.write_bytes(b"not-a-png")
                report = self.report()
                self.assertFalse(report["required_ok"])
                self.assertIn(report["character_image"]["validation"], ("missing", "invalid_png"))

    def test_qt_decode_failure_is_not_reported_as_healthy_png(self):
        broken = lambda _path: {**self.probe(_path), "readable": False}
        report = self.report(qt_probe=broken)
        self.assertFalse(report["required_ok"])
        self.assertEqual(report["character_image"]["validation"], "decode_failed")

    def test_qt_dll_failure_never_echoes_exception_paths_or_secrets(self):
        sensitive = "C:/Users/synthetic-private-user/secret-provider-token"
        def fail(_path):
            raise ImportError(sensitive)
        report = self.report(qt_probe=fail)
        text = json.dumps(report, ensure_ascii=False)
        self.assertFalse(report["required_ok"])
        self.assertNotIn(sensitive, text)
        self.assertNotIn("synthetic-private-user", text)

    def test_report_omits_paths_environment_and_unread_private_files(self):
        secret = "synthetic-private-api-key"
        sensitive_path = self.root / "private-username"
        sensitive_path.mkdir()
        image = sensitive_path / "private-desktop.png"
        write_png(image)
        (sensitive_path / "settings.ini").write_text("api_key=" + secret + "\nbase_url=https://private-provider.invalid", encoding="utf-8")
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret, "OPENAI_BASE_URL": "https://private-provider.invalid"}):
            report = diagnostics.collect_diagnostics(image_path=image, module_finder=lambda _name: object(),
                                                      version_getter=lambda _name: "1.0", qt_probe=self.probe)
        text = json.dumps(report, ensure_ascii=False) + diagnostics.format_summary(report)
        for forbidden in (secret, "private-username", "private-provider.invalid", str(self.root), str(image)):
            self.assertNotIn(forbidden, text)

    def test_no_application_or_model_packages_are_imported_for_metadata_checks(self):
        looked_up = []
        def finder(name):
            looked_up.append(name)
            return object()
        self.report(module_finder=finder)
        self.assertEqual(looked_up, [module for _, module in diagnostics.REQUIRED_PACKAGES + diagnostics.OPTIONAL_PACKAGES])
        self.assertTrue(all("." not in name for name in looked_up))

    def test_diagnostic_creates_no_runtime_directories_or_files(self):
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.report()
        finally:
            os.chdir(previous)
        after = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.assertEqual(after, before)

    def test_cli_exit_code_and_json_are_based_only_on_required_issues(self):
        healthy = self.report(module_finder=lambda name: object() if name in ("PySide6", "openai", "keyring") else None)
        failed = {**healthy, "required_ok": False}
        for report, expected in ((healthy, 0), (failed, 1)):
            with self.subTest(expected=expected), patch.object(diagnostics, "collect_diagnostics", return_value=report), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(diagnostics.main(["--json"]), expected)
                self.assertEqual(json.loads(output.getvalue())["required_ok"], report["required_ok"])

    def test_real_qt_png_decode_is_read_only_and_detects_corrupt_payload(self):
        # This environment has the ordinary Qt dependency. No GUI or loop is
        # created: only QImageReader touches these synthetic fixture bytes.
        if diagnostics.importlib.util.find_spec("PySide6") is None:
            self.assertFalse(self.report(module_finder=lambda _name: None)["required_ok"])
            return
        before = self.image.read_bytes()
        probe = diagnostics._probe_qt(self.image)
        self.assertTrue(probe["readable"])
        self.assertEqual(probe["format"], "PNG")
        self.assertEqual((probe["width"], probe["height"]), (2, 1))
        self.assertTrue(probe["alpha_channel"])
        self.assertEqual(self.image.read_bytes(), before)
        write_png(self.image, broken_payload=True)
        self.assertFalse(diagnostics._probe_qt(self.image)["readable"])

    def test_actual_stdlib_only_cli_reports_missing_qt_instead_of_crashing(self):
        # -S hides every site-package, including Qt and the model SDKs. The
        # module must remain useful without importing any GUI application.
        result = subprocess.run([sys.executable, "-S", "-X", "utf8", "-m", "desktop_pet.diagnostics", "--json"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
        report = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["required_ok"])
        self.assertFalse(any(item["available"] for item in report["dependencies"]["required"]))
        self.assertEqual(report["character_image"]["validation"], "header_only")
        self.assertNotIn(str(Path.home()), result.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
