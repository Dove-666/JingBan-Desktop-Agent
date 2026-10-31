"""Isolated real Android/iroh QA host with a MOCK HTTP chat model.

Run after installing the debug APK, before the opt-in AndroidDesktopE2eTest:
    .venv/Scripts/python.exe tools/android_desktop_fixture.py --adb E:/Android/Sdk/platform-tools/adb.exe --serial emulator-5554

Only a private temporary directory holds the consumed pairing URI. ADB sends
configuration via stdin to app-private files; no secret is a command argument.
The app writes aura-e2e-command.json; the bridge reads it through run-as and
the Qt thread handles delete/revoke commands. This is test control, not a new
production RPC or an HTTP backdoor. No user configuration/vault is opened.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.mock_api import MockApiServer
from tests.test_settings import FakeVault


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class AdbBridge(threading.Thread):
    def __init__(self, adb: str, serial: str, package: str, incoming: Queue, outgoing: Queue):
        super().__init__(name="android-private-qa-files", daemon=True)
        self.prefix = [adb] + (["-s", serial] if serial else [])
        self.package = package
        self.incoming, self.outgoing = incoming, outgoing
        self.stop = threading.Event()
        self.seen = set()
        self.config_published = threading.Event()

    def execute(self, args: list[str], data: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self.prefix + args, input=data, capture_output=True, timeout=8,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def push(self, name: str, data: dict) -> bool:
        if not re.fullmatch(r"aura-e2e-[a-z]+\.json", name):
            raise ValueError("Unexpected private filename")
        self.execute(["shell", "run-as", self.package, "mkdir", "-p", "files"])
        # A single quoted remote shell command keeps redirection inside run-as.
        # The package and filename are strictly validated, and stdin carries the
        # secret JSON. Never echo stderr or the command's input to a report.
        remote = f"run-as {self.package} sh -c 'cat > files/{name}.tmp'"
        written = self.execute(["shell", remote], json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if written.returncode:
            return False
        moved = self.execute(["shell", "run-as", self.package, "mv", f"files/{name}.tmp", f"files/{name}"])
        return moved.returncode == 0

    def run(self):
        pending = None
        try:
            self.execute(["shell", "run-as", self.package, "rm", "-f", "files/aura-e2e-command.json",
                          "files/aura-e2e-ack.json", "files/aura-e2e-result.json"])
        except (OSError, subprocess.TimeoutExpired):
            pass
        while not self.stop.is_set():
            try:
                if pending is None:
                    try:
                        pending = self.outgoing.get_nowait()
                    except Empty:
                        pass
                if pending is not None and self.push(*pending):
                    if pending[0] == "aura-e2e-config.json":
                        self.config_published.set()
                    pending = None
                pulled = self.execute(["exec-out", "run-as", self.package, "cat", "files/aura-e2e-command.json"])
                if pulled.returncode == 0 and len(pulled.stdout) <= 16384:
                    command = json.loads(pulled.stdout.decode("utf-8"))
                    identity = command.get("id")
                    if isinstance(identity, str) and identity not in self.seen:
                        self.seen.add(identity)
                        self.incoming.put(command)
            except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError, TypeError):
                pass  # Installation/ADB may still be starting; no secrets are logged.
            self.stop.wait(.35)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="E:/Android/Sdk/platform-tools/adb.exe")
    parser.add_argument("--serial", default="")
    parser.add_argument("--package", default="com.aura.companion.debug")
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--report", type=Path, default=PROJECT / "artifacts/android/qa/desktop-fixture-report.json")
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{1,150}", args.package) or not Path(args.adb).is_file():
        parser.error("A valid installed debug package and adb executable are required")
    directory = args.private_dir.resolve() if args.private_dir else Path(tempfile.mkdtemp(prefix="aura-android-private-qa-")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    incoming, outgoing = Queue(), Queue()
    bridge = AdbBridge(args.adb, args.serial, args.package, incoming, outgoing)
    bridge.start()
    report = {"isolated_fixture": True, "mock_chat_model": True, "hardware_phone_tested": False,
              "cross_network_tested": False, "commands": [], "ready": False}
    started = time.monotonic()
    seen_host_commands = set()
    published = False
    injection_announced = False
    pairing_generation = 0
    seed_id = str(uuid4())
    with MockApiServer() as api:
        settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat),
                                   CredentialStore(backend=FakeVault()))
        settings.save_api_settings(ApiSettings(api.base_url, "isolated-mock-model", 10))
        settings.save_api_key("ISOLATED_ANDROID_MOCK_KEY_NO_REAL_SERVICE")
        for index in range(1, 31):
            api.enqueue(json.dumps({"reply": f"Android E2E reply {index}", "emotion": "happy"}), delay=.05)
        session = ApplicationController(PetConfig(), settings)
        user = session.chat.store.append_message(seed_id, "user", "Isolated desktop seed", "user",
                                                  metadata={"request_status": "pending"})
        session.chat.store.complete_request(seed_id, user["id"], "ANDROID_DESKTOP_SEED")
        session.remote.set_enabled(True)
        session.finished.connect(app.quit)

        def publish_pairing() -> dict:
            nonlocal injection_announced, pairing_generation
            pairing = session.remote.begin_pairing()
            if not pairing:
                raise RuntimeError("Isolated endpoint is not ready for pairing")
            pairing_generation += 1
            configuration = {"isolated_fixture": True, "mock_chat_model": True,
                             "pairing_uri": pairing["uri"], "seed_conversation_id": seed_id,
                             "fixture_generation": pairing_generation}
            private_json(directory / "pairing-private.json", configuration)
            bridge.config_published.clear()
            injection_announced = False
            outgoing.put(("aura-e2e-config.json", configuration))
            report["pairing_generation"] = pairing_generation
            report["config_injected"] = False
            return {"renewed": True, "generation": pairing_generation, "pairing_lifetime_seconds": 300}

        def inspect() -> dict:
            return {"mock_chat_requests": len(api.requests), "authorized_devices": len(session.remote.devices),
                    "expression": session.pet_controller.expression,
                    "conversation_count": len(session.chat.store.list_conversations()),
                    "chat_busy": session.chat.busy}

        def tick():
            nonlocal published, injection_announced
            if time.monotonic() - started > args.max_seconds:
                report["stopped_reason"] = "fixture_timeout"
                app.quit()
                return
            if not published and session.remote.status["state"] == "listening":
                publish_pairing()
                published = True
                report["ready"] = True
                # Paths and flags only. Never print URI, code, ticket or vault contents.
                print(json.dumps({"ready": True, "private_directory": str(directory), "report": str(args.report)}), flush=True)
                private_json(args.report, report)
            if bridge.config_published.is_set() and not injection_announced:
                injection_announced = True
                report["config_injected"] = True
                private_json(args.report, report)
                print(json.dumps({"config_injected": True, "generation": pairing_generation}), flush=True)
            if session.remote.status["state"] == "error" and not published:
                report["startup_error"] = session.remote.status["message"]
                private_json(args.report, report)
            host = directory / "desktop-control.json"
            try:
                if host.is_file():
                    command = json.loads(host.read_text(encoding="utf-8"))
                    identity = command.get("id")
                    if identity not in seen_host_commands:
                        seen_host_commands.add(identity)
                        incoming.put(command)
            except (OSError, ValueError, TypeError):
                pass
            try:
                command = incoming.get_nowait()
            except Empty:
                return
            identity, action = command.get("id"), command.get("action")
            params = command.get("params") or {}
            if not isinstance(identity, str) or not isinstance(params, dict):
                return
            if action == "delete_conversation" and session.chat.busy:
                incoming.put(command)
                return
            try:
                if action == "inspect":
                    result = inspect()
                elif action == "delete_conversation":
                    conversation_id = params.get("conversation_id")
                    if not isinstance(conversation_id, str) or str(UUID(conversation_id)) != conversation_id:
                        raise ValueError()
                    # Use the real controller, then wait for its worker commit
                    # before acknowledging. Re-queue while its async save runs.
                    if session.chat.store.get_conversation(conversation_id) is not None:
                        session.chat.delete_conversation(conversation_id)
                        incoming.put(command)
                        return
                    result = {"deleted": True, "conversation_id": conversation_id}
                elif action == "revoke_devices":
                    for device in session.remote.devices:
                        session.remote.revoke_device(device["id"])
                    result = inspect()
                elif action == "renew_pairing":
                    # Explicit test-control action only. Never renew from a
                    # timer, unauthorized response or an already revoked peer.
                    result = publish_pairing()
                elif action == "stop":
                    result = inspect()
                    app.quit()
                else:
                    raise ValueError()
                acknowledgement = {"id": identity, "ok": True, "result": result}
                report["commands"].append({"action": action, "ok": True})
            except Exception:
                acknowledgement = {"id": identity, "ok": False, "error": "isolated_fixture_command_failed"}
                report["commands"].append({"action": action, "ok": False})
            outgoing.put(("aura-e2e-ack.json", acknowledgement))
            private_json(directory / "desktop-control-ack.json", acknowledgement)
            report.update(inspect())
            report["config_injected"] = bridge.config_published.is_set()
            private_json(args.report, report)

        timer = QTimer()
        timer.setInterval(100)
        timer.timeout.connect(tick)
        timer.start()
        try:
            app.exec()
        finally:
            timer.stop()
            report.update(inspect())
            report["config_injected"] = bridge.config_published.is_set()
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            private_json(args.report, report)
            session.shutdown()
            deadline = time.monotonic() + 10
            while QThreadPool.globalInstance().activeThreadCount() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(.01)
            bridge.stop.set()
            bridge.join(timeout=10)
            # The temporary authorization is consumed/cancelled. Remove the
            # host copy too; only synthetic history and public reports remain.
            (directory / "pairing-private.json").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
