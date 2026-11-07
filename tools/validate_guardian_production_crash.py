"""Freeze sources and run the owned Qt/SDK loopback App-crash canary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.freeze_agent_benchmark_sources import freeze


def main():
    destination = ROOT / "artifacts" / ("guardian-production-source-" + uuid4().hex[:12])
    manifest = freeze(destination)
    extras = set((ROOT / "tests").rglob("*.py")) | set((ROOT / "agent_backend/nanobot/tests").glob("*.py"))
    extras.update(ROOT / name for name in ("tools/qa_guardian_queue_app.py", "tools/freeze_agent_benchmark_sources.py"))
    copied = {}
    for source in sorted(extras):
        relative = source.relative_to(ROOT).as_posix()
        raw = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        copied[relative] = hashlib.sha256(raw).hexdigest()
    (destination / "qa-extras-manifest.json").write_text(json.dumps({"schema": "aura.guardian_qa_extras.v1",
        "files": copied, "user_data_copied": False}, indent=2) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-u", str(destination / "tests/test_guardian_production_crash.py")],
        capture_output=True, text=True, encoding="utf-8", timeout=110)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(json.dumps({"passed": False, "code": "GUARDIAN_CONTROLLED_CRASH_VALIDATION_FAILED",
                          "source_snapshot": destination.name}))
        return 1
    report_path = destination / "artifacts/guardian-production-v1-actual-crash.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    unchanged = all(hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest for name, digest in copied.items())
    report.update(source_snapshot=destination.name, qa_extras_unchanged=unchanged,
                  source_copy_consistent=manifest["source_copy_consistent"])
    report["passed"] = report["passed"] is True and unchanged
    (ROOT / "artifacts/guardian-production-v1-actual-crash.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "planner_requests": report["planner_requests"],
                      "source_snapshot": destination.name, "qa_extras_unchanged": unchanged}))
    return 0 if report["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
