"""Minimal independent bridge negatives; mocked JSONL, zero native effects."""
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
from gui_verification import report

EXPECTED = "2c558e4cae6ebc69752a7c5ab39dd6ef3b8e4d613d4dcc813c27874dff74fdd2"
TRACKED = ("gui_bridge.py", "worker.py", "task_harness.py", "goal_contract.py", "execution_state.py")


def fence():
    return {p: hashlib.sha256((ROOT / "agent_backend/nanobot" / p).read_bytes()).hexdigest() for p in TRACKED}


class Input:
    def __init__(self): self.lines, self.closed = [], False
    def write(self, data): self.lines.append(json.loads(data))
    async def drain(self): pass
    def close(self): self.closed = True


class Output:
    def __init__(self, entries): self.entries = iter(entries)
    async def readline(self):
        entry = next(self.entries, b"")
        if isinstance(entry, Exception): raise entry
        return (json.dumps(entry) + "\n").encode() if isinstance(entry, dict) else entry


class Process:
    def __init__(self, entries):
        self.stdin, self.stdout = Input(), Output(entries)
        self.returncode, self.waits = None, 0
    async def wait(self): self.returncode = 0; self.waits += 1; return 0


def wire(identity):
    return "independent:" + hashlib.sha256(identity.encode()).hexdigest()[:12] if identity else "independent"


def bridge():
    return GuiBridge({"base_url": "http://127.0.0.1:1/v1", "api_key": "owned-secret", "model": "none"},
        "independent", {"max_steps": 0, "timeout_seconds": 0}, lambda *a, **k: None)


def state(value):
    return (value.pending_effect, value.effect_state, value.action_epoch, value.called,
            value.failed, value.failure_code, value.process, value.verification)


async def failure(value, identity):
    try:
        await value.run("owned parent", **({"action_id": identity} if identity else {}))
    except BridgeFailure as exc:
        return {"code": exc.code, "effect_state": exc.effect_state, "text": str(exc)}
    raise AssertionError("Expected fixed failure")


async def cache_after_fresh_observation():
    first = Process([{"id": wire("old"), "type": "error", "code": "ACTION_FAILED", "effect_state": "uncertain"}])
    fresh = Process([{"id": wire("fresh"), "type": "final", "status": "completed", "steps": 0,
        "summary": "owned read-only observation", "verification": report("verified"),
        "effect_state": "not_dispatched", "actions_dispatched": 0}])
    with patch("gui_bridge.shutil.which", return_value=sys.executable), patch(
            "gui_bridge.asyncio.create_subprocess_exec", side_effect=[first, fresh]) as spawn:
        value = bridge()
        original = await failure(value, "old")
        assert value.pending_effect and value.process is None
        await value.run("owned parent", action_id="fresh", verify_only=True)
        assert value.resolve_uncertainty(value.verification)
        assert not value.pending_effect
        clean = state(value)
        repeated_clean = await failure(value, "old")
        assert state(value) == clean and repeated_clean == original
        # A NEW failed input reuses the actual owned mocked process; the old
        # cache must not clear this current uncertainty or touch the process.
        fresh.stdout = Output([{"id": wire("new"), "type": "error", "code": "ACTION_FAILED", "effect_state": "uncertain"}])
        await failure(value, "new")
        pending = state(value)
        assert value.pending_effect and value.process is None
        repeated_pending = await failure(value, "old")
        assert state(value) == pending and repeated_pending == original
        assert spawn.await_count == 2
        assert sum(row.get("type") == "run" for row in first.stdin.lines + fresh.stdin.lines) == 3
        assert first.stdin.closed and fresh.stdin.closed and first.waits == fresh.waits == 1
        return {"passed": True, "old_failure": original, "old_cache_after_fresh_did_not_change_state": True,
                "old_cache_did_not_clear_new_pending": True, "mock_spawns": 2, "wire_runs": 3, "closed_processes": 2}


async def legacy_failure(acting):
    private_text = "OWNED_PRIVATE_EXCEPTION owned-secret C:/owned/private.txt"
    entries = ([{"id": wire(None), "type": "progress", "stage": "acting", "status": "running", "action": "click"}] if acting else [])
    entries.append(RuntimeError(private_text))
    process = Process(entries)
    with patch("gui_bridge.shutil.which", return_value=sys.executable), patch(
            "gui_bridge.asyncio.create_subprocess_exec", return_value=process) as spawn:
        value = bridge()
        first = await failure(value, None)
        snapshot = state(value)
        repeated = await failure(value, None)
        assert state(value) == snapshot and repeated == first
        assert first == {"code": "GUI_FAILED", "effect_state": "uncertain" if acting else "not_dispatched", "text": "GUI_FAILED"}
        assert value.pending_effect is acting and process.stdin.closed and process.waits == 1
        assert value.process is None and value._running_action_id is None and not value._pause_requested
        assert spawn.await_count == 1
        assert sum(row.get("type") == "run" for row in process.stdin.lines) == 1
        assert private_text not in repr(value._errors) and "owned-secret" not in repr(value._errors)
        return {"passed": True, "acting_started": acting, "failure": first, "cache": value._errors,
                "closed": True, "mock_spawns": 1, "wire_runs": 1, "private_exception_not_exposed": True}


async def audit():
    return {"cache_after_fresh_verification": await cache_after_fresh_observation(),
            "legacy_generic_after_acting": await legacy_failure(True),
            "legacy_generic_before_acting": await legacy_failure(False)}


def main():
    before = fence()
    assert before["gui_bridge.py"] == EXPECTED
    loop = asyncio.new_event_loop()
    blocked = []
    def deny(*a, **k): blocked.append("network"); raise AssertionError("NETWORK_FORBIDDEN")
    try:
        with patch.object(socket, "getaddrinfo", deny), patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny):
            result = loop.run_until_complete(audit())
    finally:
        loop.close()
    after = fence()
    assert before == after and not blocked
    output = ROOT / "artifacts/task-audit/gui-settlement-independent-after-20261106"
    output.mkdir(parents=True, exist_ok=False)
    value = {"before": before, "after": after, "source_unchanged": before == after,
        "network_attempts": blocked, "real_model_requests": 0, "real_subprocesses": 0,
        "real_gui": False, "cases": result, "passed": True}
    (output / "verification.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps(value, indent=2))


if __name__ == "__main__": main()
