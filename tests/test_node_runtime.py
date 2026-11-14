"""Owned Node selection and reproducible binary supply without system changes."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from desktop_pet.node_runtime import node_supported, node_version, supported_node_version
from desktop_pet.agents.service import node_executable
from tools.packaging import node_runtime as supply


class NodeSelectionTests(unittest.TestCase):
    def test_fixed_libuv_release_ranges(self):
        for value in ((24, 20, 0), (24, 22, 3), (26, 7, 0), (27, 0, 0)):
            self.assertTrue(supported_node_version(value), value)
        for value in (None, (24, 15, 0), (24, 19, 9), (25, 99, 0), (26, 6, 9)):
            self.assertFalse(supported_node_version(value), value)

    def test_frozen_owned_node_never_falls_back_to_path(self):
        with patch("desktop_pet.agents.service.bundled_node_executable", return_value=Path("missing/node.exe")), \
                patch("desktop_pet.agents.service.node_supported", return_value=False), \
                patch("desktop_pet.agents.service.shutil.which") as fallback:
            self.assertIsNone(node_executable())
            fallback.assert_not_called()

    def test_source_owned_runtime_precedes_system_and_invalid_owned_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = root / "runtime/node/node.exe"
            node.parent.mkdir(parents=True)
            node.write_bytes(b"owned executable fixture")
            with patch("desktop_pet.agents.service.bundled_node_executable", return_value=None), \
                    patch("desktop_pet.agents.service._ROOT", root), \
                    patch("desktop_pet.agents.service.shutil.which") as fallback, \
                    patch("desktop_pet.agents.service.node_supported", return_value=True):
                self.assertEqual(node_executable(), node)
                fallback.assert_not_called()
                with patch("desktop_pet.agents.service.node_supported", return_value=False):
                    self.assertIsNone(node_executable())
                    fallback.assert_not_called()

    def test_old_path_node_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("desktop_pet.agents.service.bundled_node_executable", return_value=None), \
                patch("desktop_pet.agents.service._ROOT", Path(directory)), \
                patch("desktop_pet.agents.service.shutil.which", return_value="system/node.exe"), \
                patch("desktop_pet.agents.service.node_supported", return_value=False):
            self.assertIsNone(node_executable())

    def test_supported_path_node_is_available_when_no_owned_runtime(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("desktop_pet.agents.service.bundled_node_executable", return_value=None), \
                patch("desktop_pet.agents.service._ROOT", Path(directory)), \
                patch("desktop_pet.agents.service.shutil.which", return_value="system/node.exe"), \
                patch("desktop_pet.agents.service.node_supported", return_value=True):
            self.assertEqual(node_executable(), Path("system/node.exe"))

    def test_unversioned_file_is_rejected_without_execution_on_windows(self):
        if sys.platform != "win32": self.skipTest("Windows PE metadata")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node.exe"
            path.write_bytes(b"not an executable")
            with patch("desktop_pet.node_runtime.subprocess.run") as execute:
                self.assertIsNone(node_version(path))
                self.assertFalse(node_supported(path))
                execute.assert_not_called()

    def test_diagnostic_explicit_old_node_is_blocked(self):
        from desktop_pet.agents.health import DoctorRequest, collect_agent_health
        with patch("desktop_pet.node_runtime.node_supported", return_value=False):
            report = collect_agent_health(DoctorRequest(node_path=Path(sys.executable)))
        self.assertEqual(report.overall, "blocked")
        self.assertFalse(any(item["available"] for item in report.capabilities))


class NodeSupplyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "node.zip"
        self.pin = {"version": "24.22.3", "platform": "win-x64", "archive_url": "https://example.invalid/node.zip"}
        self.node, self.license = b"synthetic PE bytes", b"synthetic license"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("node-v24.22.3-win-x64/node.exe", self.node)
            bundle.writestr("node-v24.22.3-win-x64/LICENSE", self.license)
            bundle.writestr("../../must-not-extract", b"irrelevant")
        self.pin.update(archive_sha256=supply.sha256(self.archive),
                        node_sha256=hashlib.sha256(self.node).hexdigest(),
                        license_sha256=hashlib.sha256(self.license).hexdigest())
        self.target = self.root / "private/node"

    def test_verified_archive_provisions_only_exact_members_and_provenance(self):
        with patch.object(supply, "pinned_runtime", return_value=self.pin):
            supply.provision_node(archive=self.archive, target=self.target)
            self.assertTrue(supply.validate_runtime(self.target))
            self.assertEqual({path.name for path in self.target.iterdir()}, {"node.exe", "LICENSE", "provenance.json"})
            self.assertFalse((self.root / "must-not-extract").exists())

    def test_archive_tampering_is_rejected_before_installing_members(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"tamper")
        with patch.object(supply, "pinned_runtime", return_value=self.pin), self.assertRaisesRegex(ValueError, "archive checksum"):
            supply.provision_node(archive=self.archive, target=self.target)
        self.assertFalse((self.target / "node.exe").exists())

    def test_member_hash_mismatch_is_rejected_even_for_valid_archive_hash(self):
        self.pin["node_sha256"] = "0" * 64
        with patch.object(supply, "pinned_runtime", return_value=self.pin), self.assertRaisesRegex(ValueError, "member checksum"):
            supply.provision_node(archive=self.archive, target=self.target)
        self.assertFalse((self.target / "node.exe").exists())

    def test_valid_cache_does_not_download_and_tampered_license_invalidates_it(self):
        with patch.object(supply, "pinned_runtime", return_value=self.pin):
            supply.provision_node(archive=self.archive, target=self.target)
            with patch.object(supply.urllib.request, "urlopen") as network:
                self.assertEqual(supply.provision_node(target=self.target), self.target)
                network.assert_not_called()
            (self.target / "LICENSE").write_bytes(b"tampered")
            self.assertFalse(supply.validate_runtime(self.target))

    def test_staging_copies_exact_runtime_and_provenance(self):
        with patch.object(supply, "pinned_runtime", return_value=self.pin):
            supply.provision_node(archive=self.archive, target=self.target)
            destination = self.root / "payload/runtime/node"
            with patch.object(supply, "provision_node", return_value=self.target):
                self.assertEqual(supply.stage_node(destination), self.pin)
            self.assertTrue(supply.validate_runtime(destination))


if __name__ == "__main__": unittest.main()
