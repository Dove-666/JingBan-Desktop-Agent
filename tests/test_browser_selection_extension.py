"""Exercise the actual packaged browser worker without touching real browsers."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BrowserSelectionExtensionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to exercise the browser service worker")
    def test_node_worker_and_dom_regressions(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--test", "--test-reporter=tap", str(ROOT / "tests/browser_selection_extension.test.mjs")],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# tests 13", result.stdout)

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is needed to inspect installer plans")
    def test_installer_plan_is_read_only_and_pins_current_user_registration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aura-browser-plan-") as directory:
            destination = Path(directory) / "not-created"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "tools/install_browser_ask.ps1"), "-InstallDirectory", str(destination)],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout.lstrip("\ufeff"))
            self.assertEqual(plan["mode"], "plan")
            self.assertEqual(plan["extension_id"], "gbeikpndpckhjeakbckijelidpdpinei")
            self.assertEqual(len(plan["registry"]), 2)
            self.assertTrue(all(entry["hive"] == "HKEY_CURRENT_USER" for entry in plan["registry"]))
            self.assertFalse(plan["browser_extension_installed"])
            self.assertFalse(destination.exists())

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is needed to inspect uninstaller plans")
    def test_uninstaller_missing_marker_does_not_create_or_remove_anything(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aura-browser-uninstall-plan-") as directory:
            destination = Path(directory) / "not-created"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "tools/uninstall_browser_ask.ps1"), "-InstallDirectory", str(destination)],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["mode"], "plan")
            self.assertFalse(destination.exists())

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is needed to prepare a temporary host")
    def test_prepare_only_and_uninstall_preserve_unrelated_files_without_registry_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aura-browser-prepare-") as directory:
            folder = Path(directory)
            project = folder / "project space % test"
            (project / "tools").mkdir(parents=True)
            (project / "tools/browser_ask_host.py").write_text("# temporary inert host\n", encoding="utf-8")
            (project / "browser-extension/whale-ask").mkdir(parents=True)
            (project / "browser-extension/whale-ask/manifest.json").write_text("{}", encoding="utf-8")
            destination = folder / "prepared host"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "tools/install_browser_ask.ps1"), "-PrepareOnly", "-InstallDirectory", str(destination),
                 "-ProjectDirectory", str(project), "-PythonPath", sys.executable,
                 "-ConfigPath", str(folder / "unused-settings.ini")],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["mode"], "prepare-only")
            manifest = json.loads((destination / "native-host.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["allowed_origins"], ["chrome-extension://gbeikpndpckhjeakbckijelidpdpinei/"])
            self.assertEqual(manifest["type"], "stdio")
            marker = json.loads((destination / "installation.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["registered_keys"], [])
            launcher = (destination / "host.cmd").read_text(encoding="utf-8")
            self.assertIn("project space %% test", launcher)
            self.assertIn("--config-path", launcher)
            self.assertIn("DisableDelayedExpansion", launcher)
            self.assertFalse((folder / "unused-settings.ini").exists())
            unrelated = destination / "keep-me.txt"
            unrelated.write_text("preserve unrelated material", encoding="utf-8")
            uninstalled = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "tools/uninstall_browser_ask.ps1"), "-Apply", "-InstallDirectory", str(destination)],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertTrue(unrelated.exists())
            self.assertEqual([item.name for item in destination.iterdir()], ["keep-me.txt"])

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is needed to prepare a temporary host")
    def test_packaged_host_prepares_without_python_or_source_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jingban-bridge-package-") as directory:
            folder = Path(directory)
            product = folder / "鲸伴 test % path"
            product.mkdir()
            (product / "鲸伴网页桥.exe").write_bytes(b"MZ synthetic path fixture, never executed")
            extension = product / "browser-extension/whale-ask"
            extension.mkdir(parents=True)
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            destination = folder / "prepared"
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "tools/install_browser_ask.ps1"), "-PrepareOnly", "-InstallDirectory", str(destination),
                "-ProjectDirectory", str(product), "-ConfigPath", str(folder / "isolated.ini")],
                cwd=ROOT, capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout.lstrip("\ufeff"))
            self.assertTrue(plan["packaged_host"])
            launcher = (destination / "host.cmd").read_text(encoding="utf-8")
            self.assertIn("鲸伴网页桥.exe", launcher)
            self.assertNotIn("python.exe", launcher)
            self.assertNotIn("browser_ask_host.py", launcher)
            self.assertIn("%% path", launcher)
            self.assertFalse((folder / "isolated.ini").exists())
            marker = json.loads((destination / "installation.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["registered_keys"], [])


if __name__ == "__main__":
    unittest.main()
