"""Host configuration/private-scope tests. No native renderer is launched."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_backend.nanobot import host_office_runtime as host
from agent_backend.nanobot.libreoffice_render import LibreOfficeRenderAdapter
from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents import office_runtime as loader
from tests.test_libreoffice_render_adapter import FakeRenderer, package


class HostOfficeConfigurationTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-host-office-config-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.config = {"engine": "libreoffice", "runtime": str(self.root / "vendor/runtime"),
            "scratch_parent": str(self.root / "private/scratch"), "pdftoppm": str(self.root / "poppler/bin/pdftoppm.exe")}

    def test_strict_shape_and_absolute_paths_exclude_model_python_profile_and_secrets(self):
        self.assertEqual("libreoffice", host.checked_office_renderer(self.config)["engine"])
        for value in (None, {}, {**self.config, "python": "arbitrary.exe"}, {**self.config, "profile": "shared"},
                      {**self.config, "engine": "com"}, {**self.config, "api_key": "SYNTHETIC_PRIVATE_KEY"},
                      {**self.config, "runtime": "relative/runtime"}, {**self.config, "runtime": True},
                      {**self.config, "runtime": "\\\\server\\share\\runtime"}):
            with self.subTest(value=value), self.assertRaises(host.HostOfficeConfigurationError) as caught:
                host.checked_office_renderer(value)
            self.assertEqual("HOST_OFFICE_CONFIGURATION_INVALID", str(caught.exception))

    def test_scratch_cannot_contain_or_be_inside_native_runtime_or_trusted_code(self):
        for scratch in (self.root, self.root / "vendor", self.root / "vendor/runtime/tasks", host.HERE):
            with self.subTest(scratch=scratch), self.assertRaises(host.HostOfficeConfigurationError):
                host.checked_office_renderer({**self.config, "scratch_parent": str(scratch)})

    def test_read_and_write_capability_overlaps_are_symmetric(self):
        config = host.checked_office_renderer(self.config)
        workspace = self.root / "workspace"
        host.check_office_scopes(config, workspace)
        for path in (self.root, self.root / "private/scratch/task", self.root / "vendor/runtime/program",
                     self.root / "vendor/runtime-manifest.json", self.root / "poppler/bin", host.HERE):
            for access in ("read", "write"):
                with self.subTest(path=path, access=access), self.assertRaises(host.HostOfficeConfigurationError):
                    host.check_office_scopes(config, workspace,
                        [{"path": str(path), "access": access, "directory": path.suffix == ""}])

    def test_fixed_loader_missing_does_not_opt_in_and_valid_file_has_no_extra_schema(self):
        path = self.root / "agent_backend/nanobot/runtime/office_renderer.json"
        path.parent.mkdir(parents=True)
        with patch.object(loader, "_ROOT", self.root), patch.object(loader, "_CONFIG", path):
            self.assertIsNone(loader.load_office_runtime())
            path.write_text(json.dumps(self.config), encoding="utf-8")
            self.assertEqual(host.checked_office_renderer(self.config), loader.load_office_runtime())

    def test_loader_rejects_duplicate_key_malformed_oversize_and_private_fields_without_leak(self):
        path = self.root / "agent_backend/nanobot/runtime/office_renderer.json"
        path.parent.mkdir(parents=True)
        candidates = ["null", "{}", "{broken", '{"engine":"com","engine":"libreoffice"}',
            json.dumps({**self.config, "secret": "SYNTHETIC_NEVER_LOG"}), "X" * 16385]
        with patch.object(loader, "_ROOT", self.root), patch.object(loader, "_CONFIG", path):
            for content in candidates:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content[:32]), self.assertRaises(loader.OfficeRuntimeConfigurationError) as caught:
                    loader.load_office_runtime()
                self.assertNotIn("SYNTHETIC", str(caught.exception))

    def test_loader_rejects_hardlinked_configuration(self):
        path = self.root / "agent_backend/nanobot/runtime/office_renderer.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.config), encoding="utf-8")
        os.link(path, self.root / "second-name.json")
        with patch.object(loader, "_ROOT", self.root), patch.object(loader, "_CONFIG", path):
            with self.assertRaises(loader.OfficeRuntimeConfigurationError): loader.load_office_runtime()


class HostOfficeScopeFenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-host-office-scopes-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.config = {"engine": "libreoffice", "runtime": str(self.root / "vendor/runtime"),
            "scratch_parent": str(self.root / "private/scratch"), "pdftoppm": str(self.root / "poppler/bin/pdftoppm.exe")}
        for name in ("vendor/runtime", "private/scratch", "poppler/bin"): (self.root / name).mkdir(parents=True)
        (self.workspace / "input.docx").write_bytes(package())
        self.files = WorkspaceFiles(self.workspace)
        self.runtime = host.HostOfficeRuntime(self.config, self.files)
        self.fake = FakeRenderer()

    async def test_conflicting_grant_preserves_ordinary_file_tools_but_render_does_not_launch(self):
        self.files.add_grant({"path": str(self.root), "directory": True, "access": "read"})
        self.assertIsNone(self.runtime.binding)
        with patch("desktop_pet.agents.libreoffice_renderer.LibreOfficeRenderer",
                   side_effect=AssertionError("Render isolation must fail before native binding")):
            with self.assertRaisesRegex(host.OfficeRenderError, "OFFICE_ISOLATION_UNAVAILABLE") as caught:
                await self.runtime.render_document("input.docx", "output.pdf")
        self.assertTrue(caught.exception.safe_no_publication)
        self.files.write("ordinary.txt", "ordinary file tools still work")
        self.assertEqual("ordinary file tools still work", self.files.read("ordinary.txt"))
        self.assertFalse((self.workspace / "output.pdf").exists())

    async def test_late_grant_before_publication_is_rechecked_and_never_publishes(self):
        def late(result, _):
            self.files.add_grant({"path": self.config["scratch_parent"], "directory": True, "access": "read"})
            return result
        self.fake.callback = late
        self.runtime.binding = LibreOfficeRenderAdapter(self.fake)
        with self.assertRaisesRegex(host.OfficeRenderError, "OFFICE_ISOLATION_UNAVAILABLE") as caught:
            await self.runtime.render_document("input.docx", "output.pdf")
        self.assertEqual(1, self.fake.calls)
        self.assertTrue(caught.exception.safe_no_publication)
        self.assertFalse((self.workspace / "output.pdf").exists())

    async def test_binding_is_reused_and_native_paths_are_host_only(self):
        self.fake.reconcile_cleanup_evidence = lambda **scope: None
        with patch("desktop_pet.agents.libreoffice_renderer.LibreOfficeRenderer", return_value=self.fake) as factory:
            await self.runtime.render_document("input.docx", "one.pdf")
            await self.runtime.render_document("input.docx", "two.pdf")
        self.assertEqual(1, factory.call_count)
        self.assertEqual(2, self.fake.calls)
        self.assertEqual(host.sys.executable, factory.call_args.kwargs["python"])
        self.assertEqual(self.config["runtime"], factory.call_args.kwargs["runtime"])


if __name__ == "__main__": unittest.main()
