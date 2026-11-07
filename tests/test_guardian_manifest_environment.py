"""No-launch manifest/environment production-boundary regression cases."""
from __future__ import annotations

import unittest
from uuid import uuid4

from desktop_pet.agents.guardian_native import GuardianError, environment_block
from desktop_pet.agents.job_guardian import new_manifest, validate_manifest, canonical, decode


class ManifestEnvironmentTests(unittest.TestCase):
    def manifest(self, **extra):
        return new_manifest(job_id=str(uuid4()), conversation_id=str(uuid4()), generation=str(uuid4()),
            operation_id=str(uuid4()), installation_id="a" * 64, launch_epoch="b" * 64,
            process_creation_time=1234, **extra)

    def test_v2_binds_full_remote_reference_and_keeps_v1_explicitly_distinct(self):
        old = self.manifest()
        current = self.manifest(user_message_id=str(uuid4()), settings_scope="c" * 64, origin_peer_id="d" * 64)
        self.assertEqual(old["schema"], "aura.host_job_manifest.v1")
        self.assertEqual(current["schema"], "aura.host_job_manifest.v2")
        self.assertEqual(decode(canonical(current)), current)
        for key in ("user_message_id", "settings_scope", "origin_peer_id"):
            broken = dict(current); broken.pop(key)
            with self.subTest(key=key), self.assertRaises(GuardianError): validate_manifest(broken)
        with self.assertRaises(GuardianError): validate_manifest({**current, "schema": "aura.host_job_manifest.v1"})
        with self.assertRaises(GuardianError): validate_manifest({**old, "schema": "aura.host_job_manifest.v2"})
        with self.assertRaises(GuardianError): self.manifest(user_message_id=str(uuid4()))

    def test_v2_remote_identity_fields_cannot_be_missing_malformed_or_counter_aliases(self):
        current = self.manifest(user_message_id=str(uuid4()), settings_scope="c" * 64, origin_peer_id="d" * 64)
        for key, values in (("user_message_id", (None, 1, True, "0" * 36)),
                            ("settings_scope", (None, "C" * 64, "c" * 63, {})),
                            ("origin_peer_id", (None, "D" * 64, "remote-device", []))):
            for value in values:
                with self.subTest(key=key, kind=type(value).__name__), self.assertRaises(GuardianError):
                    validate_manifest({**current, key: value})

    def test_explicit_unicode_environment_is_sorted_double_terminated_and_copied(self):
        source = {"z-last": "", "Path": "C:/trusted/bin", "AURA_中文": "中 🌊 = value", "=C:": "C:/owned"}
        block = environment_block(source)
        source["AURA_中文"] = "changed after snapshot"
        self.assertTrue(block.endswith("\0\0"))
        self.assertFalse(block.endswith("\0\0\0"))
        self.assertIn("AURA_中文=中 🌊 = value\0", block)
        self.assertNotIn("changed after snapshot", block)
        self.assertLess(block.index("Path="), block.index("z-last="))
        self.assertEqual(environment_block({}), "\0\0")

    def test_invalid_environment_is_fixed_error_with_no_values_in_message(self):
        secret = "synthetic-never-in-exception"
        for value in (None, [], {"A": secret + "\0"}, {"A\0B": secret}, {"BAD=NAME": secret},
                      {"": secret}, {"A": 123}, {"A": "\ud800"}, {"\udfff": secret},
                      {"Path": secret, "PATH": "another"}, {"A": "x" * (1024 * 1024)}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(GuardianError) as caught:
                environment_block(value)
            self.assertEqual(str(caught.exception), "GUARDIAN_ENVIRONMENT_INVALID")
            self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__": unittest.main()
