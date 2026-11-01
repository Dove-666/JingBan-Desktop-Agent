"""Bounded Windows UI Automation tools with snapshot-scoped element references.

Design references (no upstream implementation is copied): Microsoft UFO's
ControlReceiver (MIT, eae3c0a8d168c47cb10df0652e293e092eda1078), Windows-MCP
UIA patterns (94b52cfb48076cb793e710351517fa18a316cd12), and agent-desktop's
stale-ref semantics (Apache-2.0, 9eb9b296192fcb70bda25e918cd6dd66abfa3d0d).
Unlike a native method returning successfully, a refreshed property is evidence
of that property only; it is never proof of the entire user's task.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid


ERROR_CODES = frozenset({
    "DESKTOP_UNSUPPORTED", "DESKTOP_UNAVAILABLE", "DESKTOP_TIMEOUT",
    "DESKTOP_PROTOCOL", "DESKTOP_CANCELLED", "DESKTOP_STALE_REF",
    "DESKTOP_AMBIGUOUS_TARGET", "DESKTOP_UNSUPPORTED_ACTION",
    "DESKTOP_ACCESS_DENIED", "DESKTOP_PASSWORD_CONTROL",
    "DESKTOP_INVALID_ARGUMENT", "DESKTOP_POSTCONDITION_FAILED",
})
ACTIONS = frozenset({"invoke", "set_value", "check", "uncheck", "select", "focus"})
PROPERTIES = frozenset({"name", "value", "checked", "selected", "enabled", "focused"})
MAX_CONTROLS = 120
MAX_OUTPUT = 192 * 1024
_REF = re.compile(r"^uia_[a-f0-9]{16}_[a-f0-9]{16}$")


class DesktopControlError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "DESKTOP_UNAVAILABLE"
        super().__init__(self.code)


def _text(value, limit=240):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _state(node):
    if node.get("password") is True:
        return {}
    raw = node.get("state") or {}
    if not isinstance(raw, dict):
        raise DesktopControlError("DESKTOP_PROTOCOL")
    result = {}
    for name in PROPERTIES - {"name"}:
        value = raw.get(name)
        if type(value) is bool:
            result[name] = value
        elif name == "value" and isinstance(value, str) and len(value) <= 1024 and raw.get("value_truncated") is not True:
            result[name] = value
        elif name in {"checked", "selected"} and value is None:
            result[name] = None
    return result


def _identity(window, node):
    """Keep native identities private; never reconstruct one from model text."""
    if (not isinstance(window, dict) or not isinstance(node, dict)
            or type(window.get("handle")) is not int or window["handle"] <= 0
            or type(window.get("process_id")) is not int or window["process_id"] <= 0
            or not isinstance(window.get("process_started"), str) or not window["process_started"]
            or not isinstance(window.get("runtime_id"), str) or not window["runtime_id"]
            or not isinstance(node.get("runtime_id"), str) or not node["runtime_id"]
            or not isinstance(node.get("name"), str)
            or not isinstance(node.get("control_type"), str)):
        raise DesktopControlError("DESKTOP_PROTOCOL")
    return {"window": {name: window[name] for name in
            ("handle", "process_id", "process_started", "runtime_id")},
            "runtime_id": node["runtime_id"], "name": node["name"],
            "control_type": node["control_type"], "automation_id": node.get("automation_id", "")}


class PowerShellUIABackend:
    """One fixed script, private JSON stdin, no user-built shell expressions.

    A subprocess boundary can stop a stuck accessibility provider. No PowerShell
    command supplied by the model is accepted, and no screenshots are written.
    """
    def __init__(self, cancel=None, timeout_seconds=12.0):
        self.cancel = cancel or threading.Event()
        self.timeout_seconds = min(20.0, max(.1, float(timeout_seconds)))

    async def request(self, operation, **arguments):
        if os.name != "nt":
            raise DesktopControlError("DESKTOP_UNSUPPORTED")
        if self.cancel.is_set():
            raise DesktopControlError("DESKTOP_CANCELLED")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).with_name("windows_uia.ps1")
        environment = {key: value for key, value in os.environ.items()
                       if not re.search(r"API.?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT", key, re.I)}
        process = None
        reader = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-MTA",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=environment, creationflags=0x08000000)
            payload = _json({"operation": operation, **arguments}).encode("utf-8")
            reader = asyncio.create_task(process.communicate(payload))
            deadline = time.monotonic() + self.timeout_seconds
            while not reader.done():
                if self.cancel.is_set():
                    raise DesktopControlError("DESKTOP_CANCELLED")
                if time.monotonic() >= deadline:
                    raise DesktopControlError("DESKTOP_TIMEOUT")
                await asyncio.wait({reader}, timeout=.05)
            output, _ = await reader
            if self.cancel.is_set():
                raise DesktopControlError("DESKTOP_CANCELLED")
            if process.returncode != 0 or len(output) > MAX_OUTPUT:
                raise DesktopControlError("DESKTOP_PROTOCOL")
            value = json.loads(output.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise DesktopControlError("DESKTOP_PROTOCOL")
            if value.get("error"):
                raise DesktopControlError(value["error"])
            return value
        except DesktopControlError:
            raise
        except (ValueError, UnicodeError, OSError):
            raise DesktopControlError("DESKTOP_UNAVAILABLE") from None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if reader is not None and not reader.done():
                reader.cancel()
                try:
                    await reader
                except (asyncio.CancelledError, OSError):
                    pass


class DesktopControls:
    """A per-task service; constructing it does not observe or alter the screen."""
    def __init__(self, cancel=None, *, backend=None, clock=time.monotonic, max_age_seconds=60):
        self.cancel = cancel or threading.Event()
        self.backend = backend or PowerShellUIABackend(self.cancel)
        self.clock = clock
        self.max_age_seconds = max(.1, min(120.0, float(max_age_seconds)))
        self._lock = asyncio.Lock()
        self._refs = {}
        self._created = 0.0
        self._snapshot = ""
        self._mutated = False
        self._epoch = 0

    def invalidate(self):
        """Host calls this when a different executor may have altered the UI."""
        self._epoch += 1
        self._refs.clear()
        self._snapshot = ""
        self._mutated = True

    def _check_cancelled(self):
        if self.cancel.is_set():
            raise DesktopControlError("DESKTOP_CANCELLED")

    async def observe(self):
        async with self._lock:
            self._check_cancelled()
            # Even an unsuccessful refresh invalidates the previous references.
            self._refs.clear()
            self._snapshot = ""
            epoch = self._epoch
            result = await self.backend.request("snapshot", limit=MAX_CONTROLS)
            self._check_cancelled()
            if epoch != self._epoch:
                raise DesktopControlError("DESKTOP_STALE_REF")
            window, nodes = result.get("window"), result.get("nodes")
            if not isinstance(window, dict) or not isinstance(nodes, list):
                raise DesktopControlError("DESKTOP_PROTOCOL")
            snapshot = uuid.uuid4().hex[:16]
            refs, controls, seen = {}, [], set()
            for node in nodes[:MAX_CONTROLS]:
                identity = _identity(window, node)
                if identity["runtime_id"] in seen:
                    # A duplicated native identity cannot safely identify a row.
                    raise DesktopControlError("DESKTOP_AMBIGUOUS_TARGET")
                seen.add(identity["runtime_id"])
                ref = f"uia_{snapshot}_{uuid.uuid4().hex[:16]}"
                password = node.get("password") is True
                actions = node.get("actions", [])
                if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
                    raise DesktopControlError("DESKTOP_PROTOCOL")
                available = [] if password else sorted(set(actions) & ACTIONS)
                refs[ref] = {"target": identity, "password": password, "actions": available}
                controls.append({"ref": ref, "name": _text(node["name"]),
                    "control_type": _text(node["control_type"], 80),
                    "actions": available, "state": _state(node), "protected": password})
            self._refs = refs
            self._snapshot, self._created, self._mutated = snapshot, self.clock(), False
            return _json({"status": "observed", "snapshot_id": snapshot,
                "window": {"title": _text(window.get("title")), "process_id": window.get("process_id")},
                "controls": controls, "truncated": result.get("truncated") is True or len(nodes) > MAX_CONTROLS,
                "scope": "foreground_window_accessibility_only"})

    def _reference(self, ref, *, mutation=False):
        self._check_cancelled()
        if (not isinstance(ref, str) or not _REF.fullmatch(ref) or ref not in self._refs
                or self.clock() - self._created > self.max_age_seconds
                or mutation and self._mutated):
            raise DesktopControlError("DESKTOP_STALE_REF")
        item = self._refs[ref]
        if item["password"]:
            raise DesktopControlError("DESKTOP_PASSWORD_CONTROL")
        return item

    @staticmethod
    def _check_identity(item, result):
        if not isinstance(result, dict) or not isinstance(result.get("node"), dict):
            raise DesktopControlError("DESKTOP_PROTOCOL")
        actual = _identity(result.get("window"), result["node"])
        if actual != item["target"]:
            raise DesktopControlError("DESKTOP_STALE_REF")
        if result["node"].get("password") is True:
            raise DesktopControlError("DESKTOP_PASSWORD_CONTROL")
        return result["node"]

    async def act(self, ref, action, value=""):
        async with self._lock:
            if action not in ACTIONS or not isinstance(value, str) or len(value) > 1024 or "\0" in value:
                raise DesktopControlError("DESKTOP_INVALID_ARGUMENT")
            item = self._reference(ref, mutation=True)
            if action not in item["actions"]:
                raise DesktopControlError("DESKTOP_UNSUPPORTED_ACTION")
            node = self._check_identity(item, await self.backend.request("read", target=deepcopy(item["target"])))
            self._check_cancelled()
            if self._reference(ref, mutation=True) is not item:
                raise DesktopControlError("DESKTOP_STALE_REF")
            state = _state(node)
            if state.get("enabled") is not True:
                raise DesktopControlError("DESKTOP_ACCESS_DENIED")
            property_name, expected = {
                "check": ("checked", True), "uncheck": ("checked", False),
                "select": ("selected", True), "set_value": ("value", value),
                "focus": ("focused", True), "invoke": (None, None)}[action]
            if property_name and state.get(property_name) == expected:
                return _json({"status": "already_satisfied", "action": action, "dispatched": False,
                    "postcondition": {"passed": True, "property": property_name,
                                      "actual": state.get(property_name), "expected": expected},
                    "task_verified": False})
            if action in {"check", "uncheck"} and type(state.get("checked")) is not bool:
                raise DesktopControlError("DESKTOP_UNSUPPORTED_ACTION")
            # Mark before the await: an exception can mean uncertain delivery.
            # Never automatically resend an Invoke or toggle operation.
            self._mutated = True
            result = await self.backend.request("act", target=deepcopy(item["target"]), action=action, value=value)
            self._check_cancelled()
            if result.get("dispatched") is not True:
                raise DesktopControlError("DESKTOP_PROTOCOL")
            try:
                after = self._check_identity(item, await self.backend.request("read", target=deepcopy(item["target"])))
                actual = _state(after).get(property_name) if property_name else None
                passed = property_name is not None and actual == expected
            except DesktopControlError as error:
                if error.code == "DESKTOP_CANCELLED":
                    raise
                actual, passed = None, False
            self._check_cancelled()
            return _json({"status": "executed", "action": action, "dispatched": True,
                "postcondition": {"passed": passed if property_name else None,
                                  "property": property_name, "actual": actual, "expected": expected},
                "task_verified": False, "refresh_required": True})

    async def verify(self, ref, property, expected):
        async with self._lock:
            if (property not in PROPERTIES
                    or property in {"name", "value"} and (not isinstance(expected, str) or len(expected) > 1024)
                    or property not in {"name", "value"} and type(expected) is not bool):
                raise DesktopControlError("DESKTOP_INVALID_ARGUMENT")
            item = self._reference(ref)
            node = self._check_identity(item, await self.backend.request("read", target=deepcopy(item["target"])))
            self._check_cancelled()
            if self._reference(ref) is not item:
                raise DesktopControlError("DESKTOP_STALE_REF")
            actual = node.get("name") if property == "name" else _state(node).get(property)
            passed = type(actual) is type(expected) and actual == expected
            return _json({"status": "verified" if passed else "not_satisfied", "passed": passed,
                "method": "windows_uia", "property": property, "expected": expected,
                "actual": actual, "snapshot_id": self._snapshot,
                "scope": "single_control_property_only", "task_verified": False})
