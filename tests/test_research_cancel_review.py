"""Independent repeated-cancel review using a real SDK child and owned HTTP."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_research_sdk as fixtures


class ResearchRepeatedCancelReviewTests(unittest.TestCase):
    setUp = fixtures.ResearchSdkTests.setUp
    tearDown = fixtures.ResearchSdkTests.tearDown
    make = fixtures.ResearchSdkTests.make
    specs = fixtures.ResearchSdkTests.specs

    def test_second_cancel_cannot_finish_parent_before_exact_child_cleanup_joins(self):
        entered, release_model = threading.Event(), threading.Event()
        cleanup_entered, release_cleanup = threading.Event(), threading.Event()
        def action(_):
            entered.set(); release_model.wait(10)
            return "Owned delayed provider response"
        group, model = self.make(action)
        settle = group._settle_owned
        def delayed_settle(run):
            cleanup_entered.set()
            if not release_cleanup.wait(5): return False
            return settle(run)

        async def exercise():
            task = asyncio.create_task(group.arun(self.scope, self.specs()[:1]))
            returned_early = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 8))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(cleanup_entered.wait, 3))
                task.cancel()
                await asyncio.sleep(.05)
                returned_early = task.done()
            finally:
                release_cleanup.set(); release_model.set()
                try: await asyncio.shield(task)
                except asyncio.CancelledError: pass
                for _ in range(150):
                    if group.cleanup_confirmed(): break
                    await asyncio.sleep(.02)
                self.assertTrue(group.cleanup_confirmed(), "Retained exact SDK child must be closed")
            self.assertFalse(returned_early, "Second cancel returned while exact child cleanup was still blocked")

        with patch.object(group, "_settle_owned", side_effect=delayed_settle):
            asyncio.run(exercise())
        self.assertEqual(1, len(model.planner_calls))


if __name__ == "__main__": unittest.main()
