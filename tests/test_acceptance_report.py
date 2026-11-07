import json
import unittest

from agent_backend.nanobot.acceptance_report import checked_assessment, project_assessment


class AcceptanceReportTests(unittest.TestCase):
    def test_only_fixed_statuses_codes_and_counts_cross_process_boundary(self):
        value = project_assessment({"passed": False, "issues": ["SECRET_PRIVATE_TOKEN", {"secret": True}],
            "goal": {"judge_status": "unknown", "requirements": [{"passed": False,
                     "path": "private/path", "issues": ["GOAL_FILE_UNAVAILABLE"]}]},
            "artifacts": [{"passed": True, "path": "private/other"}]})
        self.assertEqual(checked_assessment(value), value)
        self.assertEqual(value["requirements_checked"], 1)
        self.assertEqual(value["artifacts_passed"], 1)
        text = json.dumps(value)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("private/", text)

    def test_content_unknown_codes_invalid_counts_and_extra_fields_are_rejected(self):
        value = project_assessment({"passed": True})
        for mutation in ({"issue_codes": ["A_PRIVATE_SECRET"]}, {"summary": "hidden"},
                         {"judge_status": ["satisfied"]}, {"version": True},
                         {"requirements_passed": 2}, {"artifacts_checked": -1}):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    checked_assessment({**value, **mutation})


if __name__ == "__main__":
    unittest.main()
