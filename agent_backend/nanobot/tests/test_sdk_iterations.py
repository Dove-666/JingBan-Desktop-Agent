"""Pinned SDK adaptation checks; no model, desktop or network calls."""
import importlib.util
from itertools import count, islice
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdk_iterations import _iteration_indexes, unlimited_iterations_runner, UnlimitedIterationsUnavailable


class IterationIteratorTests(unittest.TestCase):
    def test_zero_uses_true_unbounded_iterator_and_positive_limits_remain_finite(self):
        self.assertIsInstance(_iteration_indexes(0), count)
        self.assertEqual(list(islice(_iteration_indexes(0), 100_000, 100_003)), [100_000, 100_001, 100_002])
        self.assertEqual(list(_iteration_indexes(3)), [0, 1, 2])

    def test_invalid_iteration_limits_cannot_become_unlimited(self):
        for value in (-1, True, None, "0", 0.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _iteration_indexes(value)


@unittest.skipUnless(importlib.util.find_spec("nanobot"), "run with the isolated nanobot SDK Python")
class PinnedRunnerTests(unittest.TestCase):
    def test_private_runner_keeps_sdk_method_and_module_globals_untouched(self):
        from nanobot.agent.runner import AgentRunner
        original = AgentRunner._run_core
        runner = unlimited_iterations_runner()
        self.assertIs(AgentRunner._run_core, original)
        self.assertIsNot(type(runner)._run_core, original)
        self.assertIsNot(type(runner)._run_core.__globals__, original.__globals__)
        self.assertNotIn("_aura_iteration_indexes", original.__globals__)

    def test_unknown_sdk_version_and_changed_loop_fail_without_finite_fallback(self):
        with patch("sdk_iterations.version", return_value="0.5.0"):
            with self.assertRaises(UnlimitedIterationsUnavailable):
                unlimited_iterations_runner()
        for body in (
            "async def _run_core(self, spec):\n    for iteration in range(12):\n        pass\n",
            "async def _run_core(self, spec):\n    for iteration in range(spec.max_iterations):\n        can_make_followup_request = iteration + 1 <= spec.max_iterations\n",
        ):
            with self.subTest(body=body), patch("sdk_iterations.inspect.getsource", return_value=body):
                with self.assertRaises(UnlimitedIterationsUnavailable):
                    unlimited_iterations_runner()


if __name__ == "__main__":
    unittest.main(verbosity=2)
