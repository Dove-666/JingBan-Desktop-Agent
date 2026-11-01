"""Isolated real Android/iroh QA host; a MOCK HTTP model is the default.

Run after installing the debug APK, before the opt-in AndroidDesktopE2eTest:
    .venv/Scripts/python.exe tools/android_desktop_fixture.py --adb E:/Android/Sdk/platform-tools/adb.exe --serial emulator-5560

Only a private temporary directory holds the consumed pairing URI. ADB sends
configuration via stdin to app-private files; no secret is a command argument.
The app writes aura-e2e-command.json; the bridge reads it through run-as and
the Qt thread handles delete/revoke commands. This is test control, not a new
production RPC or an HTTP backdoor. Only --use-existing-api explicitly reads
the desktop's current API preferences and vault credential, into memory. It
never changes the user's settings and blocks all desktop chat model requests.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import replace
import hashlib
import ipaddress
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
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.api_client import ApiError, SYSTEM_PROMPT
from desktop_pet.agents import AgentResult, AgentError, AgentService
from desktop_pet.config import PetConfig
from desktop_pet.settings_manager import ApiSettings, CredentialStore, CredentialsError, SettingsError, SettingsManager
from tests.mock_api import MockApiServer
from tests.test_settings import FakeVault


def install_local_only_qa() -> None:
    """Fixture-process override: direct addresses can be ready without a relay.

    The real endpoint, tickets, authenticated QUIC handshake and RPC streams
    remain native iroh. No production transport code or system network changes.
    Addresses are checked in memory and are never printed into diagnostic logs.
    """
    import iroh
    from desktop_pet.remote import transport
    from desktop_pet.remote.transport import TransportError

    original_bind = transport._bind_endpoint

    async def bind_ephemeral(sdk, secret, *_args, **_kwargs):
        # Production may reserve a fixed port. This independent QA endpoint
        # must never compete with the user's running desktop for that socket.
        return await original_bind(sdk, secret, listen_port=0)

    async def local_ready(endpoint):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for value in endpoint.addr().direct_addresses():
                try:
                    host, port = value.rsplit(":", 1)
                    address = ipaddress.ip_address(host.strip("[]"))
                    if (0 < int(port) <= 65535 and not address.is_unspecified
                            and not address.is_loopback and not address.is_link_local):
                        return
                except (ValueError, TypeError):
                    continue
            await asyncio.sleep(.05)
        raise TransportError("Local-only QA has no usable direct address.")

    iroh.Endpoint.online = local_ready
    transport._bind_endpoint = bind_ephemeral


class DesktopModelBlocker:
    """Keep real-provider QA strictly phone-only; no arguments are retained."""
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self):
        with self._lock:
            return self._calls

    def record(self):
        with self._lock:
            self._calls += 1

    def complete_context(self, *_args, **_kwargs):
        self.record()
        raise ApiError("fixture_desktop_ai_disabled", "Desktop model calls are disabled in this QA fixture.")

    complete = complete_context
    request_text = complete_context
    test_connection = complete_context

    def shutdown(self):
        pass


class SyntheticNativeAgent:
    """Native-RPC admission probe only; does not simulate actual tool quality."""
    def __init__(self):
        self.calls = []
        self.cancelled = 0

    def execute(self, settings, key, request, context, cancellation_event=None, progress_callback=None):
        self.calls.append({"kind": request.kind, "cid": context.get("conversation_id"),
                           "image": bool(context.get("current_image_url"))})
        ordinal = len(self.calls)
        if callable(progress_callback):
            progress_callback("planning")
        if "QA_WAIT_FOR_CANCEL" in request.task:
            if not cancellation_event.wait(45):
                raise AgentError("timeout", "Synthetic task timed out.")
            self.cancelled += 1
            raise AgentError("cancelled", "Synthetic task stopped.")
        return AgentResult(f"Android Agent reply {ordinal}", kind="agent")

    def cancel_request(self, event):
        event.set()

    def shutdown(self):
        pass


def readonly_worker_harness(workspace: Path) -> tuple[Path, Path]:
    """Owned worker harness: observe real tools, forbid every mutation/input."""
    guard = workspace / "blocked-actions.json"
    harness = workspace / "readonly-worker.py"
    harness.write_text(
        "import asyncio,importlib.util,json\nfrom pathlib import Path\n"
        f"spec=importlib.util.spec_from_file_location('qa_phone_readonly_worker',{str(PROJECT / 'agent_backend/nanobot/worker.py')!r})\n"
        "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
        "import windows_apps\n"
        f"guard=Path({str(guard)!r})\n"
        "def blocked(*args,**kwargs):\n"
        "    guard.write_text(json.dumps({'unexpected_action_requested':True}))\n"
        "    raise RuntimeError('READONLY_ACCEPTANCE_ACTION_BLOCKED')\n"
        "async def blocked_gui(*args,**kwargs):\n"
        "    blocked()\n"
        "worker.GuiBridge._run_once=blocked_gui\n"
        "worker.WorkspaceFiles.write=blocked\n"
        "windows_apps.WindowsApplications.begin_launch=blocked\n"
        "asyncio.run(worker.main())\n", encoding="utf-8")
    return harness, guard


class RealReadonlyAgent(AgentService):
    """Actual AgentService; retain only public acceptance facts from tool logs."""
    def __init__(self, guard: Path):
        super().__init__()
        self.guard = guard
        self.audit_lock = threading.Lock()
        self.audit = {"executions": 0, "completed": 0, "errors": 0,
                      "disk_observations": 0, "actual_c_drive_verified": False,
                      "last_reply_meaningful": False, "last_tools": [],
                      "safe_disk_activity_observed": False}

    def execute(self, settings, key, request, context, cancellation_event=None,
                progress_callback=None, event_callback=None):
        identifier = context.get("conversation_id")
        previous = len(self._sessions.get(identifier, []))
        with self.audit_lock:
            self.audit["executions"] += 1
        def observed_event(event):
            if event.get("label") == "检查磁盘占用":
                with self.audit_lock:
                    self.audit["safe_disk_activity_observed"] = True
            if callable(event_callback):
                event_callback(event)
        try:
            result = super().execute(settings, key, request, context,
                                     cancellation_event, progress_callback, observed_event)
            records = self._sessions.get(identifier, [])[previous:]
            observations = [r for r in records if r.get("role") == "tool"]
            known = {"inspect_system", "inspect_disk_usage", "inspect_directory", "system_info"}
            names = [r.get("name") if r.get("name") in known else "unexpected_tool" for r in observations]
            values = []
            for record in observations:
                if record.get("name") == "inspect_disk_usage":
                    try:
                        value = json.loads(record["content"])
                        if isinstance(value, dict):
                            values.append(value)
                    except (ValueError, TypeError):
                        pass
            verified = any(value.get("read_only") is True and value.get("deleted_files") == 0
                and (value.get("disk") or {}).get("drive") == "C:\\"
                and (value.get("disk") or {}).get("total_bytes", 0) > 0 for value in values)
            with self.audit_lock:
                self.audit.update(completed=self.audit["completed"] + int(result.status == "completed"),
                    disk_observations=self.audit["disk_observations"] + len(values),
                    actual_c_drive_verified=verified,
                    last_reply_meaningful=bool(result.reply.strip()) and any(
                        marker in result.reply for marker in ("C", "盘", "空间", "GB", "GiB")),
                    last_tools=names)
            return result
        except Exception:
            with self.audit_lock:
                self.audit["errors"] += 1
            raise

    def public_inspection(self):
        with self.audit_lock:
            return {"readonly_real_agent_qa": True, **self.audit,
                    "unexpected_action_requested": self.guard.exists(),
                    "real_desktop_captures": 0, "real_desktop_actions": 0,
                    "cleanup_performed": False, "real_api_key_exported": False}


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def inspect_mock_image_payloads(requests: list[dict]) -> dict:
    """Project actual HTTP image positions into a secret-free QA summary.

    Request bodies and Authorization headers remain in MockApiServer memory.
    Only booleans, ordinal numbers and decoded image digests leave this helper.
    """
    has_images, current_images, history_images, image_indexes, image_digests = [], [], [], [], []
    valid = True
    for ordinal, request in enumerate(list(requests), 1):
        users = [message for message in request.get("body", {}).get("messages", [])
                 if isinstance(message, dict) and message.get("role") == "user"]
        user_images = []
        for user in users:
            content = user.get("content")
            parts = content if isinstance(content, list) else []
            images = [part for part in parts if isinstance(part, dict) and part.get("type") == "image_url"]
            user_images.append(images)
            if images and (len(parts) != 2 or len(images) != 1 or not isinstance(parts[0], dict)
                    or parts[0].get("type") != "text" or not isinstance(parts[0].get("text"), str)
                    or not parts[0]["text"].strip()):
                valid = False
        images = [part for group in user_images for part in group]
        has_images.append(bool(images))
        current_images.append(bool(user_images and user_images[-1]))
        history_images.append(any(user_images[:-1]))
        if not images:
            continue
        image_indexes.append(ordinal)
        for part in images:
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            if not isinstance(url, str) or len(url) > 6 * 1024 * 1024:
                valid = False
                continue
            match = re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})", url)
            try:
                if match is None:
                    raise ValueError()
                decoded = base64.b64decode(match.group(1), validate=True)
                if not decoded or len(decoded) > 4 * 1024 * 1024:
                    raise ValueError()
                image_digests.append(hashlib.sha256(decoded).hexdigest())
            except (ValueError, TypeError):
                valid = False
    return {"image_requests": len(image_indexes), "image_request_indexes": image_indexes,
            "request_has_image": has_images, "image_payloads_valid": valid,
            "request_current_user_has_image": current_images,
            "request_history_has_image": history_images,
            "image_sha256": image_digests,
            "last_image_sha256": image_digests[-1] if image_digests else None,
            "last_request_has_image": has_images[-1] if has_images else False,
            "last_current_user_has_image": current_images[-1] if current_images else False,
            "last_history_has_image": history_images[-1] if history_images else False}


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--direct-api", action="store_true",
                      help="Use the Android emulator host gateway for isolated MOCK direct-API QA")
    mode.add_argument("--use-existing-api", action="store_true",
                      help="Explicitly read current desktop API configuration into an isolated in-memory vault; ordinary desktop chat AI is blocked")
    parser.add_argument("--local-only-qa", action="store_true",
                        help="MOCK fixtures only: allow ready when native direct addresses exist, without waiting for a public home relay")
    parser.add_argument("--desktop-assist-qa", action="store_true",
                        help="MOCK only: authorize a synthetic foreground observer; never capture or control the real desktop")
    parser.add_argument("--readonly-real-agent-qa", action="store_true",
                        help="OPT-IN real planner via agent.send only; requires --use-existing-api, blocks writes/input and credential export")
    parser.add_argument("--reply-start", type=int, default=1,
                        help="First synthetic reply number, for isolated continuation QA only")
    args = parser.parse_args()
    if args.readonly_real_agent_qa and not args.use_existing_api:
        parser.error("readonly-real-agent-qa requires explicit --use-existing-api")
    if args.local_only_qa and args.use_existing_api and not args.readonly_real_agent_qa:
        parser.error("local-only-qa is restricted to synthetic MOCK fixtures")
    if args.desktop_assist_qa and (args.use_existing_api or not args.direct_api):
        parser.error("desktop-assist-qa requires the synthetic direct-api fixture")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{1,150}", args.package) or not Path(args.adb).is_file():
        parser.error("A valid installed debug package and adb executable are required")
    if not 1 <= args.reply_start <= 10000:
        parser.error("reply-start must be a positive bounded synthetic index")
    if args.local_only_qa:
        install_local_only_qa()
    if os.environ.get("AURA_REMOTE_FRAME_PROBE") == "1":
        from tools.qa_remote_frame_probe import install_frame_probe
        install_frame_probe()
    directory = args.private_dir.resolve() if args.private_dir else Path(tempfile.mkdtemp(prefix="aura-android-private-qa-")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    existing_provider, existing_key = None, ""
    source_settings_path, source_settings_hash = None, None
    if args.use_existing_api:
        # Match the real entry point's QStandardPaths namespace. Read only the
        # API fields/key, never the user's chat database or remote preferences.
        app.setApplicationName("AURA Desktop Pet")
        try:
            source = SettingsManager()
            if args.readonly_real_agent_qa:
                source_settings_path = Path(source.config_path)
                source_settings_hash = hashlib.sha256(source_settings_path.read_bytes()).hexdigest()
            existing_provider = source.load_api_settings()
            existing_provider.validate(require_model=True)
            existing_key = source.get_api_key()
            if not existing_key.strip():
                raise CredentialsError("Credential unavailable")
        except (SettingsError, CredentialsError):
            unavailable = {"isolated_fixture": True, "mock_chat_model": False,
                           "ready": False, "api_config_available": False, "desktop_model_calls": 0}
            private_json(args.report, unavailable)
            print(json.dumps(unavailable), flush=True)
            return 2
        finally:
            source = None
    if not args.direct_api and not args.use_existing_api:
        # Legacy E2E exercises desktop-forwarded chat. Limit advertised methods
        # in this isolated process only, leaving production capabilities intact.
        from desktop_pet.remote.controller import CAPABILITIES
        CAPABILITIES[:] = [name for name in CAPABILITIES if name not in ("chat.config", "chat.import")]
    readonly_rpc = {"status", "agent.send", "agent.cancel", "sync.snapshot", "sync.pull", "operation.get",
                    "image.list", "image.get"}
    if args.readonly_real_agent_qa:
        from desktop_pet.remote.controller import CAPABILITIES
        CAPABILITIES[:] = [name for name in CAPABILITIES if name in readonly_rpc]
    incoming, outgoing = Queue(), Queue()
    bridge = AdbBridge(args.adb, args.serial, args.package, incoming, outgoing)
    bridge.start()
    report = {"isolated_fixture": True, "mock_chat_model": not args.use_existing_api, "hardware_phone_tested": False,
              "cross_network_tested": False, "commands": [], "ready": False,
              "local_only_qa": args.local_only_qa, "relay_tested": False,
              "random_port_qa": args.local_only_qa,
              "synthetic_desktop": args.desktop_assist_qa, "real_desktop_captures": 0, "real_desktop_actions": 0,
              "readonly_real_agent_qa": args.readonly_real_agent_qa,
              "desktop_model_calls": 0, "api_config_available": False}
    model_counter = DesktopModelBlocker()
    route_lock = threading.Lock()
    route_calls = {"chat.send": 0, "chat.import": 0, "agent.send": 0, "agent.cancel": 0}
    started = time.monotonic()
    seen_host_commands = set()
    published = False
    injection_announced = False
    pairing_generation = 0
    seed_id = str(uuid4())
    worker_override = None
    with MockApiServer() as api:
        settings = SettingsManager(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat),
                                   CredentialStore(backend=FakeVault()))
        if args.use_existing_api:
            # Ordinary preferences go only to this isolated INI; the credential
            # is held solely by FakeVault in this process, never in a key file.
            settings.save_api_settings(existing_provider, pending_api_key=existing_key)
            settings.save_api_key(existing_key)
            existing_key = ""
        else:
            model_base_url = api.base_url.replace("127.0.0.1", "10.0.2.2") if args.direct_api else api.base_url
            desktop_base_url = api.base_url if args.desktop_assist_qa else model_base_url
            settings.save_api_settings(ApiSettings(desktop_base_url, "isolated-mock-model", 10))
            settings.save_api_key("ISOLATED_ANDROID_MOCK_KEY_NO_REAL_SERVICE")
        if not args.use_existing_api:
            for index in range(args.reply_start, args.reply_start + 30):
                api.enqueue(json.dumps({"reply": f"Android E2E reply {index}", "emotion": "happy"}), delay=.05)
        session = ApplicationController(PetConfig(), settings,
                                        api_client=model_counter if args.use_existing_api else None)
        synthetic_captures = {"count": 0}
        synthetic_agent = SyntheticNativeAgent() if args.desktop_assist_qa else None
        readonly_agent = None
        if args.readonly_real_agent_qa:
            from desktop_pet.agents import service as agent_host
            workspace = PROJECT / "agent_workspace" / ("qa-phone-readonly-" + uuid4().hex)
            workspace.mkdir(parents=True)
            harness, guard = readonly_worker_harness(workspace)
            worker_override = patch.object(agent_host, "_WORKER_PATH", harness)
            worker_override.start()
            session.chat._agent_service.shutdown()
            readonly_agent = RealReadonlyAgent(guard)
            session.chat._agent_service = readonly_agent
            # Local metadata fallback avoids authorizing a second real call.
            session.chat._metadata_factory = None
            settings.save_agent_settings(replace(settings.load_agent_settings(), enabled=True,
                tools_enabled=True, computer_enabled=False, workspace=str(workspace)))
            session.chat.refresh_agent_settings()
            session.perception._timer.stop()
        if args.desktop_assist_qa:
            session.chat._agent_service = synthetic_agent
            settings.save_agent_settings(replace(settings.load_agent_settings(), enabled=True))
            session.chat.refresh_agent_settings()
            from PySide6.QtGui import QImage, QColor
            from desktop_pet.perception.screen_observer import ScreenObserver, ForegroundInfo
            info = ForegroundInfo(123, 456, "aura-qa-fixture.exe", "Synthetic desktop QA", 0, True)
            def synthetic_capture(_info):
                synthetic_captures["count"] += 1
                image = QImage(80, 60, QImage.Format.Format_RGB32)
                image.fill(QColor("#75b8f4"))
                return image
            # Keep the production privacy validator; only OS metadata/capture
            # providers are replaced with synthetic input in this test process.
            session.perception.observer = ScreenObserver(metadata_provider=lambda: info,
                capture=synthetic_capture, own_pid=-1)
            session.perception.settings = replace(session.perception.settings, enabled=True, allow_remote=True)
            session.perception._timer.stop()
        if not args.use_existing_api:
            original_complete = session.chat._client.complete_context
            def count_desktop_completion(*call_args, **call_kwargs):
                model_counter.record()
                return original_complete(*call_args, **call_kwargs)
            session.chat._client.complete_context = count_desktop_completion
        original_dispatch = session.remote._dispatch
        def count_dispatch(peer_id, request_id, method, params, generation):
            if args.readonly_real_agent_qa and method not in readonly_rpc:
                from desktop_pet.remote.errors import RemoteError
                # A real credential must never pass through chat.config or any
                # phone-direct API path, even if an unexpected caller asks.
                raise RemoteError("readonly_fixture_method_disabled", "This read-only QA fixture does not provide that method.")
            if method in route_calls:
                with route_lock:
                    route_calls[method] += 1
            return original_dispatch(peer_id, request_id, method, params, generation)
        session.remote._dispatch = count_dispatch
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
            configuration = {"isolated_fixture": True, "mock_chat_model": not args.use_existing_api,
                             "pairing_uri": pairing["uri"], "seed_conversation_id": seed_id,
                             "fixture_generation": pairing_generation,
                             "direct_api": (args.direct_api or args.use_existing_api) and not args.readonly_real_agent_qa,
                             "local_only_qa": args.local_only_qa, "relay_tested": False,
                             "random_port_qa": args.local_only_qa,
                             "synthetic_desktop": args.desktop_assist_qa,
                             "readonly_real_agent_qa": args.readonly_real_agent_qa,
                             "use_existing_api": args.use_existing_api}
            if args.direct_api and not args.use_existing_api:
                # Synthetic mock credentials only, delivered to the private
                # debug test file; never include real provider credentials.
                configuration["manual_api"] = {"base_url": model_base_url,
                    "model": settings.load_api_settings().model,
                    "api_key": settings.get_api_key(), "timeout_seconds": 10,
                    "reasoning_effort": "default"}
            private_json(directory / "pairing-private.json", configuration)
            bridge.config_published.clear()
            injection_announced = False
            outgoing.put(("aura-e2e-config.json", configuration))
            report["pairing_generation"] = pairing_generation
            report["config_injected"] = False
            return {"renewed": True, "generation": pairing_generation, "pairing_lifetime_seconds": 300}

        def inspect() -> dict:
            selected = settings.load_api_settings()
            expected_key = settings.get_api_key()
            last = api.requests[-1] if api.requests else {}
            messages = last.get("body", {}).get("messages", [])
            current_user = max((index for index, message in enumerate(messages)
                                if isinstance(message, dict) and message.get("role") == "user"), default=-1)
            with route_lock:
                sends, imports = route_calls["chat.send"], route_calls["chat.import"]
                agent_sends, agent_cancels = route_calls["agent.send"], route_calls["agent.cancel"]
            try:
                selected.validate(require_model=True)
                available = bool(expected_key.strip())
            except SettingsError:
                available = False
            if readonly_agent is not None:
                return {**readonly_agent.public_inspection(),
                    "authorized_devices": len(session.remote.devices), "chat_busy": session.chat.busy,
                    "mock_chat_requests": len(api.requests), "desktop_completion_calls": model_counter.calls,
                    "desktop_chat_send_operations": sends, "desktop_import_operations": imports,
                    "desktop_agent_send_operations": agent_sends, "desktop_agent_cancel_operations": agent_cancels,
                    "api_config_available": available, "chat_config_advertised": "chat.config" in CAPABILITIES,
                    "production_settings_unchanged": source_settings_path is not None and
                        source_settings_hash == hashlib.sha256(source_settings_path.read_bytes()).hexdigest()}
            return {"mock_chat_requests": len(api.requests), "authorized_devices": len(session.remote.devices),
                    "expression": session.pet_controller.expression,
                    "conversation_count": len(session.chat.store.list_conversations()),
                    "chat_busy": session.chat.busy, "direct_api_fixture": args.direct_api or args.use_existing_api,
                    "use_existing_api": args.use_existing_api,
                    "desktop_model_calls": model_counter.calls + len(api.metadata_requests), "api_config_available": available,
                    "desktop_completion_calls": model_counter.calls, "desktop_metadata_requests": len(api.metadata_requests),
                    # These are dispatch attempt counts, including duplicate
                    # retries; no method is inferred from operation hashes.
                    "desktop_chat_send_operations": sends, "desktop_import_operations": imports,
                    "desktop_agent_send_operations": agent_sends,
                    "desktop_agent_cancel_operations": agent_cancels,
                    "desktop_agent_executions": len(synthetic_agent.calls) if synthetic_agent else 0,
                    "agent_last_kind": synthetic_agent.calls[-1]["kind"] if synthetic_agent and synthetic_agent.calls else None,
                    "agent_last_has_image": synthetic_agent.calls[-1]["image"] if synthetic_agent and synthetic_agent.calls else False,
                    "agent_session_turns": sum(call["cid"] == synthetic_agent.calls[-1]["cid"] for call in synthetic_agent.calls) if synthetic_agent and synthetic_agent.calls else 0,
                    "agent_context_classification_absent": True,
                    "synthetic_desktop_captures": synthetic_captures["count"],
                    "system_prompt_present": bool(messages) and isinstance(messages[0], dict) and messages[0].get("role") == "system"
                        and messages[0].get("content") == SYSTEM_PROMPT,
                    "last_request_has_history": any(isinstance(message, dict)
                        and message.get("role") in ("user", "assistant") for message in messages[:current_user])
                        if current_user >= 0 else False,
                    "configured_model": selected.model,
                    "last_request_model": last.get("body", {}).get("model"),
                    "last_request_uses_current_key": bool(last) and last.get("authorization") == "Bearer " + expected_key,
                    "last_request_reasoning_effort": last.get("body", {}).get("reasoning_effort"),
                    **inspect_mock_image_payloads(api.requests)}

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
                elif action == "rotate_api" and args.direct_api:
                    settings.save_api_settings(ApiSettings(model_base_url, "isolated-rotated-model", 10, "low"))
                    settings.save_api_key("ISOLATED_ANDROID_ROTATED_MOCK_KEY_NO_REAL_SERVICE")
                    result = {"rotated": True}
                elif action == "disable_api" and args.direct_api:
                    settings.clear_api_key()
                    result = {"disabled": True}
                elif action == "enable_api" and args.direct_api:
                    settings.save_api_key("ISOLATED_ANDROID_ROTATED_MOCK_KEY_NO_REAL_SERVICE")
                    result = {"enabled": True}
                elif action == "desktop_permission" and args.desktop_assist_qa:
                    allowed = params.get("allowed")
                    if type(allowed) is not bool:
                        raise ValueError()
                    session.perception.settings = replace(session.perception.settings, allow_remote=allowed)
                    result = {"allowed": allowed}
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

        report.update(inspect())
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
            if worker_override is not None:
                worker_override.stop()
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
