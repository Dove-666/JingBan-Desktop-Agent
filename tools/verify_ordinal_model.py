"""Explicit opt-in, read-only real vision-provider test on our fixture PNGs."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-configured-verifier", action="store_true", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    allowed = (ROOT / "artifacts/gui_video_followup_20261101/ordinal_pipeline").resolve()
    if not manifest_path.is_relative_to(allowed):
        raise ValueError("OWN_FIXTURE_MANIFEST_REQUIRED")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    if isinstance(cases, dict):
        cases = [{"name": name, **case} for name, case in cases.items()]
    if not 1 <= len(cases) <= 4:
        raise ValueError("INVALID_FIXTURE_CASES")
    from PySide6.QtCore import QCoreApplication, QSettings
    from desktop_pet.settings_manager import SettingsManager
    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    ini = Path(os.environ["LOCALAPPDATA"]) / "AURA Desktop Pet/settings.ini"
    before = hashlib.sha256(ini.read_bytes()).hexdigest()
    manager = SettingsManager(QSettings(str(ini), QSettings.Format.IniFormat))
    settings = manager.load_agent_settings()
    key = manager.get_api_key()
    if not key or not settings.gui_model or not shutil.which("node"):
        raise RuntimeError("CONFIGURED_VERIFIER_UNAVAILABLE")
    request = {"config": {"baseURL": settings.gui_base_url, "model": settings.gui_model, "apiKey": key}, "cases": []}
    for c in cases:
        observations = []
        for o in c["observations"]:
            path = (manifest_path.parent / o["file"]).resolve()
            if not path.is_relative_to(allowed) or path.suffix.lower() != ".png":
                raise ValueError("OWN_FIXTURE_PNG_REQUIRED")
            raw = path.read_bytes()
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 4 * 1024 * 1024:
                raise ValueError("INVALID_FIXTURE_PNG")
            action = o.get("after_action")
            # Match production's low-level evidence, not semantic fixture labels.
            if action in {"next", "like", "noop"}:
                action = "click"
            observations.append({"base64": base64.b64encode(raw).decode("ascii"),
                **{name: o[name] for name in ("sequence", "captured_at") if name in o},
                "after_action": action or "none"})
        request["cases"].append({"name": c["name"], "task": c["task"], "observations": observations})
    environment = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
    completed = subprocess.run([shutil.which("node"), str(ROOT / "tools/verify_ordinal_model.mjs")],
        input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=150, env=environment, creationflags=0x08000000 if os.name == "nt" else 0)
    request["config"]["apiKey"] = ""
    key = ""
    if completed.returncode:
        raise RuntimeError("VERIFIER_PROBE_FAILED")
    result = json.loads(completed.stdout)
    results = result.get("results") if isinstance(result, dict) else None
    if not isinstance(results, list) or len(results) != len(cases):
        raise RuntimeError("VERIFIER_RESULT_COUNT_MISMATCH")
    for c, actual in zip(cases, results):
        if (not isinstance(actual, dict) or actual.get("name") != c["name"]
                or not isinstance(actual.get("verification"), dict)
                or actual["verification"].get("status") not in {"verified", "failed", "unverified"}):
            raise RuntimeError("VERIFIER_RESULT_IDENTITY_OR_STATUS_INVALID")
        actual["expected"] = c["expected"]
        status = actual["verification"]["status"]
        actual["matched"] = status == "verified" if c["expected"] == "verified" else status != "verified"
    result.update({"passed": all(r["matched"] for r in result["results"]), "model": settings.gui_model,
        "scope": "real configured vision verifier; read-only owned fixture PNGs; no desktop actions or real account task",
        "tested_at": datetime.now(timezone.utc).isoformat(), "settings_read_only": True,
        "ini_hash_unchanged": before == hashlib.sha256(ini.read_bytes()).hexdigest()})
    target = allowed.parent / "real_model_verification.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
