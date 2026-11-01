"""Pure clock tests: no Qt, live settings, assets, or real desktop input."""

import random
import unittest

from desktop_pet.idle_behavior import DEFAULT_IDLE_ACTIONS, IdleBehaviorPolicy


class IdleBehaviorPolicyTests(unittest.TestCase):
    def policy(self, seed=11, **kwargs):
        return IdleBehaviorPolicy(random.Random(seed), **kwargs)

    def trigger_anger(self, policy):
        self.assertIsNone(policy.record_part_click("left_ear"))
        self.assertIsNone(policy.advance(0.5, idle_allowed=False))
        self.assertIsNone(policy.record_part_click("right_ear"))
        self.assertIsNone(policy.advance(0.5, idle_allowed=False))
        self.assertEqual(("angry", None), policy.record_part_click("tail"))

    def test_first_quiet_period_and_later_interval(self):
        policy = self.policy()
        first = policy.next_idle_in
        self.assertGreaterEqual(first, 4.0)
        self.assertLessEqual(first, 7.0)
        self.assertIsNone(policy.advance(first - 0.01, idle_allowed=True))
        event = policy.advance(0.02, idle_allowed=True)
        self.assertEqual("idle", event[0])
        self.assertIn(event[1], DEFAULT_IDLE_ACTIONS)
        self.assertGreaterEqual(policy.next_idle_in, 8.0)
        self.assertLessEqual(policy.next_idle_in, 16.0)

    def test_shuffle_bags_visit_every_action_and_never_repeat_across_bags(self):
        for seed in range(30):
            policy = self.policy(seed)
            count = len(DEFAULT_IDLE_ACTIONS)
            choices = [policy.advance(1000, idle_allowed=True)[1] for _ in range(count * 10)]
            self.assertTrue(all(a != b for a, b in zip(choices, choices[1:])))
            for start in range(0, len(choices), count):
                self.assertEqual(set(DEFAULT_IDLE_ACTIONS), set(choices[start:start + count]))

    def test_same_seed_reproduces_delays_and_choices(self):
        left, right = self.policy(), self.policy()
        for _ in range(20):
            self.assertEqual(left.next_idle_in, right.next_idle_in)
            self.assertEqual(left.advance(1000, idle_allowed=True),
                             right.advance(1000, idle_allowed=True))

    def test_blocked_time_does_not_accumulate_or_catch_up(self):
        policy = self.policy()
        quiet = policy.next_idle_in
        self.assertIsNone(policy.advance(quiet / 2, idle_allowed=True))
        for _ in range(5):
            self.assertIsNone(policy.advance(3600, idle_allowed=False))
        self.assertAlmostEqual(quiet / 2, policy.next_idle_in)
        self.assertIsNone(policy.advance(0, idle_allowed=True))
        self.assertEqual("idle", policy.advance(quiet / 2 + 0.01, idle_allowed=True)[0])
        remaining = policy.next_idle_in
        self.assertIsNone(policy.advance(3600, idle_allowed=False))
        self.assertEqual(remaining, policy.next_idle_in)

    def test_long_frame_returns_only_one_action_and_leaves_fresh_delay(self):
        policy = self.policy()
        self.assertEqual("idle", policy.advance(10**8, idle_allowed=True)[0])
        self.assertIsNone(policy.advance(0.1, idle_allowed=True))
        self.assertGreater(policy.next_idle_in, 7.0)

    def test_three_mixed_clicks_trigger_anger_at_inclusive_window_boundary(self):
        policy = self.policy()
        self.assertIsNone(policy.record_part_click("left_ear"))
        policy.advance(1.5, idle_allowed=False)
        self.assertIsNone(policy.record_part_click("right_ear"))
        policy.advance(1.5, idle_allowed=False)
        self.assertEqual(("angry", None), policy.record_part_click("tail"))
        self.assertTrue(policy.is_angry)
        self.assertEqual(8.0, policy.angry_remaining)

    def test_sparse_clicks_do_not_trigger_and_history_is_bounded(self):
        policy = self.policy()
        for _ in range(30):
            self.assertIsNone(policy.record_part_click("tail"))
            policy.advance(3.01, idle_allowed=False)
        self.assertFalse(policy.is_angry)
        self.assertEqual(0, len(policy._click_ages))

    def test_unsupported_parts_do_not_count_or_delay_idle(self):
        policy = self.policy()
        quiet = policy.next_idle_in
        for _ in range(5):
            self.assertIsNone(policy.record_part_click("body"))
        self.assertFalse(policy.is_angry)
        self.assertAlmostEqual(quiet, policy.next_idle_in)

    def test_more_clicks_do_not_extend_anger_or_cause_immediate_retrigger(self):
        policy = self.policy()
        self.trigger_anger(policy)
        for _ in range(7):
            self.assertIsNone(policy.advance(1.0, idle_allowed=True))
            for part in ("left_ear", "right_ear", "tail"):
                self.assertIsNone(policy.record_part_click(part))
        self.assertEqual(1.0, policy.angry_remaining)
        self.assertEqual(("calmed", None), policy.advance(1.0, idle_allowed=True))
        self.assertFalse(policy.is_angry)
        self.assertIsNone(policy.record_part_click("tail"))

    def test_anger_expires_even_while_pose_is_blocked(self):
        policy = self.policy()
        self.trigger_anger(policy)
        self.assertEqual(("calmed", None), policy.advance(1000, idle_allowed=False))
        self.assertIsNone(policy.advance(0, idle_allowed=True))
        self.assertGreaterEqual(policy.next_idle_in, 4.0)
        self.assertLessEqual(policy.next_idle_in, 7.0)
        self.assertEqual("idle", policy.advance(7.0, idle_allowed=True)[0])

    def test_activity_restarts_quiet_period_and_holds_do_not_shorten_each_other(self):
        policy = self.policy()
        policy.advance(policy.next_idle_in - 0.1, idle_allowed=True)
        policy.register_activity(hold_seconds=10)
        self.assertIsNone(policy.advance(5, idle_allowed=True))
        policy.register_activity(hold_seconds=1)
        self.assertGreaterEqual(policy.next_idle_in, 9.0)
        self.assertIsNone(policy.advance(5, idle_allowed=True))
        self.assertGreaterEqual(policy.next_idle_in, 4.0)
        self.assertLessEqual(policy.next_idle_in, 7.0)
        self.assertEqual("idle", policy.advance(7, idle_allowed=True)[0])

    def test_activity_holds_elapse_while_blocked_without_consuming_quiet(self):
        policy = self.policy()
        policy.register_activity(hold_seconds=10)
        quiet = policy.next_idle_in - 10
        self.assertIsNone(policy.advance(100, idle_allowed=False))
        self.assertAlmostEqual(quiet, policy.next_idle_in)

    def test_extreme_elapsed_time_does_not_poison_later_click_windows(self):
        policy = self.policy()
        for _ in range(3):
            policy.record_part_click("tail")
            policy.advance(1e308, idle_allowed=False)
        self.assertFalse(policy.is_angry)
        self.trigger_anger(policy)
        self.assertEqual(("calmed", None), policy.advance(1e308, idle_allowed=False))

    def test_cancel_and_reset_clear_anger_and_stale_clicks(self):
        policy = self.policy()
        self.trigger_anger(policy)
        self.assertTrue(policy.cancel_anger())
        self.assertFalse(policy.is_angry)
        self.assertIsNone(policy.advance(9, idle_allowed=False))
        self.assertFalse(policy.cancel_anger())
        policy.record_part_click("tail")
        policy.record_part_click("tail")
        policy.register_activity(hold_seconds=100)
        policy.reset()
        self.assertIsNone(policy.record_part_click("tail"))
        self.assertGreaterEqual(policy.next_idle_in, 4.0)
        self.assertLessEqual(policy.next_idle_in, 7.0)

    def test_single_available_clip_and_rejected_invalid_inputs(self):
        policy = self.policy(idle_actions=("one_clip",))
        self.assertEqual(("idle", "one_clip"), policy.advance(100, idle_allowed=True))
        self.assertEqual(("idle", "one_clip"), policy.advance(100, idle_allowed=True))
        for bad in (-1, float("nan"), float("inf"), True, "5", None):
            with self.subTest(seconds=bad):
                with self.assertRaises(ValueError):
                    policy.advance(bad, idle_allowed=True)
                with self.assertRaises(ValueError):
                    policy.register_activity(bad)
        for actions in ((), ("a", "a"), (None,), ("",)):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                self.policy(idle_actions=actions)


if __name__ == "__main__":
    unittest.main()
