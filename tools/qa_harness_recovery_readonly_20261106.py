"""Owned, offline read-only audit of actual GuiBridge exception settlement.

No production edits, native input, real subprocesses, providers or GUI windows.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_backend/nanobot"))
from gui_bridge import BridgeFailure, GuiBridge

TRACKED = ["task_harness.py", "worker.py", "gui_bridge.py", "execution_state.py",
           "goal_contract.py", "action_intent.py"]


def fence():
    return {name: hashlib.sha256((ROOT / "agent_backend/nanobot" / name).read_bytes()).hexdigest()
            for name in TRACKED}


class Input:
    def __init__(self): self.lines = []
    def write(self, value): self.lines.append(json.loads(value))
    async def drain(self): pass
    def close(self): pass


class Process:
    def __init__(self, frames):
        self.stdin = Input()
        self.stdout = asyncio.StreamReader()
        self.returncode, self.pid = None, 99999999
        for frame in frames:
            self.stdout.feed_data((json.dumps(frame) + "\n").encode())
        self.stdout.feed_eof()
    async def wait(self): self.returncode = 0; return 0


def wire(action):
    return "audit:" + hashlib.sha256(action.encode()).hexdigest()[:12]


async def observe(name, frames):
    bridge = GuiBridge({"base_url": "http://127.0.0.1:1/v1", "api_key": "owned-fixture", "model": "none"},
        "audit", {"max_steps": 0, "timeout_seconds": 0}, lambda *a, **k: None)
    process, observations = Process(frames), []
    with patch("gui_bridge.shutil.which", return_value=sys.executable), patch(
            "gui_bridge.asyncio.create_subprocess_exec", return_value=process) as spawn:
        for attempt in (1, 2):
            try:
                await bridge.run("owned parent goal", action_id=name)
            except BridgeFailure as exc:
                observations.append({"attempt": attempt, "exception_effect": exc.effect_state,
                    "code": exc.code, "bridge_effect": bridge.effect_state,
                    "pending_effect": bridge.pending_effect, "called": bridge.called,
                    "action_epoch": bridge.action_epoch, "process_closed": bridge.process is None})
            else:
                raise AssertionError("Expected failure")
        assert spawn.await_count == 1
    return {"observations": observations, "mock_spawn_count": spawn.await_count,
            "wire_run_count": sum(row.get("type") == "run" for row in process.stdin.lines)}


async def audit():
    uncertain = await observe("uncertain", [{"id": wire("uncertain"), "type": "error",
        "code": "ACTION_FAILED", "effect_state": "uncertain", "actions_dispatched": 1}])
    assert uncertain["observations"][0]["exception_effect"] == "uncertain"
    assert uncertain["observations"][1]["exception_effect"] == "not_dispatched"
    assert all(row["pending_effect"] for row in uncertain["observations"])
    malformed = await observe("malformed", [
        {"id": wire("malformed"), "type": "progress", "stage": "acting", "status": "running", "action": "click"},
        {"id": wire("malformed"), "type": "final", "status": "completed", "steps": 1,
         "summary": "owned", "verification": {"status": "verified"}}])
    first = malformed["observations"][0]
    assert first["exception_effect"] == "not_dispatched"
    assert first["bridge_effect"] == "uncertain" and first["pending_effect"] is False
    return {"cached_uncertain_failure": uncertain, "malformed_after_acting": malformed}


def main():
    before = fence()
    # Windows asyncio creates an internal socketpair. Establish it before the
    # strict deny-all guard; the audited code cannot create outbound sockets.
    loop = asyncio.new_event_loop()
    blocked = []
    def no_network(*args, **kwargs):
        blocked.append("network")
        raise AssertionError("NETWORK_FORBIDDEN")
    try:
        with patch.object(socket, "getaddrinfo", no_network), patch.object(socket.socket, "connect", no_network), patch.object(socket.socket, "connect_ex", no_network):
            results = loop.run_until_complete(audit())
    finally:
        loop.close()
    after = fence()
    output = ROOT / "artifacts/task-audit/harness-recovery-independent-20261106"
    output.mkdir(parents=True, exist_ok=False)
    value = {"scope": "Actual GuiBridge.run/_run_once; mocked JSONL subprocess only. No worker settlement exercised.",
        "initial_fixture_failure": "First inline attempt guarded socketpair before Windows event-loop initialization; blocked before audit. No native action/provider/subprocess started.",
        "before": before, "after": after, "sources_unchanged": before == after,
        "blocked_network_attempts": blocked, "results": results}
    (output / "bridge-observations.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output / "bridge-observations.json"), **value}, indent=2))
    assert before == after and not blocked


if __name__ == "__main__":
    main()
