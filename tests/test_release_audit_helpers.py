"""Exercise manifest boundary handling and redacted audit reporting before use."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.qa_release.audit_helpers import credential_findings, manifest_member, private_member, regular_files, validate_manifest_rows


class ManifestBoundaryTest(unittest.TestCase):
    def test_manifest_rejects_escape_and_noncanonical_paths(self):
        for value in ("../settings.ini", "/etc/passwd", "C:/vault", "foo\\bar", "a//b", "a/./b", "a/../b", "x\n.txt", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manifest_member(value)
        self.assertEqual(manifest_member("_internal/角色/whale.png").as_posix(), "_internal/角色/whale.png")

    def test_windows_case_collisions_are_rejected(self):
        row = {"bytes": 1, "sha256": "a" * 64}
        with self.assertRaises(ValueError):
            validate_manifest_rows([{**row, "path": "A.txt"}, {**row, "path": "a.txt"}])

    def test_invalid_size_or_checksum_is_rejected(self):
        for values in ({"bytes": True, "sha256": "a" * 64}, {"bytes": -1, "sha256": "a" * 64},
                       {"bytes": 1, "sha256": "wrong"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_manifest_rows([{**values, "path": "whale.png"}])

    def test_credential_findings_never_include_match(self):
        secret = "sk-" + "A7b9" * 10
        text = "API_KEY = '" + secret + "'"
        rows = credential_findings(text, "module.py")
        self.assertEqual(len(rows), 1)
        self.assertNotIn(secret, json.dumps(rows))
        self.assertEqual(set(rows[0]), {"path", "line", "kind"})
        self.assertEqual(credential_findings("api_key = os.environ.get('API_KEY')", "safe.py"), [])

    def test_private_state_and_signing_material_are_blocked(self):
        for name in ("_internal/settings.ini", "data/character_cards.sqlite3", "keys/release.pfx", ".env.local", "config/credentials.json"):
            with self.subTest(name=name):
                self.assertTrue(private_member(Path(name)))
        self.assertFalse(private_member(Path("_internal/runtime/manifest.json")))
        self.assertFalse(private_member(Path("licenses/THIRD-PARTY-NOTICES.txt")))

    def test_inventory_reads_only_owned_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "inner").mkdir()
            (base / "inner/file.txt").write_text("example", encoding="utf-8")
            self.assertEqual(regular_files(base), [base / "inner/file.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
