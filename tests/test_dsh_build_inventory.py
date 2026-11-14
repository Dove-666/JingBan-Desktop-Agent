"""Release inventory follows the exact DSH staging selection, not old sources."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.packaging import worker_runtime
from tools.qa_release import source_inventory


class DshInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dsh-source-inventory-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def put(self, name, content="owned build input"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_staging_and_inventory_share_yaml_mjs_and_retired_module_selection(self):
        expected = {"desktop_pet/agents/service.py", "desktop_pet/agents/dsh_storage.py",
                    "agent_backend/deepseek_harness/worker.mjs", "agent_backend/deepseek_harness/profile.yaml",
                    "agent_backend/deepseek_harness/profile.yml", "agent_backend/deepseek_harness/package-lock.json"}
        excluded = {"desktop_pet/agents/checkpoints.py", "desktop_pet/agents/execution_runtime.py",
                    "desktop_pet/agents/office_runtime.py", "desktop_pet/agents/research_delegation.py",
                    "desktop_pet/agents/libreoffice_renderer.py", "desktop_pet/agents/libreoffice_helper.py",
                    "agent_backend/nanobot/worker.py", "agent_backend/ui_tars/bridge.mjs",
                    "agent_backend/deepseek_harness/test/local.test.mjs",
                    "agent_backend/deepseek_harness/node_modules/pkg/index.js"}
        for name in expected | excluded: self.put(name)
        selected = set(worker_runtime.iter_staged_worker_sources(project_root=self.root))
        self.assertEqual({path.relative_to(self.root).as_posix() for path in selected}, expected)
        output = self.root / "staged"
        with patch.object(worker_runtime, "PROJECT_ROOT", self.root):
            worker_runtime._copy_source(self.root / "agent_backend", output / "agent_backend", worker_runtime.BACKEND_SOURCE_SUFFIXES)
            worker_runtime._copy_source(self.root / "agent_backend/deepseek_harness", output / "agent_backend/deepseek_harness", worker_runtime.DSH_SOURCE_SUFFIXES)
            worker_runtime._copy_source(self.root / "desktop_pet", output / "desktop_pet", {".py"})
        self.assertEqual({path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}, expected)

    def test_source_only_registry_and_cua_manifests_are_fenced_without_native_build_caches(self):
        include = {"agent_backend/deepseek_harness/.npmrc", "agent_backend/deepseek_harness/package.json",
                   "third_party/node/runtime.json", "tools/packaging/node_runtime.py",
                   "agent_backend/deepseek_harness/package-lock.json",
                   "third_party/deepseek_harness/cua-windows/package.json",
                   "third_party/deepseek_harness/cua-windows/UPSTREAM.json",
                   "third_party/deepseek_harness/cua-windows/build-native.ps1",
                   "third_party/deepseek_harness/cua-windows/native/entry/CuaWindows.csproj",
                   "third_party/deepseek_harness/cua-windows/native/cua-windows.slnx",
                   "third_party/deepseek_harness/cua-windows/native/entry/app.manifest",
                   "third_party/deepseek_harness/cua-windows/native/src/Entry.cs",
                   "agent_backend/deepseek_harness/node_modules/@deepseek-ai/dsh/package.json"}
        exclude = {"third_party/deepseek_harness/cua-windows/native/obj/cache.json",
                   "third_party/deepseek_harness/cua-windows/native/bin/build.dll",
                   "agent_backend/nanobot/requirements.txt"}
        for name in include | exclude: self.put(name)
        with patch.object(source_inventory, "ROOT", self.root):
            inventory = source_inventory.inputs()
        self.assertTrue(include <= set(inventory))
        self.assertFalse(exclude & set(inventory))
        self.assertTrue(all(len(value) == 64 for value in inventory.values()))


if __name__ == "__main__": unittest.main()
