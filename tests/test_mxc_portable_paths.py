"""Current-user path resolution must not loosen the disabled MXC boundary."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from agent_backend.nanobot import mxc_execution as mxc
from tools import qa_mxc_probe as probe


class PortableMxcPathTests(unittest.TestCase):
    def test_managed_python_uses_current_absolute_roaming_root_without_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            roaming = Path(temporary) / "a different roaming profile"
            with patch.dict(os.environ, {"APPDATA": str(roaming)}), \
                    patch.object(Path, "home", side_effect=AssertionError("No home lookup when APPDATA is supplied")), \
                    patch.object(Path, "exists", side_effect=AssertionError("No filesystem search")), \
                    patch.object(mxc.shutil, "which", side_effect=AssertionError("No PATH interpreter discovery")):
                result = mxc._python_runtime_root()
            self.assertEqual(result.parent.parent, roaming / "uv")
            self.assertEqual(result.name, mxc.PYTHON_ROOT.name)

    def test_missing_empty_and_relative_roaming_roots_use_current_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "current-user"
            for value in (None, "", "relative-roaming-root"):
                with self.subTest(value=value), patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                    if value is not None:
                        os.environ["APPDATA"] = value
                    result = mxc._python_runtime_root()
                    self.assertEqual(result.parent.parent, home / "AppData" / "Roaming" / "uv")

    def test_probe_uses_current_local_root_without_path_or_directory_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "different-local-profile"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}), \
                    patch.object(Path, "home", side_effect=AssertionError("No fallback needed")), \
                    patch.object(Path, "glob", side_effect=AssertionError("No version-directory search")), \
                    patch.object(probe.shutil, "which", side_effect=AssertionError("No PATH CLI discovery")):
                result = probe._codex_probe_executable()
            self.assertEqual(result.parents[4], local)
            self.assertEqual(result.name, "codex.exe")

    def test_missing_empty_and_relative_local_roots_use_current_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "current-user"
            for value in (None, "", "relative-local-root"):
                with self.subTest(value=value), patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                    if value is not None:
                        os.environ["LOCALAPPDATA"] = value
                    self.assertEqual(probe._codex_probe_executable().parents[4], home / "AppData" / "Local")

    def test_resolving_runtime_paths_cannot_enable_execution_or_skip_review(self):
        for live in (False, True):
            with self.subTest(live=live):
                runner = mxc.MxcPythonRunner(live_enabled=live)
                with patch.object(runner, "_pins", side_effect=AssertionError("Capability check must not touch runtimes")):
                    capability = runner.capability()
                self.assertFalse(capability["available"])
                self.assertFalse(capability["runtime_verified"])
                self.assertEqual(capability["code"], "MXC_REQUIRES_HOST_REVIEW")
        runner = mxc.MxcPythonRunner()
        with patch.object(runner, "_pins", side_effect=AssertionError("Disabled call cannot probe runtimes")), \
                patch.object(runner, "_native_run", side_effect=AssertionError("Disabled call cannot launch code")):
            result = runner.execute("raise AssertionError('must never execute')", scope=None)
        self.assertEqual(result.code, "MXC_DISABLED")

    def test_resolved_runtime_stays_protected_from_user_file_grants(self):
        from agent_backend.nanobot.project_runtime import project_scope_conflicts
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"APPDATA": str(root / "roaming")}):
                # Set the constant from the same explicit current-user resolution.
                runtime = mxc._python_runtime_root()
                with patch.object(mxc, "PYTHON_ROOT", runtime):
                    boundary = {"host_private_root": root / "private", "host_configuration": root / "private/runtime.json"}
                    self.assertTrue(project_scope_conflicts([runtime], **boundary))
                    self.assertTrue(project_scope_conflicts([runtime / "Lib"], **boundary))
                    self.assertFalse(project_scope_conflicts([root / "ordinary-workspace"], **boundary))


if __name__ == "__main__":
    unittest.main()
