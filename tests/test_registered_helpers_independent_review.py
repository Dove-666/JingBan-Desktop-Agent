"""Independent registered-helper review; fixed owned data and loopback only."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent_backend.nanobot.document_previews import PagePreviewCache
from agent_backend.nanobot.document_visual_review import review_document_page, DocumentVisualReviewError
from agent_backend.nanobot.safe_files import WorkspaceFiles
from tests.test_document_previews import png
from agent_backend.nanobot.acceptance_report import checked_assessment, project_assessment
from agent_backend.nanobot.tool_catalog import CATALOG
from tests import test_research_parent_worker as parent_fixture


class _FixedVisionProvider:
    def __init__(self, content):
        self.content, self.calls = content, []
        self.api_key = "SYNTHETIC_HELPER_REVIEW_KEY"
    async def chat(self, **arguments):
        self.calls.append(deepcopy(arguments))
        return SimpleNamespace(finish_reason="stop", content=self.content, tool_calls=[], reasoning_content="NEVER_PUBLISH_REASONING")


class RegisteredPagePayloadReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="registered-page-review-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.pdf = b"%PDF-1.7 owned fixed synthetic source"
        (self.root / "owned.pdf").write_bytes(self.pdf)
        self.files = WorkspaceFiles(self.root)
        self.cache = PagePreviewCache(self.files)
        self.raw = png()
        self.reference = self.cache.publish("owned.pdf", hashlib.sha256(self.pdf).hexdigest(), [self.raw], pages=1)[0]["reference"]

    async def test_exact_sent_png_complete_bare_base64_is_rejected_before_planner_observation(self):
        provider = _FixedVisionProvider(base64.b64encode(self.raw).decode("ascii"))
        with self.assertRaises(DocumentVisualReviewError) as caught:
            await review_document_page(provider, "owned-unused-model", self.cache, self.reference, "Describe the visible page.")
        self.assertEqual("DOCUMENT_REVIEW_INVALID_RESPONSE", caught.exception.code)
        self.assertEqual(1, len(provider.calls))

    async def test_whitespace_wrapped_complete_sent_image_is_rejected_without_rejecting_normal_text(self):
        encoded = base64.b64encode(self.raw).decode("ascii")
        content = "Image content:\n```\n" + " \n".join(encoded[index:index+12] for index in range(0, len(encoded), 12)) + "\n```"
        provider = _FixedVisionProvider(content)
        with self.assertRaises(DocumentVisualReviewError) as caught:
            await review_document_page(provider, "owned-unused-model", self.cache, self.reference, "Describe the page.")
        self.assertEqual("DOCUMENT_REVIEW_INVALID_RESPONSE", caught.exception.code)
        self.assertEqual(1, len(provider.calls))

    async def test_unrelated_normal_base64_text_remains_a_narrow_hash_bound_observation(self):
        text = "Visible text includes SGVsbG8=, a short example encoded word."
        provider = _FixedVisionProvider(text)
        response = json.loads(await review_document_page(provider, "owned-unused-model", self.cache, self.reference,
            "Read the visible label.", secrets=(provider.api_key,)))
        self.assertEqual(text, response["observation"])
        self.assertEqual("model_visual_observation", response["scope"])
        self.assertTrue(response["reference_only"])
        self.assertFalse(response["whole_document_verified"] or response["visual_layout_verified"])
        self.assertEqual(hashlib.sha256(self.pdf).hexdigest(), response["pdf_sha256"])
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), response["png_sha256"])
        self.assertNotIn("NEVER_PUBLISH_REASONING", json.dumps(response))
        self.assertNotIn(provider.api_key, json.dumps(response))
        self.assertEqual(1, len(provider.calls))
        request = provider.calls[0]
        self.assertEqual(2, len(request["messages"]))
        self.assertEqual([], request["tools"])
        self.assertEqual("none", request["tool_choice"])
        self.assertFalse({"thinking", "reasoning", "reasoning_effort"} & set(request))

    async def test_foreign_reference_never_reaches_vision_provider(self):
        provider = _FixedVisionProvider("No reference can be adopted.")
        with self.assertRaises(DocumentVisualReviewError):
            await review_document_page(provider, "owned-unused-model", self.cache, "page_" + "0"*32, "Read the page.")
        self.assertEqual([], provider.calls)


class RegisteredHelperCatalogReviewTests(unittest.TestCase):
    def test_only_narrow_typed_inputs_are_advertised_and_gui_permission_does_not_enable_helpers(self):
        for name in ("delegate_research", "inspect_document_page"):
            spec = CATALOG[name]
            self.assertTrue(spec.read_only)
            self.assertEqual("none", spec.effect)
            self.assertFalse(spec.parameters["additionalProperties"])
            self.assertFalse(spec.available(tools_enabled=False, computer_enabled=True, gui_configured=True, platform="win32"))
            self.assertFalse(spec.available(tools_enabled=True, computer_enabled=False, platform="linux"))
        self.assertEqual({"assignments"}, set(CATALOG["delegate_research"].parameters["properties"]))
        assignment = CATALOG["delegate_research"].parameters["properties"]["assignments"]["items"]
        self.assertEqual({"task", "paths"}, set(assignment["properties"]))
        self.assertFalse(assignment["additionalProperties"])
        self.assertEqual({"reference", "question"}, set(CATALOG["inspect_document_page"].parameters["properties"]))

    def test_unknown_cleanup_acceptance_remains_failed_and_projects_no_private_fields(self):
        report = project_assessment({"passed": False, "issues": ["RESEARCH_CLEANUP_UNCONFIRMED", "PRIVATE_SYNTHETIC"],
            "goal": {"judge_status": "satisfied", "requirements": [{"passed": True, "path": "owned/private"}]},
            "children": [{"reply": "NEVER_PUBLIC_CHILD_BODY"}]})
        self.assertEqual(report, checked_assessment(report))
        self.assertFalse(report["passed"])
        self.assertIn("RESEARCH_CLEANUP_UNCONFIRMED", report["issue_codes"])
        self.assertIn("ACCEPTANCE_CHECK_FAILED", report["issue_codes"])
        self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(report))
        self.assertNotIn("NEVER_PUBLIC_CHILD_BODY", json.dumps(report))


@unittest.skipUnless(parent_fixture.PYTHON.is_file(), "Actual isolated SDK is required")
class RegisteredParentBoundaryReviewTests(unittest.TestCase):
    setUp = parent_fixture.ResearchParentWorkerTests.setUp
    tearDown = parent_fixture.ResearchParentWorkerTests.tearDown
    start = parent_fixture.ResearchParentWorkerTests.start
    parents = staticmethod(parent_fixture.ResearchParentWorkerTests.parents)

    def test_zero_host_request_identity_gets_no_delegation_authority_but_plain_answer_still_works(self):
        def action(body):
            names = {row["function"]["name"] for row in body.get("tools", [])}
            self.assertNotIn("delegate_research", names)
            self.assertNotIn("inspect_document_page", names)
            self.assertIn("read_file", names)
            return "PLAIN_REVIEW_OK"
        client, model, _ = self.start(action, request_id="0"*32, task="Say PLAIN_REVIEW_OK.", judge_mode="answer")
        result = client.terminal(timeout=15)
        self.assertEqual("completed", result.get("status"), result)
        self.assertEqual("PLAIN_REVIEW_OK", result["reply"])
        self.assertEqual(1, len(model.planner_calls))

    def test_model_dictionary_host_identity_and_scope_fields_do_not_start_children(self):
        def action(body):
            self.assertFalse(parent_fixture.child_request(body))
            names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
            if "delegate_research" not in names:
                return "delegate_research", {"assignments": parent_fixture.ASSIGNMENTS,
                    "parent_run_id": "00000000-0000-0000-0000-000000000001", "authority_guard": True,
                    "scope": {"read": "*", "write": "*", "claims_verified": True}}
            return "PLAIN_REVIEW_OK"
        client, model, _ = self.start(action, task="Say PLAIN_REVIEW_OK.", judge_mode="answer")
        result = client.terminal(timeout=15)
        self.assertEqual("completed", result.get("status"), result)
        self.assertEqual("PLAIN_REVIEW_OK", result["reply"])
        self.assertEqual(2, len(model.planner_calls))
        self.assertTrue(all(not parent_fixture.child_request(body) for body in model.planner_calls))
        self.assertEqual({"a.txt", "b.txt"}, {path.name for path in self.workspace.iterdir()})

    def test_unknown_fault_at_finish_judge_and_delivery_boundaries_never_emits_completed(self):
        for boundary in ("finish_task", "judge_original_goal", "_run_agent_impl"):
            with self.subTest(boundary=boundary):
                def action(body):
                    if parent_fixture.child_request(body): return parent_fixture.child_step(body)
                    names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
                    if "delegate_research" not in names:
                        return "delegate_research", {"assignments": parent_fixture.ASSIGNMENTS}
                    if boundary != "judge_original_goal" and "finish_task" not in names:
                        return "finish_task", {"status": "completed", "summary": parent_fixture.ANSWER}
                    return parent_fixture.ANSWER
                wrapper = (
                    "from research_runtime import ResearchRuntime,ResearchRuntimeError\n"
                    "ready=ResearchRuntime.assert_parent_ready\nclose=ResearchRuntime.aclose\n"
                    "def checked(self):\n"
                    "    caller=sys._getframe(2).f_code.co_name\n"
                    "    if self._entries and caller==" + repr(boundary) + ": self._qa_unknown=True\n"
                    "    if getattr(self,'_qa_unknown',False): raise ResearchRuntimeError('RESEARCH_CLEANUP_UNCONFIRMED')\n"
                    "    return ready(self)\n"
                    "async def checked_close(self):\n"
                    "    value=await close(self)\n"
                    "    if getattr(self,'_qa_unknown',False): raise ResearchRuntimeError('RESEARCH_CLEANUP_UNCONFIRMED')\n"
                    "    return value\n"
                    "ResearchRuntime.assert_parent_ready=checked\nResearchRuntime.aclose=checked_close\n"
                    "asyncio.run(worker.main())\n")
                client, model, _ = self.start(action, wrapper=wrapper)
                result = client.terminal(timeout=25)
                self.assertEqual("needs_user", result.get("status"), result)
                self.assertFalse(result["acceptance"]["passed"])
                self.assertIn("RESEARCH_CLEANUP_UNCONFIRMED", result["acceptance"]["issue_codes"])
                self.assertEqual(1 if boundary == "_run_agent_impl" else 0, len(model.judge_calls))
                self.assertEqual(3 if boundary == "_run_agent_impl" else 2, len(self.parents(model)))
                self.assertFalse(any(frame.get("type") == "session" and frame.get("message", {}).get("role") == "assistant"
                    and frame.get("message", {}).get("content") == parent_fixture.ANSWER for frame in client.all_frames))

    def test_outer_cleanup_selects_failed_result_before_main_emits_and_does_not_mutate_old_completed_object(self):
        proof = self.root / "returned-result-not-mutated.json"
        wrapper = (
            "from research_runtime import ResearchRuntimeError\nfrom acceptance_report import project_assessment\n"
            "previous={'reply':'MUST_NOT_BE_DELIVERED','emotion':'neutral','status':'completed','acceptance':project_assessment({'passed':True})}\n"
            "class Unknown:\n"
            "    async def aclose(self): raise ResearchRuntimeError('RESEARCH_CLEANUP_UNCONFIRMED')\n"
            "    def assert_parent_ready(self): raise ResearchRuntimeError('RESEARCH_CLEANUP_UNCONFIRMED')\n"
            "async def impl(request):\n"
            "    worker._running_research=Unknown()\n"
            "    return previous\n"
            "worker._run_agent_impl=impl\n"
            "async def main():\n"
            "    await worker.main()\n"
            "    import pathlib,json\n"
            "    pathlib.Path(" + repr(str(proof)) + ").write_text(json.dumps({'original_completed':previous['status']=='completed','original_report_passed':previous['acceptance']['passed']}))\n"
            "asyncio.run(main())\n")
        client, model, _ = self.start(lambda _: "No model should be called", wrapper=wrapper)
        result = client.terminal(timeout=10)
        client.process.wait(timeout=8)
        self.assertEqual("needs_user", result.get("status"), result)
        self.assertFalse(result["acceptance"]["passed"])
        self.assertNotEqual("MUST_NOT_BE_DELIVERED", result["reply"])
        self.assertEqual({"original_completed": True, "original_report_passed": True}, json.loads(proof.read_text()))
        self.assertEqual([], model.planner_calls)
        self.assertEqual([], model.judge_calls)


if __name__ == "__main__": unittest.main()
