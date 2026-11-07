"""Actual SDK→registered render_document→LibreOffice fixtures, loopback model only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND / "tests"))
import test_goal_contract_worker as support
from test_harness_worker import tool
from test_worker import Client, PYTHON

RUNTIME = Path("E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime")
SCRATCH = Path("E:/Tools/AURA/render-scratch")
POPPLER = shutil.which("pdftoppm.exe")
ARTIFACTS = REPO / "artifacts/qa-libreoffice-worker"
SPEC = {"title": "AURA Actual SDK Rendering", "sections": [{"heading": "Report",
    "paragraphs": ["Actual requested paragraph.", "AURA_SDK_RENDER_75319"]}]}


@unittest.skipUnless(os.name == "nt" and PYTHON.is_file() and RUNTIME.is_dir() and POPPLER,
                     "opt-in verified Windows LibreOffice runtime missing")
class LibreOfficeWorkerTests(unittest.TestCase):
    def config(self):
        return {"engine": "libreoffice", "runtime": str(RUNTIME), "scratch_parent": str(SCRATCH),
            "pdftoppm": POPPLER}

    def workspace(self):
        root = REPO / "agent_workspace"; root.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="actual-lo-worker-", dir=root)

    def frame(self, workspace, model, *, office=True, grants=()):
        return {"type": "run", "id": "actual-lo-" + uuid4().hex, "kind": "agent",
            "conversation_id": str(uuid4()), "session_history": [], "context": "",
            "task": "Create report.docx containing Actual requested paragraph. Render it to report.pdf using LibreOffice, read the PDF and report the actual result. Do not claim visual layout verification.",
            "config": {"base_url": model.base_url, "model": "offline-lo-fixture", "api_key": support.KEY,
                **({"office_renderer": self.config()} if office else {})},
            "options": {"workspace": str(workspace), "file_grants": list(grants), "max_steps": 12,
                "timeout_seconds": 90, "tools_enabled": True, "computer_enabled": False,
                "include_activity": True, "checkpoint_required": False}}

    def terminal(self, client, *, timeout=65, grant=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = client.frames.get(timeout=max(.1, deadline - time.monotonic()))
            if frame["type"] == "interaction" and frame.get("kind") == "file_access" and grant:
                client.send({"type": "interaction_reply", "id": frame["id"], "interaction_id": frame["interaction_id"],
                    "answer": {"allowed": True, "grant": grant}})
            if frame["type"] in {"final", "error"} and frame.get("code") != "BUSY": return frame
        self.fail("No terminal frame")

    def assert_private(self, client, model):
        self.assertNotIn(support.KEY, json.dumps(client.all_frames))
        self.assertEqual([], model.errors)

    def write_report(self, name, value):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / (name + ".json")).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def test_actual_registered_tool_publishes_pdf_for_independent_reader_and_goal_judge(self):
        summary = "Created report.docx and rendered report.pdf with LibreOffice; the PDF text was read back. Visual layout was not verified."
        steps = [tool("create_document", path="report.docx", kind="docx", spec=SPEC),
            tool("render_document", path="report.docx", output_path="report.pdf"),
            tool("read_document", path="report.pdf"), tool("finish_task", status="completed", summary=summary), summary]
        with self.workspace() as owned:
            workspace = Path(owned)
            model = support.SeparateGoalModel(steps, judge_mode="optimistic", workspace=workspace)
            inspected = []
            def judge(snapshot):
                code = "from pypdf import PdfReader; import json,sys; p=PdfReader(sys.argv[1],strict=True); print(json.dumps({'pages':len(p.pages),'text':'\\n'.join(v.extract_text() or '' for v in p.pages)}))"
                read = subprocess.run([str(PYTHON), "-I", "-c", code, str(workspace / "report.pdf")],
                    capture_output=True, timeout=15, creationflags=0x08000000)
                self.assertEqual(0, read.returncode)
                result = json.loads(read.stdout)
                self.assertEqual(1, result["pages"])
                self.assertIn("Actual requested paragraph.", result["text"])
                observations = snapshot["facts"]["observed"]["observations"]
                self.assertTrue(any(item["tool"] == "render_document" for item in observations))
                inspected.append(True)
                return json.dumps({"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
                    "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                    "status": "satisfied", "evidence_ids": snapshot["evidence_ids"]})
            model.judge = judge
            client = Client()
            try:
                client.send(self.frame(workspace, model))
                result = self.terminal(client)
                self.assertEqual("final", result["type"], result)
                self.assertEqual("completed", result["status"], result)
                self.assertTrue(inspected)
                self.assertEqual(steps.__len__(), len(model.planner_calls))
                messages = [message for call in model.planner_calls for message in call["messages"] if message.get("role") == "tool"]
                render = next(json.loads(message["content"]) for message in messages if message.get("name") == "render_document")
                self.assertEqual("LibreOffice 26.8.2 headless", render["engine"])
                self.assertFalse(render["microsoft_office_rendered"])
                self.assertFalse(render["visual_layout_verified"])
                self.assertFalse(render["previews_published"])
                self.assertEqual({"report.docx", "report.pdf"}, {path.name for path in workspace.iterdir()})
                self.assert_private(client, model)
                self.write_report("positive", {"passed": True, "actual_sdk_worker": True,
                    "actual_registered_tool": True, "actual_libreoffice": True, "independent_pdf_reader": True,
                    "goal_judge_calls": len(model.judge_calls), "planner_calls": len(model.planner_calls),
                    "pages": 1, "pdf_sha256": hashlib.sha256((workspace / "report.pdf").read_bytes()).hexdigest(),
                    "cloud_model": False, "visual_layout_verified": False})
            finally:
                self.assertEqual("", client.close()); model.close()

    def test_scope_conflict_initial_and_late_grant_keeps_ordinary_file_tools_usable(self):
        grant = {"path": str(REPO), "access": "read", "directory": True}
        records = []
        for late in (False, True):
            with self.subTest(late=late), self.workspace() as owned:
                workspace = Path(owned)
                steps = [tool("write_file", path="ordinary.txt", content="HELLO"), tool("read_file", path="ordinary.txt")]
                if late: steps.append(tool("request_file_access", path=str(REPO), access="read", directory=True, reason="Read authorized repository source for this fixture"))
                steps += [tool("render_document", path="input.docx", output_path="blocked.pdf"),
                    "ordinary.txt was created and read; PDF rendering is unavailable because private renderer scope overlaps the granted repository."]
                model = support.SeparateGoalModel(steps, judge_mode="optimistic", workspace=workspace)
                client = Client()
                try:
                    request = self.frame(workspace, model, grants=[] if late else [grant])
                    request["task"] = "Create ordinary.txt containing HELLO and read it back. Optionally try rendering input.docx only if renderer isolation permits; report a limitation without changing permissions."
                    client.send(request)
                    result = self.terminal(client, grant=grant if late else None)
                    self.assertEqual("final", result["type"], result)
                    self.assertEqual("HELLO", (workspace / "ordinary.txt").read_text())
                    self.assertFalse((workspace / "blocked.pdf").exists())
                    observations = json.dumps([message for call in model.planner_calls for message in call["messages"] if message.get("role") == "tool"])
                    self.assertIn("OFFICE_ISOLATION_UNAVAILABLE", observations)
                    self.assertNotIn("INVALID_REQUEST", json.dumps(result))
                    self.assert_private(client, model)
                    records.append({"late_grant": late, "ordinary_write_read": True, "render_isolation_rejected": True,
                        "pdf_not_published": True, "request_not_rejected": True})
                finally:
                    self.assertEqual("", client.close()); model.close()
        self.write_report("scope-conflicts", {"passed": True, "records": records})

    def test_malformed_host_config_is_invalid_before_any_planner_request(self):
        for value in (None, {**self.config(), "python": "model-choice.exe"}, {**self.config(), "runtime": "relative"}):
            with self.subTest(value=value), self.workspace() as owned:
                model = support.SeparateGoalModel([], judge_mode="optimistic", workspace=Path(owned))
                client = Client()
                try:
                    request = self.frame(Path(owned), model); request["config"]["office_renderer"] = value
                    client.send(request); result = self.terminal(client, timeout=12)
                    self.assertEqual("INVALID_REQUEST", result.get("code"), result)
                    self.assertFalse(model.planner_calls)
                finally:
                    self.assertEqual("", client.close()); model.close()

    def test_absent_config_keeps_legacy_com_dispatch_without_starting_real_office(self):
        with self.workspace() as owned, tempfile.TemporaryDirectory(prefix="aura-lo-legacy-route-") as helper:
            workspace = Path(owned); wrapper = Path(helper) / "fixed_legacy_fixture.py"; marker = Path(helper) / "legacy.json"
            wrapper.write_text("import sys,runpy,json\nfrom pathlib import Path\n"
                + "sys.path.insert(0," + repr(str(BACKEND)) + ")\nimport office_render\n"
                + "async def legacy(*args,**kwargs):\n    Path(" + repr(str(marker)) + ").write_text(json.dumps({'legacy_com_dispatch':True,'real_office_started':False}))\n    error=office_render.OfficeRenderError('OFFICE_RUNTIME_UNAVAILABLE')\n    error.safe_no_publication=True\n    raise error\noffice_render.render_document=legacy\n"
                + "runpy.run_path(" + repr(str(BACKEND / "worker.py")) + ",run_name='__main__')\n", encoding="utf-8")
            model = support.SeparateGoalModel([tool("render_document", path="not-opened.docx"),
                "Office rendering was unavailable; no PDF was produced."], judge_mode="optimistic", workspace=workspace)
            client = Client(worker_path=wrapper)
            try:
                client.send(self.frame(workspace, model, office=False)); result = self.terminal(client)
                self.assertEqual("final", result["type"], result)
                self.assertTrue(json.loads(marker.read_text())["legacy_com_dispatch"])
                self.assertEqual([], list(workspace.iterdir()))
                self.assert_private(client, model)
            finally:
                self.assertEqual("", client.close()); model.close()

    def test_model_cannot_override_renderer_or_native_paths_as_tool_arguments(self):
        with self.workspace() as owned:
            workspace = Path(owned)
            model = support.SeparateGoalModel([tool("render_document", path="not-opened.docx",
                engine="com", runtime="model-selected", profile="shared"),
                "No rendering was executed because extra tool arguments were rejected."], judge_mode="optimistic", workspace=workspace)
            client = Client()
            try:
                client.send(self.frame(workspace, model)); result = self.terminal(client)
                self.assertEqual("final", result["type"], result)
                self.assertEqual([], list(workspace.iterdir()))
                observations = json.dumps([message for call in model.planner_calls for message in call["messages"] if message.get("role") == "tool"])
                self.assertIn("unexpected parameter engine", observations)
                self.assertIn("unexpected parameter runtime", observations)
                self.assertIn("unexpected parameter profile", observations)
                self.assertNotIn("OFFICE_RUNTIME_UNAVAILABLE", observations)
                self.assert_private(client, model)
            finally:
                self.assertEqual("", client.close()); model.close()

    def test_selected_missing_libreoffice_never_falls_back_to_legacy_com(self):
        with self.workspace() as owned, tempfile.TemporaryDirectory(prefix="aura-lo-no-fallback-") as helper:
            workspace = Path(owned); wrapper = Path(helper) / "fixed_no_fallback.py"; marker = Path(helper) / "forbidden-com.txt"
            wrapper.write_text("import sys,runpy\nfrom pathlib import Path\n"
                + "sys.path.insert(0," + repr(str(BACKEND)) + ")\nimport office_render\n"
                + "async def forbidden(*args,**kwargs):\n    Path(" + repr(str(marker)) + ").write_text('FORBIDDEN')\n    raise RuntimeError('COM_FALLBACK_MUST_NOT_RUN')\noffice_render.render_document=forbidden\n"
                + "runpy.run_path(" + repr(str(BACKEND / "worker.py")) + ",run_name='__main__')\n", encoding="utf-8")
            model = support.SeparateGoalModel([tool("create_document", path="report.docx", kind="docx", spec=SPEC),
                tool("render_document", path="report.docx", output_path="report.pdf"),
                "The selected LibreOffice runtime was unavailable; report.docx exists but no PDF was produced."],
                judge_mode="optimistic", workspace=workspace)
            client = Client(worker_path=wrapper)
            try:
                request = self.frame(workspace, model)
                request["config"]["office_renderer"]["runtime"] = str(Path(helper) / "missing/runtime")
                client.send(request); result = self.terminal(client)
                self.assertEqual("final", result["type"], result)
                self.assertFalse(marker.exists())
                self.assertFalse((workspace / "report.pdf").exists())
                observations = json.dumps([message for call in model.planner_calls for message in call["messages"] if message.get("role") == "tool"])
                self.assertIn("OFFICE_RUNTIME_UNAVAILABLE", observations)
                self.assert_private(client, model)
            finally:
                self.assertEqual("", client.close()); model.close()

    def test_actual_stop_after_helper_profile_started_does_not_publish_pdf(self):
        steps = [tool("create_document", path="report.docx", kind="docx", spec=SPEC),
            tool("render_document", path="report.docx", output_path="cancelled.pdf")]
        with self.workspace() as owned:
            workspace = Path(owned); baseline = set(SCRATCH.glob("libreoffice-owned-*"))
            model = support.SeparateGoalModel(steps, judge_mode="optimistic", workspace=workspace)
            client = Client()
            try:
                request = self.frame(workspace, model); client.send(request)
                deadline = time.monotonic() + 30; observed = False
                while time.monotonic() < deadline:
                    directories = set(SCRATCH.glob("libreoffice-owned-*")) - baseline
                    if any((directory / "profile/user/registrymodifications.xcu").is_file() for directory in directories):
                        observed = True; break
                    time.sleep(.01)
                self.assertTrue(observed, "No actual helper private-profile activity observed")
                client.send({"type": "cancel", "id": request["id"]})
                result = self.terminal(client)
                self.assertEqual("cancelled", result.get("status"), result)
                self.assertFalse((workspace / "cancelled.pdf").exists())
                self.assertEqual(baseline, set(SCRATCH.glob("libreoffice-owned-*")))
                self.assert_private(client, model)
                self.write_report("cancel", {"passed": True, "actual_helper_profile_started": True,
                    "status": "cancelled", "pdf_not_published": True, "scratch_baseline_restored": True,
                    "no_native_process_identity_inferred_from_pid_absence": True})
            finally:
                self.assertEqual("", client.close()); model.close()

    def test_fault_suppressed_real_cleanup_proof_blocks_next_file_tool_and_completion(self):
        steps = [tool("create_document", path="report.docx", kind="docx", spec=SPEC),
            tool("render_document", path="report.docx", output_path="unknown.pdf"),
            tool("write_file", path="must-not-write.txt", content="SHOULD_NOT_RUN"),
            tool("finish_task", status="completed", summary="Everything completed."), "Everything completed."]
        with self.workspace() as owned, tempfile.TemporaryDirectory(prefix="aura-lo-worker-fault-") as helper:
            workspace = Path(owned); wrapper = Path(helper) / "fixed_worker_fixture.py"; proof = Path(helper) / "fault.json"
            # Fixed QA code suppresses reporting only AFTER actual exact cleanup
            # succeeds. This does not leave an intentionally live native tree.
            wrapper.write_text("import sys,runpy,json\nfrom pathlib import Path\n"
                + "sys.path.insert(0," + repr(str(REPO)) + ")\n"
                + "from desktop_pet.agents import libreoffice_renderer as native\noriginal=native._cleanup\n"
                + "def suppressed(run):\n    closed=original(run)\n    if closed:\n        Path(" + repr(str(proof)) + ").write_text(json.dumps({'actual_exact_cleanup_succeeded':True,'proof_reporting_suppressed':True}))\n        return False\n    return False\nnative._cleanup=suppressed\n"
                + "runpy.run_path(" + repr(str(BACKEND / "worker.py")) + ",run_name='__main__')\n", encoding="utf-8")
            model = support.SeparateGoalModel(steps, judge_mode="optimistic", workspace=workspace)
            client = Client(worker_path=wrapper)
            try:
                client.send(self.frame(workspace, model)); result = self.terminal(client)
                self.assertEqual("final", result["type"], result)
                self.assertEqual("needs_user", result["status"], result)
                self.assertFalse((workspace / "unknown.pdf").exists())
                self.assertFalse((workspace / "must-not-write.txt").exists())
                self.assertTrue(json.loads(proof.read_text())["actual_exact_cleanup_succeeded"])
                observations = json.dumps([message for call in model.planner_calls for message in call["messages"] if message.get("role") == "tool"])
                self.assertIn("OFFICE_CLEANUP_UNCONFIRMED", observations)
                self.assertIn("ACTION_OUTCOME_UNCERTAIN", observations)
                self.assert_private(client, model)
                self.write_report("unknown-next-tool", {"passed": True, "actual_sdk_worker": True,
                    "actual_libreoffice_rendering": True, "actual_exact_cleanup_succeeded": True,
                    "fault": "suppressed_exact_cleanup_reporting", "pdf_not_published": True,
                    "next_write_file_blocked": True, "completed_rejected": True, "status": "needs_user"})
            finally:
                self.assertEqual("", client.close()); model.close()


if __name__ == "__main__": unittest.main()
