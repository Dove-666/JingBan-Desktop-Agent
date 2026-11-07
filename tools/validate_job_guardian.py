"""Run fixed guardian canaries; write only hashes, fixed IDs and measurements."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import tests.test_job_guardian as validation
import tests.test_host_guardian_review as independent_review


def main():
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromModule(validation),
                               unittest.defaultTestLoader.loadTestsFromModule(independent_review)])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    paths = ["desktop_pet/agents/guardian_native.py", "desktop_pet/agents/job_guardian.py",
        "desktop_pet/agents/job_guardian_helper.py", "tools/qa_job_guardian_fixture.py", "tests/test_job_guardian.py",
        "tests/test_host_guardian_review.py"]
    memory = validation.MEASUREMENTS["helper_working_set_bytes"]
    report = {"schema": "aura.isolated_job_guardian_validation.v1", "passed": result.wasSuccessful(),
        "tests_run": result.testsRun, "skipped": len(result.skipped),
        "test_boundaries": {"producer_real_owned_process_fixture_methods": 10,
            "producer_real_pipe_method": 1, "producer_strict_record_method": 1,
            "independent_review_methods": 11, "independent_actual_os_format_pipe_methods": 3},
        "failed_fixed_test_ids": [test.id() for test, _ in [*result.failures, *result.errors]],
        "source_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in paths},
        "helper_working_set_bytes": {"samples": len(memory), "minimum": min(memory) if memory else None,
            "maximum": max(memory) if memory else None},
        "external_job_observations": sorted(set(validation.MEASUREMENTS["guardian_in_external_job"])),
        "scope": {"real_win32_jobs_and_process_handles": sys.platform == "win32",
            "fixed_owned_parent_worker_grandchild_only": True, "no_model_or_network_or_user_documents": True,
            "production_registration": False, "production_queue_recovery": False,
            "new_receipt_is_stopped_only": True, "missing_pid_is_closure_evidence": False,
            "same_user_memory_compromise_defended": False, "universal_external_job_survival_proven": False,
            "power_loss_or_os_reboot_proven": False, "all_handoff_crash_points_proven": False,
            "hard_history_or_disk_quota": False}}
    target = REPO / "artifacts" / "isolated-job-guardian-validation.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
