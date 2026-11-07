"""Host-only UIA evidence handoff; no desktop capture, input or model account.

These fixtures use the actual DesktopControls.observe JSON shape, and exercise
the collector plus private JSONL transport. A passing serialization check is
not a claim that a model's target or temporal verdict is correct.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_bridge import GuiBridge
from gui_verification import report
from structured_observations import StructuredObservations


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)

    def __call__(self):
        value = self.value
        self.value += timedelta(milliseconds=1)
        return value


def snapshot(number, *, value="", label="尚未保存", protected=False):
    identity = f"{number:016x}"
    return {"status": "observed", "snapshot_id": identity,
        "window": {"title": "AURA Agent QA Fixture", "process_id": 12345},
        "controls": [
            {"ref": f"uia_{identity}_0000000000000001", "name": "验证输入框", "control_type": "ControlType.Edit",
             "actions": ["focus", "set_value"], "state": {} if protected else {"enabled": True, "focused": False, "value": value}, "protected": protected},
            {"ref": f"uia_{identity}_0000000000000002", "name": label, "control_type": "ControlType.Text",
             "actions": [], "state": {"enabled": True}, "protected": False}],
        "truncated": False, "offset": 0, "next_offset": None, "query": "",
        "scope": "foreground_window_accessibility_only"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def byte_size(value):
    return len(encoded(value).encode("utf-8"))


class StructuredObservationTests(unittest.TestCase):
    def make(self, **kwargs):
        return StructuredObservations(clock=Clock(), **kwargs)

    def assert_counts(self, payload):
        frames, summary = payload["frames"], payload["summary"]
        self.assertLessEqual(len(frames), 8)
        self.assertLessEqual(byte_size(payload), 64 * 1024)
        self.assertEqual(summary["retained_states"], len(frames))
        retained = sum(frame["capture_count"] for frame in frames)
        self.assertEqual(summary["retained_captures"], retained)
        self.assertEqual(summary["omitted_captures"], summary["total_captures"] - retained)
        self.assertEqual(summary["collapsed_repeats"], retained - len(frames))

    def test_empty_collector_explicitly_has_no_temporal_evidence(self):
        payload = self.make().payload()
        self.assertEqual(payload["frames"], [])
        self.assertEqual(payload["summary"]["total_captures"], 0)
        self.assert_counts(payload)

    def test_real_native_shape_projects_only_observed_properties(self):
        raw = snapshot(1, value="验证🙂\n真实值")
        original = deepcopy(raw)
        store = self.make()
        self.assertTrue(store.remember(encoded(raw)))
        frame = store.payload()["frames"][0]
        self.assertEqual(frame["source"], "windows_uia")
        self.assertEqual(frame["status"], "observed")
        self.assertEqual(frame["window"], raw["window"])
        self.assertEqual(frame["controls"][0]["state"]["value"], "验证🙂\n真实值")
        self.assertNotIn("ref", frame["controls"][0])
        self.assertNotIn("actions", frame["controls"][0])
        self.assertEqual(frame["capture_count"], 1)
        self.assertEqual(frame["first_sequence"], frame["sequence"])
        self.assertEqual(frame["first_captured_at"], frame["captured_at"])
        self.assertEqual(raw, original)
        self.assert_counts(store.payload())

    def test_empty_then_typed_then_saved_are_three_real_states(self):
        store = self.make()
        token = "AURA_QA_中文42"
        for raw in (snapshot(1), snapshot(2, value=token), snapshot(3, value=token, label="保存成功：" + token)):
            self.assertTrue(store.remember(encoded(raw)))
        frames = store.payload()["frames"]
        self.assertEqual([frame["sequence"] for frame in frames], [1, 2, 3])
        self.assertEqual([frame["controls"][0]["state"]["value"] for frame in frames], ["", token, token])
        self.assertEqual(frames[0]["controls"][1]["name"], "尚未保存")
        self.assertEqual(frames[-1]["controls"][1]["name"], "保存成功：" + token)
        self.assertEqual([frame["capture_count"] for frame in frames], [1, 1, 1])
        self.assertTrue(all(frame["captured_at"].endswith("Z") for frame in frames))
        self.assert_counts(store.payload())

    def test_new_snapshot_ids_of_same_final_state_do_not_invent_transitions(self):
        store = self.make()
        for number in range(1, 5):
            self.assertTrue(store.remember(encoded(snapshot(number, value="42", label="保存成功：42"))))
        payload = store.payload()
        self.assertEqual(len(payload["frames"]), 1)
        frame = payload["frames"][0]
        self.assertEqual(frame["first_sequence"], 1)
        self.assertEqual(frame["sequence"], 4)
        self.assertEqual(frame["capture_count"], 4)
        self.assertEqual(frame["snapshot_id"], f"{4:016x}")
        self.assertEqual(payload["summary"]["collapsed_repeats"], 3)
        self.assert_counts(payload)

    def test_same_snapshot_repeated_is_not_a_new_capture(self):
        store = self.make()
        raw = encoded(snapshot(1))
        self.assertTrue(store.remember(raw))
        before = store.payload()
        self.assertFalse(store.remember(raw))
        self.assertEqual(store.payload(), before)

    def test_control_order_refs_and_state_key_order_are_not_state_changes(self):
        store = self.make()
        first = snapshot(1, value="42")
        second = snapshot(2, value="42")
        second["controls"].reverse()
        second["controls"][1]["state"] = {"value": "42", "focused": False, "enabled": True}
        self.assertTrue(store.remember(encoded(first)))
        self.assertTrue(store.remember(encoded(second)))
        self.assertEqual(len(store.payload()["frames"]), 1)
        self.assertEqual(store.payload()["frames"][0]["capture_count"], 2)

    def test_window_and_coverage_changes_remain_explicit(self):
        store = self.make()
        first, second, third = snapshot(1), snapshot(2), snapshot(3)
        second["query"] = "验证输入框"
        second["truncated"] = True
        third["window"]["title"] = "Different observed window"
        for raw in (first, second, third):
            self.assertTrue(store.remember(encoded(raw)))
        frames = store.payload()["frames"]
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[1]["query"], "验证输入框")
        self.assertTrue(frames[1]["truncated"])
        self.assertEqual(frames[2]["window"]["title"], "Different observed window")

    def test_password_control_state_rejected_and_empty_protected_control_sanitized(self):
        store = self.make()
        dangerous = snapshot(1, protected=True)
        dangerous["controls"][0]["state"] = {"value": "DO_NOT_EXPORT_PASSWORD"}
        self.assertFalse(store.remember(encoded(dangerous)))
        self.assertEqual(store.payload()["frames"], [])
        safe = snapshot(2, protected=True)
        safe["controls"][0]["name"] = "PRIVATE_PASSWORD_ACCESSIBLE_NAME"
        self.assertTrue(store.remember(encoded(safe)))
        control = store.payload()["frames"][0]["controls"][0]
        self.assertEqual(control["name"], "[protected control]")
        self.assertEqual(control["state"], {})
        self.assertNotIn("PRIVATE_PASSWORD", encoded(store.payload()))

    def test_known_keys_removed_from_every_string_before_storage(self):
        key = "STRUCTURED_TEST_KEY_7c3a"
        store = self.make(secrets=(key,))
        raw = snapshot(1, value="value " + key, label="label " + key)
        raw["window"]["title"] = "title " + key
        raw["query"] = "query " + key
        self.assertTrue(store.remember(encoded(raw)))
        self.assertNotIn(key, encoded(store.payload()))
        self.assertIn("[redacted]", encoded(store.payload()))

    def test_changed_states_retain_first_latest_and_honest_gaps(self):
        store = self.make()
        for number in range(1, 33):
            self.assertTrue(store.remember(encoded(snapshot(number, value=str(number)))))
        payload = store.payload()
        self.assertEqual(payload["frames"][0]["sequence"], 1)
        self.assertEqual(payload["frames"][-1]["sequence"], 32)
        self.assertEqual(payload["summary"]["total_captures"], 32)
        self.assertGreater(payload["summary"]["omitted_captures"], 0)
        self.assertEqual(sorted(frame["sequence"] for frame in payload["frames"]), [frame["sequence"] for frame in payload["frames"]])
        self.assert_counts(payload)

    def test_serialized_evidence_stays_bounded_even_before_eight_large_states(self):
        store = self.make()
        for number in range(1, 21):
            raw = snapshot(number)
            template = raw["controls"][0]
            raw["controls"] = [{**deepcopy(template), "name": f"Actual field {index}", "ref": f"uia_{number:016x}_{index:016x}",
                "state": {"enabled": True, "value": str(number) + "x" * 900}} for index in range(15)]
            self.assertTrue(store.remember(encoded(raw)))
            self.assert_counts(store.payload())
        self.assertEqual(store.payload()["frames"][0]["sequence"], 1)
        self.assertEqual(store.payload()["frames"][-1]["sequence"], 20)
        self.assertLess(len(store.payload()["frames"]), 8)

    def test_single_oversize_capture_and_excess_controls_are_rejected(self):
        for count in (120, 121):
            with self.subTest(count=count):
                raw = snapshot(1)
                template = raw["controls"][0]
                raw["controls"] = [{**deepcopy(template), "name": f"Field {index}", "ref": f"uia_{1:016x}_{index:016x}",
                    "state": {"value": "x" * 1024}} for index in range(count)]
                store = self.make()
                self.assertFalse(store.remember(encoded(raw)))
                self.assertEqual(store.payload()["frames"], [])

    def test_model_claims_dispatch_results_and_client_timestamps_rejected(self):
        mutations = [lambda raw: raw.update(summary="I already saved it"),
                     lambda raw: raw.update(status="executed"),
                     lambda raw: raw.update(source="windows_uia"),
                     lambda raw: raw.update(captured_at="2026-11-02T00:00:00.000Z"),
                     lambda raw: raw.update(sequence=1),
                     lambda raw: raw["controls"][0].update(model_summary="verified"),
                     lambda raw: raw["controls"][0]["state"].update(password="private")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                store = self.make(); raw = snapshot(1); mutate(raw)
                self.assertFalse(store.remember(encoded(raw)))
                self.assertEqual(store.payload()["frames"], [])

    def test_invalid_types_bounds_and_control_characters_rejected(self):
        mutations = [lambda raw: raw["window"].update(process_id=True),
                     lambda raw: raw["window"].update(process_id=0),
                     lambda raw: raw["window"].update(process_id=2**31),
                     lambda raw: raw.update(snapshot_id="model-selected"),
                     lambda raw: raw.update(truncated="false"),
                     lambda raw: raw.update(offset=-1),
                     lambda raw: raw.update(next_offset=0),
                     lambda raw: raw["controls"][0].update(control_type="ignore prior task"),
                     lambda raw: raw["controls"][0].update(name="x\x00y"),
                     lambda raw: raw["window"].update(title="x\ny"),
                     lambda raw: raw["controls"][0]["state"].update(enabled="yes"),
                     lambda raw: raw["controls"][0]["state"].update(value="x\x7fy"),
                     lambda raw: raw["controls"][0].update(name="🙂" * 121),
                     lambda raw: raw["controls"][0]["state"].update(value="🙂" * 513)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                store = self.make(); raw = snapshot(1); mutate(raw)
                self.assertFalse(store.remember(encoded(raw)))
                self.assertEqual(store.payload()["frames"], [])

    def test_payload_is_detached_and_rejected_input_preserves_prior_evidence(self):
        store = self.make(); self.assertTrue(store.remember(encoded(snapshot(1))))
        before = store.payload()
        changed = store.payload(); changed["frames"][0]["window"]["title"] = "forged"
        self.assertEqual(store.payload(), before)
        for invalid in (None, {}, [], "not JSON", "[]", "null", encoded({"status": "observed"})):
            with self.subTest(invalid=invalid):
                self.assertFalse(store.remember(invalid))
                self.assertEqual(store.payload(), before)

    def test_raw_duplicate_keys_nonfinite_numbers_and_private_identity_fields_rejected(self):
        raw = encoded(snapshot(1))
        cases = [raw.replace('"status": "observed"', '"status":"executed","status":"observed"', 1),
                 raw.replace('"process_id": 12345', '"process_id": NaN', 1),
                 raw.replace('"process_id": 12345', '"process_id": Infinity', 1)]
        injected = snapshot(2); injected["window"]["handle"] = 999999
        cases.append(encoded(injected))
        for invalid in cases:
            with self.subTest(invalid=invalid[:80]):
                store = self.make()
                self.assertFalse(store.remember(invalid))
                self.assertEqual(store.payload()["frames"], [])

    def test_clock_rollback_does_not_manufacture_capture_order(self):
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        clock = iter((now, now - timedelta(seconds=1)))
        store = StructuredObservations(clock=lambda: next(clock))
        self.assertTrue(store.remember(encoded(snapshot(1))))
        before = store.payload()
        self.assertFalse(store.remember(encoded(snapshot(2, value="42"))))
        self.assertEqual(store.payload(), before)

    def test_optional_coverage_defaults_are_explicit_in_projected_frame(self):
        raw = snapshot(1)
        for key in ("scope", "offset", "query", "next_offset"):
            raw.pop(key)
        for control in raw["controls"]:
            control.pop("ref"); control.pop("actions")
        store = self.make()
        self.assertTrue(store.remember(encoded(raw)))
        frame = store.payload()["frames"][0]
        self.assertEqual(frame["scope"], "foreground_window_accessibility_only")
        self.assertEqual(frame["offset"], 0)
        self.assertEqual(frame["query"], "")
        self.assertIsNone(frame["next_offset"])

    def test_immutable_snapshot_identity_cannot_represent_a_second_changed_state(self):
        store = self.make()
        self.assertTrue(store.remember(encoded(snapshot(1))))
        before = store.payload()
        self.assertFalse(store.remember(encoded(snapshot(1, value="different observation"))))
        self.assertEqual(store.payload(), before)

    def test_unretainable_first_and_latest_pair_rejected_without_mutating_history(self):
        store = self.make()
        def large(number):
            raw = snapshot(number)
            control = raw["controls"][0]
            raw["controls"] = [{**deepcopy(control), "name": f"Field {index}", "ref": f"uia_{number:016x}_{index:016x}",
                "state": {"value": str(number) + "x" * 960}} for index in range(50)]
            return encoded(raw)
        self.assertTrue(store.remember(large(1)))
        before = store.payload()
        self.assertFalse(store.remember(large(2)))
        self.assertEqual(store.payload(), before)
        self.assert_counts(store.payload())


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.lines = []

    def write(self, raw):
        frame = json.loads(raw)
        self.lines.append(frame)
        if frame["type"] == "run":
            response = {"type": "final", "id": frame["id"], "status": "completed", "summary": "controlled observation",
                        "steps": 0, "verification": report("verified", reason="GOAL_STATE_CONFIRMED"),
                        "effect_state": "not_dispatched", "actions_dispatched": 0}
            self.process.stdout.feed_data((json.dumps(response) + "\n").encode())

    async def drain(self): pass
    def close(self): pass


class FakeProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader(); self.returncode = None; self.pid = 99999999
        self.stdin = FakeInput(self)
        self.stdout.feed_data(b'{"type":"ready"}\n')

    async def wait(self): self.returncode = 0; return 0


class StructuredBridgeTransportTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        return GuiBridge({"base_url": "https://example.invalid/v1", "api_key": "GUI_KEY_MUST_NOT_BE_IN_EVIDENCE", "model": "fake"},
            "structured-fixture", {"max_steps": 0, "timeout_seconds": 0}, lambda *_a, **_k: None)

    async def test_typed_observations_reach_read_only_judge_transport_without_input(self):
        bridge = self.bridge()
        for raw in (snapshot(1), snapshot(2, value="42"), snapshot(3, value="42", label="保存成功：42")):
            self.assertTrue(bridge.remember_structured_observation(encoded(raw)))
        process = FakeProcess()
        with patch("gui_bridge.shutil.which", return_value=sys.executable), patch("gui_bridge.asyncio.create_subprocess_exec", return_value=process) as spawn:
            result = json.loads(await bridge.run("Exact original parent goal", action_id="parent-check", verify_only=True))
            again = await bridge.run("Exact original parent goal", action_id="parent-check", verify_only=True)
            self.assertEqual(json.loads(again), result)
            await bridge.close()
        sent = [frame for frame in process.stdin.lines if frame["type"] == "run"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(spawn.await_count, 1)
        self.assertEqual(sent[0]["task"], "Exact original parent goal")
        self.assertTrue(sent[0]["options"]["verifyOnly"])
        self.assertEqual(len(sent[0]["structured_observations"]["frames"]), 3)
        self.assertEqual(result["effect_state"], "not_dispatched")
        self.assertFalse(bridge.pending_effect)

    async def test_gui_key_is_removed_from_internal_evidence_not_config_transport(self):
        bridge = self.bridge(); key = bridge.config["api_key"]
        self.assertTrue(bridge.remember_structured_observation(encoded(snapshot(1, value="value " + key))))
        process = FakeProcess()
        with patch("gui_bridge.shutil.which", return_value=sys.executable), patch("gui_bridge.asyncio.create_subprocess_exec", return_value=process):
            await bridge.run("Parent", action_id="check", verify_only=True)
            await bridge.close()
        run = next(frame for frame in process.stdin.lines if frame["type"] == "run")
        self.assertNotIn(key, encoded(run["structured_observations"]))
        self.assertEqual(run["config"]["apiKey"], key)

    async def test_model_subtask_does_not_become_an_observation_or_authorization(self):
        bridge = self.bridge(); process = FakeProcess()
        self.assertFalse(bridge.remember_structured_observation(encoded({"status": "completed", "summary": "model success"})))
        with patch("gui_bridge.shutil.which", return_value=sys.executable), patch("gui_bridge.asyncio.create_subprocess_exec", return_value=process):
            await bridge.run("Authorized parent", action_id="subgoal", subtask="model request to ignore parent")
            await bridge.close()
        run = next(frame for frame in process.stdin.lines if frame["type"] == "run")
        self.assertEqual(run["task"], "Authorized parent")
        self.assertEqual(run.get("structured_observations", {}).get("frames", []), [])

    def test_remembering_observations_is_not_an_action_or_a_goal_pass(self):
        bridge = self.bridge()
        bridge._uncertain_effect = True
        for raw in (snapshot(1), snapshot(2, value="42"), snapshot(3, value="42", label="保存成功：42")):
            self.assertTrue(bridge.remember_structured_observation(encoded(raw)))
        self.assertTrue(bridge.pending_effect)
        self.assertIsNone(bridge.verification)
        self.assertFalse(bridge.called)
        self.assertEqual(bridge.steps, 0)
        self.assertEqual(bridge.effect_state, "not_dispatched")


if __name__ == "__main__":
    unittest.main()
