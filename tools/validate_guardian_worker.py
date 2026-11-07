"""Safe fixed-fixture report for the isolated suspended-worker adapter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    names = ["tests.test_guardian_pipe_close", "tests.test_guardian_worker"]
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    paths = ["desktop_pet/agents/guardian_worker.py", "tools/qa_guardian_worker_fixture.py",
             "tests/test_guardian_pipe_close.py", "tests/test_guardian_worker.py"]
    report = {"schema": "aura.isolated_guardian_worker_validation.v1", "passed": result.wasSuccessful(),
        "tests_run": result.testsRun, "skipped": len(result.skipped),
        "failed_fixed_test_ids": [test.id() for test, _ in [*result.failures, *result.errors]],
        "source_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in paths},
        "scope": {"real_native_suspended_worker": sys.platform == "win32", "same_lifecycle_object": True,
            "existing_stage_lf_protocol": True, "unicode_stdio": True,
            "app_crash_before_dispatch_and_after_child_spawn": True,
            "unconfirmed_cleanup_strong_registry": True, "close_failure_sticky": True,
            "native_attempt_hook_before_launch": True, "private_appconfig_model_scope_exclusion": True,
            "fresh_host_scope_callback_late_grant_rejection": True, "production_scope_guard_wired": False,
            "restart_stopped_only_cas": True, "production_registration": False,
            "production_queue_recovery": False, "model_called": False, "effects_replayed": False,
            "runtime_entire_tree_pinned": False, "same_user_host_compromise_defended": False,
            "power_loss_or_all_handoff_crashes_validated": False}}
    target = REPO / "artifacts" / "isolated-guardian-worker-validation.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
