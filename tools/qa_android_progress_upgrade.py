"""Opt-in synthetic release upgrade QA, restricted to owned emulator-5560.

No keys or decrypted settings are exported. A previously unpaired emulator gets
explicitly fake pairing fields and one owned conversation; no connection/send is
performed. Cleanup removes only that run's exact rows/settings. Import is inert.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ADB = Path("E:/Android/Sdk/platform-tools/adb.exe")
SERIAL = "emulator-5560"
PACKAGE = "com.aura.companion"
BASE = "/data/user/0/" + PACKAGE
DB = BASE + "/databases/companion.db"
PREFS = BASE + "/shared_prefs/appearance.xml"


def _adb(*args, data=None, check=True):
    result = subprocess.run([str(ADB), "-s", SERIAL, *args], input=data,
                            capture_output=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError("QA_ADB_OPERATION_FAILED")
    return result


def _identity():
    if (_adb("shell", "getprop", "ro.kernel.qemu").stdout.strip() != b"1"
            or _adb("emu", "avd", "name").stdout.splitlines()[0].strip() != b"aura_api35"
            or b"uid=0" not in _adb("shell", "su", "0", "id").stdout):
        raise RuntimeError("OWNED_ROOTED_QA_EMULATOR_REQUIRED")


def _sql(statement):
    return _adb("shell", "su", "0", "sqlite3", DB,
                data=(statement + "\n").encode("utf-8")).stdout.decode("utf-8").strip()


def _q(value):
    return "'" + value.replace("'", "''") + "'"


def _write_preferences(data):
    uid = _adb("shell", "su", "0", "stat", "-c", "%u", BASE).stdout.decode().strip()
    if not re.fullmatch(r"[0-9]{4,6}", uid):
        raise RuntimeError("QA_PACKAGE_UID_UNAVAILABLE")
    _adb("shell", "su", "0", "sh", "-c", f"'cat > {PREFS}'", data=data)
    _adb("shell", "su", "0", "chown", uid + ":" + uid, PREFS)
    _adb("shell", "su", "0", "chmod", "600", PREFS)


def prepare_and_upgrade(apk, output):
    from verify_android_upgrade import verify
    _identity()
    current = _adb("shell", "dumpsys", "package", PACKAGE).stdout.decode()
    if not re.search(r"versionName=1\.12\.0(?:\s|$)", current):
        raise RuntimeError("RELEASE_1_12_BASELINE_REQUIRED")
    _adb("shell", "am", "force-stop", PACKAGE)
    initial = _sql("SELECT COUNT(*) FROM messages; SELECT COUNT(*) FROM settings "
                   "WHERE key IN ('device_ticket','device_id','local_peer_id','device_name');").splitlines()
    if initial != ["0", "0"]:
        raise RuntimeError("EMPTY_UNPAIRED_QA_BASELINE_REQUIRED")
    if _adb("shell", "su", "0", "test", "-e", PREFS, check=False).returncode == 0:
        raise RuntimeError("EMPTY_APPEARANCE_BASELINE_REQUIRED")
    nonce, cid, mid = str(uuid4()), str(uuid4()), str(uuid4())
    backup = "qa_upgrade_selected:" + nonce
    # Record exact ownership before the first reversible mutation, so an
    # interrupted preparation still has an explicit cleanup target.
    manifest = {"nonce": nonce, "cid": cid, "mid": mid, "backup": backup, "synthetic": True}
    output.mkdir(parents=True, exist_ok=True)
    (output / "synthetic-owner.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    fake = {"device_ticket": "qa-invalid-ticket-" + nonce,
            "device_id": "qa-not-a-native-peer-" + nonce,
            "local_peer_id": "qa-not-a-native-phone-" + nonce, "device_name": "升级检查电脑（合成）"}
    at = "2026-11-03T10:00:00Z"
    labels = ["分工阅读资料", "查看文档页面", "计算项目产物"]
    trace = json.dumps({"version": 1, "state": "completed", "started_at": at, "duration_ms": 3000,
                        "omitted_count": 0, "events": [
        {"id": "step-" + str(i), "kind": "tool", "label": label,
         "status": "completed", "elapsed_ms": i * 1000} for i, label in enumerate(labels)]}, ensure_ascii=False)
    commands = ["BEGIN IMMEDIATE;",
                f"INSERT INTO settings(key,value) SELECT {_q(backup)},value FROM settings WHERE key='selected_conversation';",
                f"INSERT INTO conversations(id,title,created_at,updated_at,message_count,local_only) VALUES({_q(cid)},'新工具进度验证',{_q(at)},{_q(at)},1,1);",
                f"INSERT INTO messages(id,conversation_id,role,content,created_at,source,request_status,execution_mode,agent_trace) VALUES({_q(mid)},{_q(cid)},'user','合成升级检查：三项工具进度',{_q(at)},'user','complete','agent',{_q(trace)});",
                f"INSERT OR REPLACE INTO settings(key,value) VALUES('selected_conversation',{_q(cid)});"]
    commands += [f"INSERT INTO settings(key,value) VALUES({_q(k)},{_q(v)});" for k, v in fake.items()]
    commands += ["COMMIT;"]
    _sql("\n".join(commands))
    preferences = (f'<?xml version="1.0" encoding="utf-8"?><map><string name="theme">LIGHT</string>'
                   f'<boolean name="auto_connect" value="false"/><string name="qa_upgrade_marker">{nonce}</string></map>').encode()
    _write_preferences(preferences)
    report = verify(ADB, SERIAL, apk)
    counts = _sql(f"SELECT COUNT(*) FROM messages WHERE id={_q(mid)} AND conversation_id={_q(cid)}; "
                  "SELECT COUNT(*) FROM settings WHERE key IN ('device_ticket','device_id','local_peer_id','device_name');").splitlines()
    report.update({"original_baseline_empty_unpaired": True, "synthetic_records_preserved": counts == ["1", "4"],
                   "synthetic_pairing_only": True, "actual_pairing_or_keystore_continuity_verified": False,
                   "model_calls": 0, "connections_attempted": 0,
                   "scope": "1.12→1.13 same-signer replacement; exact DB/appearance bytes and explicitly fake pairing fields before first launch"})
    (output / "upgrade.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not (report["userdata_hashes_unchanged"] and report["first_install_time_preserved"]
            and report["synthetic_records_preserved"] and report["after"]["versionName"] == "1.13.0"):
        raise RuntimeError("QA_UPGRADE_NOT_VERIFIED")
    return report


def cleanup(output):
    _identity()
    manifest = json.loads((output / "synthetic-owner.json").read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True:
        raise RuntimeError("QA_SYNTHETIC_OWNER_REQUIRED")
    for key in ("nonce", "cid", "mid"):
        if str(__import__("uuid").UUID(manifest[key])) != manifest[key]:
            raise RuntimeError("QA_SYNTHETIC_OWNER_INVALID")
    nonce, cid, mid, backup = (manifest[k] for k in ("nonce", "cid", "mid", "backup"))
    if backup != "qa_upgrade_selected:" + nonce:
        raise RuntimeError("QA_SYNTHETIC_OWNER_INVALID")
    _adb("shell", "am", "force-stop", PACKAGE)
    fake = {"device_ticket": "qa-invalid-ticket-" + nonce, "device_id": "qa-not-a-native-peer-" + nonce,
            "local_peer_id": "qa-not-a-native-phone-" + nonce, "device_name": "升级检查电脑（合成）"}
    commands = ["BEGIN IMMEDIATE;", f"DELETE FROM messages WHERE id={_q(mid)} AND conversation_id={_q(cid)};",
                f"DELETE FROM conversations WHERE id={_q(cid)} AND title='新工具进度验证' AND NOT EXISTS(SELECT 1 FROM messages WHERE conversation_id={_q(cid)});",
                f"DELETE FROM settings WHERE key='selected_conversation' AND value={_q(cid)};",
                f"INSERT OR IGNORE INTO settings(key,value) SELECT 'selected_conversation',value FROM settings WHERE key={_q(backup)};",
                f"DELETE FROM settings WHERE key={_q(backup)};"]
    commands += [f"DELETE FROM settings WHERE key={_q(k)} AND value={_q(v)};" for k, v in fake.items()]
    commands += ["COMMIT;"]
    _sql("\n".join(commands))
    tree = ET.fromstring(_adb("shell", "su", "0", "cat", PREFS).stdout)
    for item in list(tree):
        if item.get("name") == "qa_upgrade_marker" and item.text == nonce:
            tree.remove(item)
    _write_preferences(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
    count = _sql(f"SELECT COUNT(*) FROM messages WHERE id={_q(mid)}; SELECT COUNT(*) FROM conversations WHERE id={_q(cid)}; "
                 f"SELECT COUNT(*) FROM settings WHERE key={_q(backup)} OR (key IN ('device_ticket','device_id','local_peer_id','device_name'));")
    report = {"synthetic_exact_rows_removed": count.splitlines() == ["0", "0", "0"],
              "release_data_cleared": False, "release_uninstalled": False, "model_calls": 0,
              "scope": "Only this owned synthetic conversation, message and matching fake pairing values removed; unrelated data retained"}
    (output / "cleanup.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["synthetic_exact_rows_removed"]:
        raise RuntimeError("QA_EXACT_CLEANUP_NOT_VERIFIED")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/companion-1.13.0")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status": "not_run", "adb_started": False})); return 0
    if args.cleanup:
        result = cleanup(args.output)
    else:
        if args.apk is None or not args.apk.is_file():
            parser.error("--apk is required for opt-in upgrade")
        result = prepare_and_upgrade(args.apk, args.output)
    print(json.dumps({key: value for key, value in result.items() if isinstance(value, bool)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
