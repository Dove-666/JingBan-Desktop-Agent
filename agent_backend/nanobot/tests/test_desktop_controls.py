"""Real state transitions through an injectable UIA transport; no desktop input."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_controls import DesktopControls, DesktopControlError, PowerShellUIABackend, _state


class FakeBackend:
    def __init__(self):
        self.window = {"handle": 12, "process_id": 42, "process_started": "100",
                       "runtime_id": "window-1", "title": "Fixture"}
        self.nodes = [{"runtime_id": "control-1", "name": "Like", "control_type": "ControlType.CheckBox",
                       "automation_id": "like", "password": False,
                       "actions": ["check", "uncheck", "focus"],
                       "state": {"enabled": True, "checked": False, "focused": False}}]
        self.calls = []
        self.deliver = True
        self.disappear = False
        self.cancel_after = None
        self.failed = None

    async def request(self, operation, **arguments):
        self.calls.append((operation, deepcopy(arguments)))
        if self.failed:
            raise DesktopControlError(self.failed)
        if operation == "snapshot":
            return deepcopy({"window": self.window, "nodes": self.nodes, "truncated": False})
        if not self.nodes or self.disappear:
            raise DesktopControlError("DESKTOP_STALE_REF")
        if operation == "read":
            return deepcopy({"window": self.window, "node": self.nodes[0]})
        if operation == "act":
            if self.deliver:
                action = arguments["action"]
                mapping = {"check": ("checked", True), "uncheck": ("checked", False),
                           "focus": ("focused", True), "select": ("selected", True),
                           "set_value": ("value", arguments["value"])}
                if action in mapping:
                    prop, value = mapping[action]
                    self.nodes[0]["state"][prop] = value
            if self.cancel_after:
                self.cancel_after.set()
            return {"dispatched": True}
        raise AssertionError(operation)


class DesktopControlTests(unittest.IsolatedAsyncioTestCase):
    async def setup_control(self, **kwargs):
        backend = FakeBackend()
        service = DesktopControls(backend=backend, **kwargs)
        observed = json.loads(await service.observe())
        return backend, service, observed["controls"][0]["ref"]

    async def test_native_ids_are_private_and_refs_change_after_refresh(self):
        backend, service, old = await self.setup_control()
        observed = json.loads(await service.observe())
        self.assertNotEqual(old, observed["controls"][0]["ref"])
        self.assertNotIn("handle", json.dumps(observed))
        self.assertNotIn("runtime_id", json.dumps(observed))
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(old, "check")
        self.assertEqual([x[0] for x in backend.calls], ["snapshot", "snapshot"])

    async def test_check_sets_real_state_and_reads_postcondition(self):
        backend, service, ref = await self.setup_control()
        result = json.loads(await service.act(ref, "check"))
        self.assertTrue(backend.nodes[0]["state"]["checked"])
        self.assertTrue(result["postcondition"]["passed"])
        self.assertFalse(result["task_verified"])
        self.assertEqual([x[0] for x in backend.calls], ["snapshot", "read", "act", "read"])

    async def test_missed_action_never_passes_postcondition(self):
        backend, service, ref = await self.setup_control()
        backend.deliver = False
        result = json.loads(await service.act(ref, "check"))
        self.assertTrue(result["dispatched"])
        self.assertFalse(result["postcondition"]["passed"])
        self.assertFalse(result["task_verified"])
        self.assertFalse(json.loads(await service.verify(ref, "checked", True))["passed"])

    async def test_no_blind_repeat_after_mutation(self):
        backend, service, ref = await self.setup_control()
        await service.act(ref, "check")
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(ref, "uncheck")
        self.assertEqual(sum(op == "act" for op, _ in backend.calls), 1)

    async def test_idempotent_check_does_not_toggle_an_already_checked_control(self):
        backend, service, ref = await self.setup_control()
        backend.nodes[0]["state"]["checked"] = True
        result = json.loads(await service.act(ref, "check"))
        self.assertEqual(result["status"], "already_satisfied")
        self.assertFalse(result["dispatched"])
        self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_indeterminate_toggle_does_not_cycle(self):
        backend, service, ref = await self.setup_control()
        backend.nodes[0]["state"]["checked"] = None
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_UNSUPPORTED_ACTION"):
            await service.act(ref, "check")
        self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_fresh_property_read_rejects_state_changed_by_user(self):
        backend, service, ref = await self.setup_control()
        await service.act(ref, "check")
        backend.nodes[0]["state"]["checked"] = False
        verification = json.loads(await service.verify(ref, "checked", True))
        self.assertFalse(verification["passed"])
        self.assertEqual(verification["scope"], "single_control_property_only")

    async def test_handle_process_instance_window_and_control_identity_are_guarded(self):
        for kind, key in [("window", "handle"), ("window", "process_id"),
                          ("window", "process_started"), ("window", "runtime_id"),
                          ("node", "runtime_id"), ("node", "name"),
                          ("node", "control_type"), ("node", "automation_id")]:
            with self.subTest(kind=kind, key=key):
                backend, service, ref = await self.setup_control()
                target = backend.window if kind == "window" else backend.nodes[0]
                target[key] = target[key] + 1 if isinstance(target[key], int) else target[key] + "-changed"
                with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
                    await service.act(ref, "check")
                self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_password_value_is_not_exposed_or_modifiable(self):
        backend = FakeBackend()
        backend.nodes[0].update(password=True, state={"value": "sensitive-password"})
        service = DesktopControls(backend=backend)
        result = json.loads(await service.observe())
        self.assertNotIn("sensitive-password", json.dumps(result))
        self.assertEqual(result["controls"][0]["actions"], [])
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_PASSWORD_CONTROL"):
            await service.act(result["controls"][0]["ref"], "check")
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_PASSWORD_CONTROL"):
            await service.verify(result["controls"][0]["ref"], "value", "password")

    async def test_password_changed_after_snapshot_is_blocked(self):
        backend, service, ref = await self.setup_control()
        backend.nodes[0]["password"] = True
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_PASSWORD_CONTROL"):
            await service.act(ref, "check")

    async def test_disabled_control_is_not_activated(self):
        backend, service, ref = await self.setup_control()
        backend.nodes[0]["state"]["enabled"] = False
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_ACCESS_DENIED"):
            await service.act(ref, "check")

    async def test_cancellation_after_dispatch_stops_before_any_more_read(self):
        cancel = threading.Event()
        backend, service, ref = await self.setup_control(cancel=cancel)
        backend.cancel_after = cancel
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_CANCELLED"):
            await service.act(ref, "check")
        self.assertEqual([op for op, _ in backend.calls], ["snapshot", "read", "act"])

    async def test_stale_due_to_age_never_dispatches(self):
        clock = [1]
        backend, service, ref = await self.setup_control(clock=lambda: clock[0], max_age_seconds=3)
        clock[0] = 5
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(ref, "check")

    async def test_failed_refresh_invalidates_old_reference(self):
        backend, service, ref = await self.setup_control()
        backend.failed = "DESKTOP_TIMEOUT"
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_TIMEOUT"):
            await service.observe()
        backend.failed = None
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(ref, "check")

    async def test_other_executor_invalidates_all_native_refs(self):
        backend, service, ref = await self.setup_control()
        service.invalidate()
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(ref, "check")
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.verify(ref, "checked", False)
        self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_other_executor_during_native_precheck_prevents_dispatch(self):
        backend, service, ref = await self.setup_control()
        original = backend.request

        async def request(operation, **arguments):
            result = await original(operation, **arguments)
            if operation == "read":
                service.invalidate()
            return result

        backend.request = request
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_STALE_REF"):
            await service.act(ref, "check")
        self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_duplicate_runtime_identity_is_not_resolved_by_name_guessing(self):
        backend = FakeBackend()
        backend.nodes.append(deepcopy(backend.nodes[0]))
        service = DesktopControls(backend=backend)
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_AMBIGUOUS_TARGET"):
            await service.observe()

    async def test_tree_is_bounded(self):
        backend = FakeBackend()
        base = backend.nodes[0]
        backend.nodes = [{**base, "runtime_id": f"control-{i}", "name": "x" * 600} for i in range(200)]
        service = DesktopControls(backend=backend)
        result = json.loads(await service.observe())
        self.assertEqual(len(result["controls"]), 120)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["controls"][0]["name"]), 240)

    async def test_no_coordinate_fallback_for_unsupported_pattern(self):
        backend, service, ref = await self.setup_control()
        with self.assertRaisesRegex(DesktopControlError, "DESKTOP_UNSUPPORTED_ACTION"):
            await service.act(ref, "invoke")
        self.assertFalse(any(op == "act" for op, _ in backend.calls))

    async def test_invoke_delivery_is_not_an_observed_goal(self):
        backend = FakeBackend()
        backend.nodes[0]["actions"] = ["invoke"]
        service = DesktopControls(backend=backend)
        ref = json.loads(await service.observe())["controls"][0]["ref"]
        result = json.loads(await service.act(ref, "invoke"))
        self.assertIsNone(result["postcondition"]["passed"])
        self.assertFalse(result["task_verified"])

    async def test_value_and_selection_patterns(self):
        for action, prop, value in [("set_value", "value", "鲸鱼娘 $(Get-Process) `test`"), ("select", "selected", True)]:
            backend = FakeBackend()
            backend.nodes[0]["actions"] = [action]
            backend.nodes[0]["state"][prop] = "" if prop == "value" else False
            service = DesktopControls(backend=backend)
            ref = json.loads(await service.observe())["controls"][0]["ref"]
            result = json.loads(await service.act(ref, action, value if isinstance(value, str) else ""))
            self.assertTrue(result["postcondition"]["passed"])
            self.assertEqual(backend.nodes[0]["state"][prop], value)

    async def test_long_truncated_value_cannot_falsely_pass_exact_verification(self):
        backend, service, ref = await self.setup_control()
        backend.nodes[0]["state"].update(value="x" * 1024, value_truncated=True)
        result = json.loads(await service.verify(ref, "value", "x" * 1024))
        self.assertFalse(result["passed"])

    async def test_verification_checks_boolean_types_and_no_model_supplied_pass(self):
        backend, service, ref = await self.setup_control()
        for prop, value in [("checked", "true"), ("checked", 1), ("passed", True), ("name", True)]:
            with self.subTest(prop=prop, value=value), self.assertRaisesRegex(DesktopControlError, "DESKTOP_INVALID_ARGUMENT"):
                await service.verify(ref, prop, value)

    async def test_unsupported_platform_fails_without_process(self):
        with patch("desktop_controls.os.name", "posix"), patch("desktop_controls.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(DesktopControlError, "DESKTOP_UNSUPPORTED"):
                await PowerShellUIABackend().request("snapshot")
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
