"""Host config→AgentService→actual SDK render tool→LibreOffice→PDF readback.

Uses generated-only documents and a localhost scripted model. This is not a
cloud-model or real-user Office fidelity benchmark. No global config is edited.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentService, AgentSettings, AgentRequest
from desktop_pet.agents import office_runtime as office
from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel, KEY
from agent_backend.nanobot.tests.test_harness_worker import tool
from agent_backend.nanobot.tests.test_worker import PYTHON

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path("E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime")
_UNKNOWN_OWNERS = []


@unittest.skipUnless(os.name == "nt" and PYTHON.is_file() and RUNTIME.is_dir() and shutil.which("pdftoppm.exe"),
                     "Actual isolated SDK and prepared LibreOffice required")
class OfficeHostRealToolTests(unittest.TestCase):
    def test_local_configuration_is_injected_without_model_renderer_parameters(self):
        parent = ROOT / "agent_workspace"; parent.mkdir(exist_ok=True)
        workspace_owner = tempfile.TemporaryDirectory(prefix="office-host-tool-", dir=parent)
        config_owner = tempfile.TemporaryDirectory(prefix="office-host-config-")
        workspace, config_root = Path(workspace_owner.name), Path(config_owner.name)
        config_path = config_root / "agent_backend/nanobot/runtime/office_renderer.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"engine": "libreoffice", "runtime": str(RUNTIME),
            "scratch_parent": "E:/Tools/AURA/render-scratch", "pdftoppm": shutil.which("pdftoppm.exe")}), encoding="utf-8")
        summary = "Created report.docx and report.pdf; read the PDF text back. Visual layout has not been verified."
        spec = {"title": "AURA Host Injection", "sections": [{"heading": "Host configuration",
            "paragraphs": ["AURA_HOST_FRAME_94271"]}]}
        model = SeparateGoalModel([tool("create_document", path="report.docx", kind="docx", spec=spec),
            tool("render_document", path="report.docx", output_path="report.pdf"),
            tool("read_document", path="report.pdf"), tool("finish_task", status="completed", summary=summary), summary],
            judge_mode="optimistic", workspace=workspace)
        service, handles, closed = AgentService(), [], False
        try:
            settings = replace(AgentSettings(), python_executable=str(PYTHON), workspace=str(workspace),
                enabled=True, tools_enabled=True, computer_enabled=False, timeout_seconds=90, max_steps=0)
            with patch.object(office, "_ROOT", config_root), patch.object(office, "_CONFIG", config_path):
                result = service.execute(settings, KEY,
                    AgentRequest("Create report.docx containing AURA_HOST_FRAME_94271. Render it to report.pdf, read the PDF back and report the actual result. Do not claim a visual layout review.", "agent"),
                    {"api": {"base_url": model.base_url, "model": "owned-host-fixture"}, "messages": [], "conversation_id": str(uuid4())},
                    worker_lifecycle_callback=handles.append)
            closed = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
            self.assertTrue(closed)
            self.assertEqual("completed", result.status, result)
            read = subprocess.run([str(PYTHON), "-I", "-c",
                "from pypdf import PdfReader;import json,sys;r=PdfReader(sys.argv[1],strict=True);print(json.dumps({'pages':len(r.pages),'text':'\\n'.join(p.extract_text() or '' for p in r.pages)}))",
                str(workspace/"report.pdf")], capture_output=True, encoding="utf-8", timeout=10,
                creationflags=0x08000000, check=True)
            actual = json.loads(read.stdout)
            self.assertEqual(1, actual["pages"])
            self.assertIn("AURA_HOST_FRAME_94271", actual["text"])
            observations = [item for call in model.planner_calls for item in call["messages"]
                if item.get("role") == "tool" and item.get("name") == "render_document"]
            self.assertTrue(observations)
            self.assertIn("LibreOffice", observations[-1]["content"])
            self.assertEqual([], model.errors)
        finally:
            service.shutdown(); model.close()
            if closed:
                workspace_owner.cleanup(); config_owner.cleanup()
            else:
                _UNKNOWN_OWNERS.append((service, workspace_owner, config_owner))


if __name__ == "__main__": unittest.main()
