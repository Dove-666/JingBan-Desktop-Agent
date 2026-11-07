"""Independent storage report acceptance rejects plausible but false claims."""
from copy import deepcopy
import json
import types
import unittest

from tools.qa_windows_storage_report import check_report


class StorageReportOracleTests(unittest.TestCase):
    def setUp(self):
        self.disk = types.SimpleNamespace(total=10000, free=3000)
        self.observation = {"disk": {"drive": "C:\\", "total_bytes": 10000, "used_bytes": 7000, "free_bytes": 3000},
            "status": "partial", "cleanup_performed": False, "read_only": True, "deleted_files": 0,
            "file_contents_read": False, "categories": [{"category": "user_temporary_files", "measured_bytes": None,
                "scan_status": "not_scanned", "recommendation": "review_temporary_files"}]}
        self.value = {**self.observation["disk"], "scan_complete": False, "guaranteed_reclaimable_bytes": None,
            "cleanup_performed": False, "categories": deepcopy(self.observation["categories"])}

    def check(self, value):
        return check_report(json.dumps(value), self.observation, self.disk, self.disk)

    def test_actual_partial_observation_can_pass_without_inventing_zero_or_reclaim(self):
        self.assertTrue(self.check(self.value)["passed"])

    def test_unscanned_is_not_zero_and_partial_is_not_complete(self):
        for change in (lambda v: v.update(scan_complete=True),
                       lambda v: v["categories"][0].update(measured_bytes=0),
                       lambda v: v.update(guaranteed_reclaimable_bytes=0)):
            with self.subTest(change=change):
                value = deepcopy(self.value); change(value)
                self.assertFalse(self.check(value)["passed"])

    def test_plausible_disk_value_or_cleanup_claim_cannot_replace_actual_observation(self):
        for field, value in (("total_bytes", 10001), ("free_bytes", 3001), ("cleanup_performed", True), ("total_bytes", True)):
            with self.subTest(field=field):
                modified = deepcopy(self.value); modified[field] = value
                self.assertFalse(self.check(modified)["passed"])


if __name__ == "__main__": unittest.main()
