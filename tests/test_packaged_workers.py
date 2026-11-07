"""Relocation, profile isolation and unchanged execution gates for the EXE."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet import bundle_runtime
from desktop_pet.agents import execution_runtime
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.agents.contracts import AgentValidationError
from agent_backend.nanobot import runtime_paths


class PackagedWorkerTests(unittest.TestCase):
    def test_frozen_worker_paths_follow_the_extracted_bundle(self):
        with tempfile.TemporaryDirectory(prefix="鲸伴 relocated ") as temporary, \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", temporary, create=True):
            root = Path(temporary).resolve()
            self.assertEqual(bundle_runtime.worker_python_executable(), root / "runtime/python/python.exe")
            self.assertEqual(bundle_runtime.bundled_node_executable(), root / "runtime/node/node.exe")
            self.assertNotEqual(bundle_runtime.worker_python_executable(), Path(sys.executable))

    def test_profiles_select_distinct_writable_workspaces_after_module_import(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "frozen", True, create=True):
            first, second = Path(temporary) / "profile one", Path(temporary) / "profile two"
            for profile in (first, second):
                with patch.dict(os.environ, {"JINGBAN_PROFILE_DIR": str(profile)}):
                    settings = AgentSettings()
                    self.assertEqual(Path(settings.workspace), profile / "agent_workspace")
                    settings.validate()
            with patch.dict(os.environ, {"JINGBAN_PROFILE_DIR": str(second)}):
                with self.assertRaises(AgentValidationError):
                    AgentSettings(workspace=str(first / "agent_workspace")).validate()

    def test_frozen_workspace_does_not_fall_back_into_the_installation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "frozen", True, create=True), \
                patch.dict(os.environ, {"LOCALAPPDATA": temporary, "JINGBAN_PROFILE_DIR": ""}):
            self.assertEqual(bundle_runtime.frozen_workspace_root(), Path(temporary) / "AURA Desktop Pet/agent_workspace")

    def test_backend_node_uses_shipped_runtime_without_host_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = root / "runtime/node/node.exe"
            node.parent.mkdir(parents=True)
            node.write_bytes(b"MZ synthetic path fixture")
            module = root / "agent_backend/nanobot/runtime_paths.py"
            with patch.object(runtime_paths, "__file__", str(module)), \
                    patch.object(runtime_paths.shutil, "which", side_effect=AssertionError("Host Node must not be consulted")):
                self.assertEqual(runtime_paths.node_executable(), str(node))

    def test_packaged_project_execution_gate_does_not_read_host_configuration(self):
        for frozen, environment in ((True, ""), (False, "1")):
            with self.subTest(frozen=frozen), patch.object(sys, "frozen", frozen, create=True), \
                    patch.dict(os.environ, {"JINGBAN_PACKAGED": environment}), \
                    patch.object(Path, "lstat", side_effect=AssertionError("No developer opt-in read")):
                self.assertIsNone(execution_runtime.load_execution_runtime())
                self.assertIsNone(execution_runtime.capture_execution_runtime().configuration)

    def test_memory_worker_import_is_pure_python_when_qt_is_unavailable(self):
        root = str(Path(__file__).resolve().parent.parent)
        script = (
            "import sys,importlib.abc,json;sys.path.insert(0," + repr(root) + ");"
            "exec('class NoQt(importlib.abc.MetaPathFinder):\\n def find_spec(self,fullname,path=None,target=None):\\n  if fullname.startswith(\"PySide6\"): raise AssertionError(\"Qt import in worker\")');"
            "sys.meta_path.insert(0,NoQt());import desktop_pet.memory.worker;"
            "print(json.dumps({'qt_loaded':'PySide6' in sys.modules}))"
        )
        result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["qt_loaded"])
