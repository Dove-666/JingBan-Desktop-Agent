"""Actual SDK: unknown native file effects cannot be bypassed by another tool.

Only the owned fixture callback is replaced; registry/ledger/model loop and
WorkspaceFiles publication are the production implementations. No Office
application or real provider is invoked.
"""
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "tests"))
import test_harness_worker as fixtures


class FileEffectUncertaintyTests(unittest.TestCase):
    setUp = fixtures.HarnessWorkerTests.setUp
    tearDown = fixtures.HarnessWorkerTests.tearDown
    start = fixtures.HarnessWorkerTests.start

    def run_case(self, reported_effect):
        wrapper = self.workspace / "owned_renderer_worker.py"
        wrapper.write_text(
            "import sys,asyncio\n"
            f"sys.path.insert(0,{str(BACKEND)!r})\n"
            "import office_render,worker\n"
            "async def owned_failure(*args,**kwargs):\n"
            "    error=office_render.OfficeRenderError('OFFICE_CLEANUP_UNCONFIRMED')\n"
            f"    error.effect_state={reported_effect!r}\n"
            "    raise error\n"
            "office_render.render_document=owned_failure\n"
            "asyncio.run(worker.main())\n", encoding="utf-8")
        observations = []
        def after_write(body):
            # SDK Tool.error is a text observation; a successful write is
            # JSON. Keep both wire forms, without making the model fixture
            # fail with a 500 while trying to JSON-decode the blocked call.
            observations.append([item["content"] for item in body["messages"]
                                 if item.get("role") == "tool" and item.get("name") == "write_file"][-1])
            return "The renderer remains unresolved."
        client, model = self.start([
            fixtures.tool("render_document", path="owned.docx"),
            fixtures.tool("write_file", path="later.txt", content="MUST NOT WRITE AFTER UNKNOWN"),
            after_write], worker_path=wrapper, allow_outcome_feedback=True)
        result = client.terminal()
        self.assertEqual("needs_user", result.get("status"), result)
        self.assertEqual(3 + model.repair_calls, len(model.calls))
        return observations[0]

    @unittest.skipUnless(sys.platform == "win32", "Windows render tool registry")
    def test_explicit_unknown_file_effect_blocks_later_file_tool(self):
        observation = self.run_case("unknown")
        self.assertFalse((self.workspace / "later.txt").exists(), observation)
        self.assertIn("ACTION_OUTCOME_UNCERTAIN", str(observation))

    @unittest.skipUnless(sys.platform == "win32", "Windows render tool registry")
    def test_ordinary_failed_attempt_does_not_block_independent_file_write(self):
        observation = self.run_case(None)
        self.assertEqual("MUST NOT WRITE AFTER UNKNOWN", (self.workspace / "later.txt").read_text())
        self.assertIn("written", observation)


if __name__ == "__main__": unittest.main()
