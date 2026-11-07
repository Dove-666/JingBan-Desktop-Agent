"""Threaded lifetime tests for the not-yet-integrated image storage gate."""

from __future__ import annotations

import copy
import json
import threading
import unittest

from desktop_pet.image_storage import ImageStorageGate, ImageStorageGateError


class ImageStorageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ImageStorageGate()
        self.owner = self.gate.create_owner()
        self.other_owner = self.gate.create_owner()
        self.threads: list[threading.Thread] = []
        self.unblock: list[threading.Event] = []
        self.thread_errors: list[BaseException] = []

    def tearDown(self) -> None:
        for event in self.unblock:
            event.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "owned worker did not drain")
        self.assertEqual(self.thread_errors, [])

    def reject(self, code: str, operation) -> None:
        with self.assertRaises(ImageStorageGateError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def spawn(self, operation) -> threading.Thread:
        def run() -> None:
            try:
                operation()
            except BaseException as error:
                self.thread_errors.append(error)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def held(self, token):
        entered, finish = threading.Event(), threading.Event()
        self.unblock.append(finish)
        def operation():
            entered.set()
            if not finish.wait(3):
                raise AssertionError("owned work timed out")
            return "owned-result"
        thread = self.spawn(lambda: self.gate.run(token, operation))
        self.assertTrue(entered.wait(2), "owned work did not enter")
        return finish, thread

    def assert_ready(self) -> None:
        self.assertEqual(self.gate.snapshot(), {
            "activity_count": 0, "stage_count": 0,
            "maintenance_active": False, "status_code": "IMAGE_STORAGE_READY",
        })

    def test_multiple_held_activities_block_maintenance_until_both_return(self) -> None:
        first = self.gate.activity(self.owner)
        second = self.gate.activity(self.other_owner)
        finish_a, thread_a = self.held(first)
        finish_b, thread_b = self.held(second)
        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.owner))
        finish_a.set()
        thread_a.join(2)
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.owner))
        finish_b.set()
        thread_b.join(2)
        self.assert_ready()

    def test_held_maintenance_excludes_activity_and_other_maintenance(self) -> None:
        token = self.gate.maintenance(self.owner)
        finish, thread = self.held(token)
        for _ in range(3):
            self.reject("IMAGE_STORAGE_BUSY", lambda: self.gate.activity(self.other_owner))
            self.reject("IMAGE_STORAGE_BUSY", lambda: self.gate.maintenance(self.other_owner))
        finish.set()
        thread.join(2)
        self.assert_ready()

    def test_releasing_running_maintenance_cannot_admit_new_work(self) -> None:
        token = self.gate.maintenance(self.owner)
        finish, thread = self.held(token)
        self.assertFalse(self.gate.release(token))
        self.assertFalse(self.gate.release(token))
        self.reject("IMAGE_STORAGE_BUSY", lambda: self.gate.activity(self.other_owner))
        finish.set()
        thread.join(2)
        self.assertFalse(self.gate.release(token))
        self.assert_ready()

    def test_owner_close_running_activity_does_not_claim_drain(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        finish, thread = self.held(token)
        self.gate.close_owner(self.owner)
        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.other_owner))
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        finish.set()
        thread.join(2)
        self.assertIsNone(self.gate.handoff_to_stage(token))
        self.assert_ready()

    def test_owner_close_running_maintenance_retains_exclusive_blocker(self) -> None:
        token = self.gate.maintenance(self.owner)
        finish, thread = self.held(token)
        self.gate.close_owner(self.owner)
        self.reject("IMAGE_STORAGE_BUSY", lambda: self.gate.activity(self.other_owner))
        self.reject("IMAGE_STORAGE_BUSY", lambda: self.gate.maintenance(self.other_owner))
        finish.set()
        thread.join(2)
        self.assert_ready()

    def test_failed_submit_before_entry_cancels_late_runnable(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        runnable_arrived, permit_entry = threading.Event(), threading.Event()
        self.unblock.append(permit_entry)
        calls, late_codes = [], []
        def runnable():
            runnable_arrived.set()
            if not permit_entry.wait(3):
                raise AssertionError("late runnable timed out")
            try:
                self.gate.run(token, lambda: calls.append("must-not-run"))
            except ImageStorageGateError as error:
                late_codes.append(error.code)
        def failing_submit():
            self.spawn(runnable)
            self.assertTrue(runnable_arrived.wait(2))
            raise RuntimeError("synthetic submit failed before work entry")
        with self.assertRaises(RuntimeError):
            failing_submit()
        self.assertTrue(self.gate.release(token))
        maintenance = self.gate.maintenance(self.other_owner)
        permit_entry.set()
        self.threads[-1].join(2)
        self.assertEqual(calls, [])
        self.assertEqual(late_codes, ["IMAGE_STORAGE_CANCELLED"])
        self.assertTrue(self.gate.snapshot()["maintenance_active"])
        self.gate.run(maintenance, lambda: None)
        self.assertIsNone(self.gate.handoff_to_stage(token))
        self.assert_ready()

    def test_failed_submit_after_entry_keeps_blocker_until_actual_finally(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        entered, finish = threading.Event(), threading.Event()
        self.unblock.append(finish)
        calls = []
        def operation():
            calls.append("entered")
            entered.set()
            if not finish.wait(3):
                raise AssertionError("owned work timed out")
        def failing_submit():
            self.spawn(lambda: self.gate.run(token, operation))
            self.assertTrue(entered.wait(2))
            raise RuntimeError("synthetic submit failed after work entry")
        with self.assertRaises(RuntimeError):
            failing_submit()
        self.assertFalse(self.gate.release(token))
        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.other_owner))
        finish.set()
        self.threads[-1].join(2)
        self.assertEqual(calls, ["entered"])
        self.assertIsNone(self.gate.handoff_to_stage(token))
        self.assert_ready()

    def test_import_callback_gap_and_atomic_stage_handoff_never_admit_maintenance(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        self.assertEqual(self.gate.run(token, lambda: "decoded-image"), "decoded-image")
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        start, observed_after_handoff = threading.Event(), threading.Event()
        attempts, admitted = [], []
        def contender():
            if not start.wait(3):
                raise AssertionError("contender did not start")
            for _ in range(100):
                try:
                    candidate = self.gate.maintenance(self.other_owner)
                except ImageStorageGateError as error:
                    attempts.append(error.code)
                else:
                    admitted.append(candidate)
                    self.gate.release(candidate)
            observed_after_handoff.set()
        thread = self.spawn(contender)
        start.set()
        stage = self.gate.handoff_to_stage(token)
        self.assertIsNotNone(stage)
        self.assertTrue(observed_after_handoff.wait(2))
        thread.join(2)
        self.assertEqual(admitted, [])
        self.assertEqual(attempts, ["IMAGE_STORAGE_IN_USE"] * 100)
        self.assertEqual(self.gate.snapshot()["stage_count"], 1)
        self.assertFalse(self.gate.release(token))
        self.gate.release(stage)
        self.assert_ready()

    def test_old_activity_and_stage_cannot_release_successor_stage(self) -> None:
        first = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(first, lambda: None)
        old_stage = self.gate.handoff_to_stage(first)
        second = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(second, lambda: None)
        new_stage = self.gate.handoff_to_stage(second)
        self.assertTrue(self.gate.release(old_stage))
        self.assertFalse(self.gate.release(old_stage))
        self.assertFalse(self.gate.release(first))
        self.assertFalse(self.gate.release(second))
        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.other_owner))
        self.assertEqual(self.gate.snapshot()["stage_count"], 1)
        self.reject("IMAGE_STORAGE_TOKEN_USED", lambda: self.gate.handoff_to_stage(first))
        self.gate.release(new_stage)
        self.assert_ready()

    def test_close_ready_owner_prevents_late_callback_resurrection(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(token, lambda: None)
        self.gate.close_owner(self.owner)
        self.gate.close_owner(self.owner)
        self.assertIsNone(self.gate.handoff_to_stage(token))
        self.reject("IMAGE_STORAGE_OWNER_CLOSED", lambda: self.gate.activity(self.owner))
        self.reject("IMAGE_STORAGE_OWNER_CLOSED", lambda: self.gate.maintenance(self.owner))
        self.assert_ready()

    def test_close_pending_owner_prevents_actual_operation_entry(self) -> None:
        token = self.gate.activity(self.owner)
        self.gate.close_owner(self.owner)
        calls = []
        self.reject("IMAGE_STORAGE_CANCELLED", lambda: self.gate.run(token, lambda: calls.append(1)))
        self.assertEqual(calls, [])
        self.assert_ready()

    def test_owner_close_releases_stage_but_preserves_other_owner_activity(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(token, lambda: None)
        stage = self.gate.handoff_to_stage(token)
        other = self.gate.activity(self.other_owner)
        self.gate.close_owner(self.owner)
        self.assertFalse(self.gate.release(stage))
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.assertEqual(self.gate.snapshot()["stage_count"], 0)
        self.gate.release(other)
        self.assert_ready()

    def test_exception_and_base_exception_release_only_after_operation_finally(self) -> None:
        for error_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(error=error_type.__name__):
                token = self.gate.activity(self.owner, stage_on_success=True)
                inside_finally = []
                error = error_type("SYNTHETIC_PRIVATE_MESSAGE")
                def operation():
                    try:
                        raise error
                    finally:
                        inside_finally.append(self.gate.snapshot()["activity_count"])
                        self.reject("IMAGE_STORAGE_IN_USE", lambda: self.gate.maintenance(self.other_owner))
                with self.assertRaises(error_type) as caught:
                    self.gate.run(token, operation)
                self.assertIs(caught.exception, error)
                self.assertEqual(inside_finally, [1])
                self.assertIsNone(self.gate.handoff_to_stage(token))
                self.assertNotIn("SYNTHETIC", json.dumps(self.gate.snapshot()))
                self.assert_ready()

    def test_run_is_single_entry_and_returns_original_result(self) -> None:
        token = self.gate.activity(self.owner)
        result, calls = object(), []
        self.assertIs(self.gate.run(token, lambda: calls.append(1) or result), result)
        self.reject("IMAGE_STORAGE_TOKEN_USED", lambda: self.gate.run(token, lambda: calls.append(2)))
        self.assertEqual(calls, [1])
        self.assertFalse(self.gate.release(token))
        self.assert_ready()

    def test_second_run_while_first_is_held_does_not_enter(self) -> None:
        token = self.gate.activity(self.owner)
        finish, thread = self.held(token)
        calls = []
        self.reject("IMAGE_STORAGE_TOKEN_USED", lambda: self.gate.run(token, lambda: calls.append(1)))
        self.assertEqual(calls, [])
        finish.set()
        thread.join(2)
        self.assert_ready()

    def test_invalid_foreign_and_copied_tokens_never_change_ownership(self) -> None:
        token = self.gate.activity(self.owner)
        foreign_gate = ImageStorageGate()
        foreign_owner = foreign_gate.create_owner()
        foreign_token = foreign_gate.activity(foreign_owner)
        for invalid in (None, {}, [], {"token": 1}, foreign_token, copy.copy(token)):
            with self.subTest(type=type(invalid).__name__):
                self.reject("IMAGE_STORAGE_INVALID_TOKEN", lambda: self.gate.release(invalid))
                self.reject("IMAGE_STORAGE_INVALID_TOKEN", lambda: self.gate.run(invalid, lambda: None))
                self.reject("IMAGE_STORAGE_INVALID_TOKEN", lambda: self.gate.handoff_to_stage(invalid))
        for invalid in (None, {}, [], foreign_owner, copy.copy(self.owner)):
            self.reject("IMAGE_STORAGE_INVALID_OWNER", lambda: self.gate.activity(invalid))
            self.reject("IMAGE_STORAGE_INVALID_OWNER", lambda: self.gate.maintenance(invalid))
            self.reject("IMAGE_STORAGE_INVALID_OWNER", lambda: self.gate.close_owner(invalid))
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.gate.release(token)
        foreign_gate.release(foreign_token)
        self.assert_ready()

    def test_handoff_requires_successful_import_and_cannot_run_a_stage(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        self.reject("IMAGE_STORAGE_NOT_READY", lambda: self.gate.handoff_to_stage(token))
        self.gate.run(token, lambda: None)
        stage = self.gate.handoff_to_stage(token)
        self.reject("IMAGE_STORAGE_TOKEN_USED", lambda: self.gate.handoff_to_stage(token))
        self.reject("IMAGE_STORAGE_INVALID_OPERATION", lambda: self.gate.run(stage, lambda: None))
        self.reject("IMAGE_STORAGE_INVALID_OPERATION", lambda: self.gate.handoff_to_stage(stage))
        self.gate.release(stage)
        self.assert_ready()

    def test_validation_failure_keeps_pending_operation_for_explicit_cleanup(self) -> None:
        token = self.gate.activity(self.owner)
        self.reject("IMAGE_STORAGE_INVALID_OPERATION", lambda: self.gate.run(token, None))
        self.reject("IMAGE_STORAGE_INVALID_OPERATION", lambda: self.gate.activity(self.owner, stage_on_success="yes"))
        self.assertEqual(self.gate.snapshot()["activity_count"], 1)
        self.gate.release(token)
        self.assert_ready()

    def test_snapshot_and_representations_expose_only_fixed_metadata(self) -> None:
        token = self.gate.activity(self.owner, stage_on_success=True)
        self.gate.run(token, lambda: {"conversationId": "PRIVATE_CID", "image_id": "PRIVATE_IMAGE"})
        stage = self.gate.handoff_to_stage(token)
        snapshot = self.gate.snapshot()
        self.assertEqual(set(snapshot), {"activity_count", "stage_count", "maintenance_active", "status_code"})
        self.assertEqual(snapshot["status_code"], "IMAGE_STORAGE_ACTIVE")
        snapshot["stage_count"] = 0
        self.assertEqual(self.gate.snapshot()["stage_count"], 1)
        value = json.dumps(self.gate.snapshot()) + repr(self.owner) + repr(token) + repr(stage)
        self.assertNotIn("PRIVATE", value)
        self.assertNotIn("0x", value)
        self.gate.release(stage)
        self.assert_ready()


if __name__ == "__main__":
    unittest.main()
