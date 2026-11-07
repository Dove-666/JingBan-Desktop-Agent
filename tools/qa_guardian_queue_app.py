"""Controlled Qt host using real RPC/controller/service/SDK with synthetic data.

Only the supplied owned AppConfig/workspace are used. No production credential
store, network transport, model endpoint, App window or physical desktop.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

KEY = "synthetic-guardian-production-qa-key"


def output(value):
    sys.stdout.buffer.write((json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def run(arguments):
    from PySide6.QtCore import QSettings, QThreadPool
    from PySide6.QtWidgets import QApplication
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.remote.controller import RemoteController
    from desktop_pet.settings_manager import SettingsManager, CredentialStore, ApiSettings
    from tests.test_settings import FakeVault
    from tests.test_remote_controller import FakeTransport, PEER, spin_until
    from desktop_pet.agents.guardian_native import Native

    app = QApplication.instance() or QApplication([])
    config, workspace = Path(arguments.config).resolve(), Path(arguments.workspace).resolve()
    config.mkdir(parents=True, exist_ok=True); workspace.mkdir(parents=True, exist_ok=True)
    settings = SettingsManager(QSettings(str(config / "settings.ini"), QSettings.Format.IniFormat),
                               CredentialStore(backend=FakeVault()))
    settings.save_api_settings(ApiSettings(arguments.endpoint, "offline-owned-guardian", 10))
    settings.save_api_key(KEY)
    settings.save_agent_settings(replace(settings.load_agent_settings(), enabled=True, computer_enabled=False,
        python_executable=str(REPO / "agent_backend/nanobot/.venv/Scripts/python.exe"), workspace=str(workspace),
        timeout_seconds=120, max_steps=0))
    chat = ChatController(settings)
    chat._metadata_factory = None
    remote = RemoteController(SimpleNamespace(settings=settings, chat=chat), transport_factory=FakeTransport)
    native = Native()
    try:
        spin_until(lambda: not QThreadPool.globalInstance().activeThreadCount())
        remote.set_enabled(True)
        spin_until(lambda: remote.status["state"] == "listening")
        def rpc(method, params):
            value = remote.handle_request(PEER, {"id": str(uuid4()), "method": method, "params": params})
            if value.get("ok") is not True:
                raise RuntimeError("FIXTURE_RPC_FAILED")
            return value["result"]
        if not remote.preferences.authorized(PEER):
            pair = remote.begin_pairing()
            rpc("pair", {"code": pair["code"], "name": "owned QA device"})
        cid = arguments.cid or chat.current_conversation_id
        if arguments.mode == "restart":
            old = chat._agent_queue.queue.get(arguments.old_job)
            output({"phase": "restored", "old_state": old.state, "old_worker_closed": old.worker_closed_at is not None,
                "old_reply_absent": old.reply_id is None, "barrier": chat._agent_queue.effect_barrier()})
            task = "Create b.txt containing exactly B. Read it back before finishing."
        else:
            task = "Create a.txt containing exactly A. Read it back before finishing."
        ack = rpc("agent.send", {"conversation_id": cid, "text": task})
        queue = chat._agent_queue.queue
        if arguments.mode == "first":
            def running():
                context = chat._agent_queue._active
                return (context is not None and context.handle is not None
                    and getattr(context.handle._process,"guardian",None) is not None
                    and context.handle._process._root.resumed)
            spin_until(running, timeout=15)
            context = chat._agent_queue._active
            worker = context.handle._process
            binding = queue.guardian_binding(ack["job_id"])
            output({"phase": "running", "job_id": ack["job_id"], "conversation_id": cid,
                "root_pid": worker.pid, "root_creation_time": native.creation_time(worker._handle),
                "guardian_pid": worker.guardian.process.pid,
                "guardian_creation_time": native.creation_time(worker.guardian.process.handle),
                "manifest_v2": worker.manifest["schema"] == "aura.host_job_manifest.v2",
                "queue_binding_matches": binding.matches_manifest(worker.manifest),
                "same_lifecycle_object": context.handle._process is worker})
            end = time.monotonic() + 60
            while time.monotonic() < end:
                QApplication.processEvents()
                time.sleep(.005)
            raise RuntimeError("FIXTURE_NOT_CRASHED_WITHIN_BOUND")
        spin_until(lambda: queue.get(ack["job_id"]).state == "completed", timeout=30)
        spin_until(lambda: not chat.busy and not QThreadPool.globalInstance().activeThreadCount(), timeout=10)
        current = queue.get(ack["job_id"])
        old = queue.get(arguments.old_job)
        binding = queue.guardian_binding(current.job_id)
        output({"phase": "completed", "new_job_different": current.job_id != old.job_id,
            "new_state": current.state, "new_worker_closed": current.worker_closed_at is not None,
            "new_bound": binding is not None, "old_state": old.state,
            "old_worker_closed": old.worker_closed_at is not None, "old_reply_absent": old.reply_id is None,
            "barrier": chat._agent_queue.effect_barrier(), "a_exists": (workspace / "a.txt").read_text() == "A",
            "b_exists": (workspace / "b.txt").read_text() == "B"})
        return 0
    finally:
        remote.shutdown(); chat.shutdown()
        spin_until(lambda: not QThreadPool.globalInstance().activeThreadCount(), timeout=10)
        remote.deleteLater(); chat.deleteLater(); QApplication.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["first", "restart"], required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cid")
    parser.add_argument("--old-job")
    args = parser.parse_args()
    raise SystemExit(run(args))
