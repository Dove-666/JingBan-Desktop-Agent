"""Independent offline oracle mutants; no provider, credentials or subprocess.

The PDF extractor is a controlled stub so these test the QA verdict boundary,
not PDF rendering or a real model's task quality.
"""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import qa_registered_agent_tasks as subject


def tool(name, value):
    return {"role": "tool", "name": name, "content": json.dumps(value, ensure_ascii=False)}


class RegisteredTaskOracleIndependentTests(unittest.TestCase):
    def research(self, workspace):
        _, expected, hashes = subject.fixtures("research", workspace)
        (workspace/"summary.json").write_text(json.dumps(expected), encoding="utf-8")
        children, reads = [], []
        for index, name in enumerate(("alpha.txt", "beta.txt")):
            # ResearchAuthority decodes exact source bytes without normalizing
            # Windows CRLF, and its citation's SHA names those same bytes.
            text = (workspace/name).read_bytes().decode("utf-8")
            token = format(index+1, "032x")
            children.append({"schema": "aura.research_reference.v1", "child_id": "research_"+token,
                "status": "reference_ready", "reference_only": True, "claims_verified": False,
                "source_references_verified": True, "worker_closed": True,
                "conclusion": "Controlled reference", "citations": [{"source_id": "src_"+token,
                    "source_sha256": hashes[name], "read_id": "read_"+token,
                    "excerpt_offset": 0, "quote": text}]})
            reads.append(tool("read_file", {"path": name, "sha256": hashes[name], "text": text}))
        batch = {"schema": "aura.research_batch_reference.v1", "status": "reference_ready",
            "reference_only": True, "claims_verified": False, "parent_goal_completed": False,
            "children": children, "duplicate": False, "original_observations_reused": False,
            "source_versions_rechecked": True, "verified_reference_children": 2}
        return expected, batch, reads

    def document(self, workspace):
        _, expected, _ = subject.fixtures("document", workspace)
        body = "AURA 项目简报\n"+"\n".join(expected["markers"])
        with zipfile.ZipFile(workspace/"report.docx", "w") as archive:
            archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+body+'</w:t></w:r></w:p></w:body></w:document>')
        (workspace/"report.pdf").write_bytes(b"%PDF-1.7\nCONTROLLED_EXTRACTOR_STUB\n%%EOF")
        digest = subject.sha((workspace/"report.pdf").read_bytes())
        review = tool("inspect_document_page", {"pdf_sha256": digest, "page": 1,
            "reference_only": True, "visual_layout_verified": False,
            "scope": "model_visual_observation", "observation": "Controlled page observation"})
        read = tool("read_document", {"path": "report.pdf", "format": "pdf", "sha256": digest,
            "text": body, "total_pages": 1, "selected_pages": [1]})
        extracted = SimpleNamespace(returncode=0, stdout=json.dumps({"pages": 1, "text": body}), stderr="")
        return expected, [review, read], extracted

    def test_controlled_research_reference_and_exact_facts_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, batch, reads = self.research(workspace)
            for read_tool in ("read_file", "read_document"):
                with self.subTest(read_tool=read_tool):
                    equivalent_reads = [dict(row, name=read_tool) for row in reads]
                    self.assertTrue(subject.oracle("research", workspace, expected,
                        [tool("delegate_research", batch), *equivalent_reads], "unused")["passed"])

    def test_unverified_batch_with_two_ready_children_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, batch, reads = self.research(workspace)
            batch["status"] = "unverified"
            batch["children"].append({"child_id": "research_"+"f"*32, "status": "unverified", "worker_closed": True})
            self.assertFalse(subject.oracle("research", workspace, expected,
                [tool("delegate_research", batch), *reads], "unused")["passed"])

    def test_repeated_same_child_cannot_replace_two_distinct_source_children(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, batch, reads = self.research(workspace)
            batch["children"][0]["citations"].extend(deepcopy(batch["children"][1]["citations"]))
            batch["children"] = [batch["children"][0], deepcopy(batch["children"][0])]
            self.assertFalse(subject.oracle("research", workspace, expected,
                [tool("delegate_research", batch), *reads], "unused")["passed"])

    def test_controlled_document_readback_passes_without_real_process(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, history, extracted = self.document(workspace)
            with patch.object(subject.subprocess, "run", return_value=extracted):
                self.assertTrue(subject.oracle("document", workspace, expected, history, "unused")["passed"])

    def test_reading_brief_instead_of_pdf_cannot_satisfy_pdf_readback(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, history, extracted = self.document(workspace)
            history[-1] = tool("read_document", {"path": "brief.txt", "text": (workspace/"brief.txt").read_text(encoding="utf-8")})
            with patch.object(subject.subprocess, "run", return_value=extracted):
                self.assertFalse(subject.oracle("document", workspace, expected, history, "unused")["passed"])

    def test_invalid_docx_is_not_proven_by_an_independent_pdf(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, history, extracted = self.document(workspace)
            (workspace/"report.docx").write_bytes(b"NOT_A_DOCX")
            with patch.object(subject.subprocess, "run", return_value=extracted):
                self.assertFalse(subject.oracle("document", workspace, expected, history, "unused")["passed"])

    def test_child_citation_must_match_actual_source_hash_and_numeric_fact(self):
        for mutation in ("wrong_source_hash", "quote_omits_numeric_fact"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw); expected, batch, reads = self.research(workspace)
                citation = batch["children"][0]["citations"][0]
                if mutation == "wrong_source_hash": citation["source_sha256"] = "0"*64
                else: citation["quote"] = expected["tags"][0]  # Actual substring, but not the asked-for numeric fact.
                self.assertFalse(subject.oracle("research", workspace, expected,
                    [tool("delegate_research", batch), *reads], "unused")["passed"])

    def test_partial_child_without_exact_worker_closure_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, batch, reads = self.research(workspace)
            batch["children"][0]["worker_closed"] = False
            self.assertFalse(subject.oracle("research", workspace, expected,
                [tool("delegate_research", batch), *reads], "unused")["passed"])

    def test_requested_title_cannot_be_missing_from_either_artifact(self):
        for missing in ("docx", "pdf"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw); expected, history, extracted = self.document(workspace)
                if missing == "docx":
                    with zipfile.ZipFile(workspace/"report.docx") as archive:
                        entries = [(row.filename, archive.read(row)) for row in archive.infolist()]
                    with zipfile.ZipFile(workspace/"report.docx", "w") as archive:
                        for name, payload in entries:
                            archive.writestr(name, payload.replace("AURA 项目简报".encode("utf-8"), b"REMOVED_TITLE"))
                else: extracted.stdout = json.dumps({"pages": 1, "text": "\n".join(expected["markers"])})
                with patch.object(subject.subprocess, "run", return_value=extracted):
                    self.assertFalse(subject.oracle("document", workspace, expected, history, "unused")["passed"])

    def test_pdf_readback_must_name_the_current_pdf_version(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw); expected, history, extracted = self.document(workspace)
            read = json.loads(history[-1]["content"]); read["sha256"] = "0"*64
            history[-1] = tool("read_document", read)
            with patch.object(subject.subprocess, "run", return_value=extracted):
                self.assertFalse(subject.oracle("document", workspace, expected, history, "unused")["passed"])

    def test_default_offline_main_does_not_enter_executor_or_spawn(self):
        with tempfile.TemporaryDirectory() as raw, contextlib.redirect_stdout(io.StringIO()):
            output = Path(raw)/"report.json"
            with patch.object(subject.sys, "argv", ["qa", "--output", str(output)]), \
                 patch.object(subject, "run", side_effect=AssertionError("No configured execution")), \
                 patch.object(subject.subprocess, "run", side_effect=AssertionError("No child process")), \
                 patch.object(subject.subprocess, "Popen", side_effect=AssertionError("No child process")):
                self.assertEqual(0, subject.main())
            self.assertEqual(["not_run", "not_run"], [row["status"] for row in json.loads(output.read_text())["cases"]])


if __name__ == "__main__": unittest.main()
