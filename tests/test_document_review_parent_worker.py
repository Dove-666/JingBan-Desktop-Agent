"""Registered parent render→cached page→plain SDK vision chain; no cloud model."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_research_parent_worker as fixtures

PYTHON = fixtures.PYTHON

RUNTIME = Path("E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime")


@unittest.skipUnless(os.name == "nt" and PYTHON.is_file() and RUNTIME.is_dir() and shutil.which("pdftoppm.exe"),
                     "Actual prepared LibreOffice, SDK and Poppler required")
class DocumentReviewParentWorkerTests(unittest.TestCase):
    setUp = fixtures.ResearchParentWorkerTests.setUp
    tearDown = fixtures.ResearchParentWorkerTests.tearDown
    start = fixtures.ResearchParentWorkerTests.start

    def test_actual_parent_render_preview_vision_reference_is_not_an_image_transcript_or_goal_verdict(self):
        received = []
        state = {}
        summary = "Created report.docx and report.pdf. Read the PDF text and obtained one page observation; whole-document visual layout has not been verified."
        def action(body):
            content = body["messages"][-1].get("content")
            if isinstance(content, list) and any(row.get("type") == "image_url" for row in content):
                self.assertTrue(len(body["messages"]) == 2, "Vision request acquired parent transcript")
                self.assertFalse(body.get("tools"), "Vision request acquired action tools")
                self.assertFalse(any(key in body for key in ("reasoning", "reasoning_effort", "thinking")))
                urls = [row["image_url"]["url"] for row in content if row.get("type") == "image_url"]
                self.assertTrue(len(urls) == 1 and urls[0].startswith("data:image/png;base64,"))
                raw = base64.b64decode(urls[0].split(",", 1)[1], validate=True)
                self.assertEqual(state["metadata"]["png_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
                received.append(hashlib.sha256(raw).hexdigest())
                return "The synthetic heading and paragraph are readable."
            names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
            self.assertIn("inspect_document_page", [row["function"]["name"] for row in body["tools"]])
            self.assertFalse("data:image" in json.dumps(body), "Inline page escaped into planner context")
            if "create_document" not in names:
                return "create_document", {"path": "report.docx", "kind": "docx", "spec": {
                    "title": "AURA Parent Page QA", "sections": [{"heading": "Actual cached page", "paragraphs": ["PARENT_PAGE_FACT_67219"]}]}}
            if "render_document" not in names: return "render_document", {"path": "report.docx", "output_path": "report.pdf"}
            if "inspect_document_page" not in names:
                render = json.loads(next(row["content"] for row in body["messages"]
                    if row.get("role") == "tool" and row.get("name") == "render_document"))
                self.assertEqual("available", render.get("preview_status"), "Actual renderer did not publish a private page cache")
                self.assertEqual(1, len(render["preview_pages"]))
                state["metadata"] = render["preview_pages"][0]
                state["pdf_sha"] = render["sha256"]
                return "inspect_document_page", {"reference": state["metadata"]["reference"], "question": "Describe the heading readability."}
            if "read_document" not in names:
                result = json.loads(next(row["content"] for row in body["messages"]
                    if row.get("role") == "tool" and row.get("name") == "inspect_document_page"))
                state["observation"] = result
                return "read_document", {"path": "report.pdf"}
            if "finish_task" not in names: return "finish_task", {"status": "completed", "summary": summary}
            return summary
        client, model, _ = self.start(action,
            task="Create report.docx containing PARENT_PAGE_FACT_67219 and render report.pdf. Read the PDF and inspect the exact rendered page. Describe what was observed without claiming whole-document layout verification.",
            office_renderer={"engine": "libreoffice", "runtime": str(RUNTIME), "scratch_parent": "E:/Tools/AURA/render-scratch",
                "pdftoppm": shutil.which("pdftoppm.exe")})
        result = client.terminal(timeout=40)
        self.assertEqual("completed", result.get("status"), result)
        self.assertEqual(summary, result["reply"])
        self.assertEqual(1, len(received))
        observed = state["observation"]
        self.assertTrue(observed["reference_only"])
        self.assertFalse(observed["visual_layout_verified"] or observed["whole_document_verified"])
        self.assertEqual("model_visual_observation", observed["scope"])
        self.assertEqual(received[0], observed["png_sha256"])
        self.assertEqual(state["pdf_sha"], hashlib.sha256((self.workspace / "report.pdf").read_bytes()).hexdigest())
        actual = subprocess.run([str(PYTHON), "-I", "-c",
            "from pypdf import PdfReader;import sys;r=PdfReader(sys.argv[1],strict=True);print('\\n'.join(p.extract_text() or '' for p in r.pages))",
            str(self.workspace / "report.pdf")], capture_output=True, encoding="utf-8", timeout=10,
            creationflags=0x08000000, check=True)
        self.assertIn("PARENT_PAGE_FACT_67219", actual.stdout)
        self.assertNotIn("data:image", json.dumps(client.all_frames))
        self.assertNotIn("SYNTHETIC_PARENT_RESEARCH_PRIVATE_KEY", json.dumps(client.all_frames))
        self.assertTrue(result["acceptance"]["passed"])


if __name__ == "__main__": unittest.main()
