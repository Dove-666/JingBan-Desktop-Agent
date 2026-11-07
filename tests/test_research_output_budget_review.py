"""Exercise the isolated child's actual SDK wire, not a mirrored serializer."""
import json
import unittest

from tests import test_research_sdk as fixtures


class ResearchOutputBudgetReviewTests(unittest.TestCase):
    setUp = fixtures.ResearchSdkTests.setUp
    tearDown = fixtures.ResearchSdkTests.tearDown
    make = fixtures.ResearchSdkTests.make
    specs = fixtures.ResearchSdkTests.specs

    def test_zero_uses_model_default_instead_of_a_one_token_wire_budget(self):
        def action(body):
            initial = fixtures.ResearchSdkTests.initial(body)
            actual = fixtures.ResearchSdkTests.actual_read(body)
            if actual is None:
                return "read_source", {"source_id": initial["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "Read the actual assigned source.", "citations": [
                {"read_id": actual["read_id"], "quote": actual["text"]}]})
        group, model = self.make(action)
        values = group.run(self.scope, self.specs()[:1])
        self.assertTrue(group.cleanup_confirmed())
        self.assertEqual("reference_ready", values[0]["status"], values)
        self.assertEqual(2, len(model.planner_calls))
        for body in model.planner_calls:
            for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                self.assertNotIn(key, body, f"The child must not turn model-default output into {key}=1")


if __name__ == "__main__": unittest.main()
