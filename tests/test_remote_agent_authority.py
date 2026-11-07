"""Durable pairing incarnation identity; no model, network or real settings."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.remote.errors import RemoteError
from desktop_pet.remote.settings import RemotePreferences


class RemoteAgentAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-pair-authority-")
        self.addCleanup(self.directory.cleanup)
        self.settings = SimpleNamespace(config_path=Path(self.directory.name) / "settings.ini", _credentials=None)
        self.preferences = RemotePreferences(self.settings)
        self.peer = "a" * 64

    def test_unknown_peer_cannot_obtain_grant_and_construction_does_not_write(self):
        self.assertIsNone(self.preferences.agent_authorization(self.peer, create=True))
        self.assertFalse(self.preferences.path.exists())

    def test_grant_survives_reload_metadata_changes_and_other_device_additions(self):
        self.preferences.add_device(self.peer, "test phone", "2026")
        grant = self.preferences.agent_authorization(self.peer)
        self.assertRegex(grant, r"^[0-9a-f]{64}$")
        self.preferences.add_device("b" * 64, "second phone", "2027")
        self.preferences.add_device(self.peer, "renamed phone", "2028")
        reloaded = RemotePreferences(self.settings)
        self.assertEqual(reloaded.agent_authorization(self.peer), grant)
        self.assertNotEqual(reloaded.agent_authorization("b" * 64), grant)
        self.assertEqual(set(reloaded.devices[0]), {"id", "name", "paired_at"})
        self.assertNotIn("agent_grant", json.dumps(reloaded.devices))
        self.assertNotIn(grant, reloaded.path.read_text(encoding="utf-8"))

    def test_revoke_then_repair_with_same_peer_and_time_cannot_revive_old_authority(self):
        self.preferences.add_device(self.peer, "phone", "same timestamp")
        grant = self.preferences.agent_authorization(self.peer)
        self.preferences.revoke(self.peer)
        self.assertIsNone(self.preferences.agent_authorization(self.peer, create=True))
        self.preferences.add_device(self.peer, "phone", "same timestamp")
        self.assertNotEqual(self.preferences.agent_authorization(self.peer), grant)

    def test_legacy_allowlist_is_read_only_until_explicit_admission(self):
        self.preferences.directory.mkdir()
        self.preferences.path.write_text(json.dumps({"version": 1, "enabled": True, "devices": [
            {"id": self.peer, "name": "legacy", "paired_at": "2026"}]}), encoding="utf-8")
        before = self.preferences.path.read_bytes()
        legacy = RemotePreferences(self.settings)
        self.assertTrue(legacy.authorized(self.peer))
        self.assertIsNone(legacy.agent_authorization(self.peer))
        self.assertEqual(legacy.path.read_bytes(), before)
        grant = legacy.agent_authorization(self.peer, create=True)
        self.assertEqual(RemotePreferences(self.settings).agent_authorization(self.peer), grant)

    def test_failed_grant_save_cannot_leave_a_usable_in_memory_authority(self):
        self.preferences.add_device(self.peer, "phone", "2026")
        raw = json.loads(self.preferences.path.read_text(encoding="utf-8"))
        raw["devices"][0].pop("agent_grant")
        self.preferences.path.write_text(json.dumps(raw), encoding="utf-8")
        legacy = RemotePreferences(self.settings)
        with patch("desktop_pet.remote.settings.os.replace", side_effect=OSError("private path")):
            with self.assertRaises(RemoteError):
                legacy.agent_authorization(self.peer, create=True)
        self.assertIsNone(legacy.agent_authorization(self.peer))


if __name__ == "__main__":
    unittest.main()
