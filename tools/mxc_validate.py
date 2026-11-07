"""Opt-in fixed-code backend validation; saves no raw process/test logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-fixed-payloads", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    if args.real_fixed_payloads:
        os.environ["AURA_RUN_MXC_INTEGRATION"] = "1"
    else:
        os.environ.pop("AURA_RUN_MXC_INTEGRATION", None)
    from agent_backend.nanobot import mxc_execution as mxc
    from agent_backend.nanobot.tests import test_mxc_execution
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_mxc_execution)

    class Result(unittest.TextTestResult):
        fixed_results = {}
        def addSuccess(self, test):
            self.fixed_results[test.id()] = "PASS"
            super().addSuccess(test)
        def addSkip(self, test, reason):
            self.fixed_results[test.id()] = "SKIPPED"
            super().addSkip(test, reason)
        def addFailure(self, test, error):
            self.fixed_results[test.id()] = "FAIL"
            super().addFailure(test, error)
        def addError(self, test, error):
            self.fixed_results[test.id()] = "ERROR"
            super().addError(test, error)

    result = unittest.TextTestRunner(verbosity=1, resultclass=Result).run(suite)
    report = {"fixed_code": "MXC_FIXED_VALIDATION_PASSED" if result.wasSuccessful() else "MXC_FIXED_VALIDATION_FAILED",
        "real_fixed_payloads_enabled": args.real_fixed_payloads, "model_calls": False,
        "default_backend_enabled": mxc.MxcPythonRunner().live_enabled,
        "scope": {"readonly_roots": [str(mxc.SYSTEM32), str(mxc.PYTHON_ROOT)], "original_workspace_granted": False,
            "minimal_platform_roots": False, "network_enabled": False, "file_exports": False,
            "managed_requirements_included": True, "managed_policy_injection_tested": False},
        "capability": mxc.MxcPythonRunner().capability(), "fixed_tests": result.fixed_results,
        "hashes": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
            "agent_backend/nanobot/mxc_execution.py", "agent_backend/nanobot/mxc_native.py", "agent_backend/nanobot/mxc_entry.py", "agent_backend/nanobot/tests/test_mxc_execution.py")},
        "runtime_pins": {"codex_sha256": mxc.CODEX_SHA256, "codex_version": mxc.CODEX_VERSION, "python_sha256": mxc.PYTHON_SHA256},
        "bare_cli_cancel_evidence": "artifacts/qa-mxc-probe-20261103/report.json"}
    destination = root / "artifacts" / "mxc-backend-validation"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / ("fixed-real.json" if args.real_fixed_payloads else "offline.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
