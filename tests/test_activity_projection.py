"""Public DSH activity through the process, archive and renderer boundaries."""
import json
import os
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop_pet.agents.activity import COMPUTER_ACTION_LABELS, public_activity
from desktop_pet.agents.activity_subject import normalize_subject, operation_subject, subject_detail_for_label
from desktop_pet.agents.activity_text import public_commentary
from desktop_pet.agents.contracts import AgentError
from desktop_pet.agents.service import AgentService
from desktop_pet.conversations import ConversationStore
from desktop_pet.execution_trace import ExecutionTrace
from desktop_pet.ui.chat_panel import safe_execution


def frame(**values):
    return {"type": "activity", "id": "fixture-task", "event_id": "fixture-event", "kind": "tool",
            "name": "read", "status": "completed", **values}


class ActivityProjectionTests(unittest.TestCase):
    def project(self, **values):
        wire = AgentService._parse_frame(json.dumps(frame(**values)).encode(), "fixture-task")
        return public_activity(wire)

    def test_real_file_subject_survives_while_extra_payloads_do_not(self):
        value = self.project(subject={"kind": "file", "name": "report.docx"},
                             arguments={"file_path": "C:/private/report.docx"}, reasoning="PRIVATE_THOUGHT")
        self.assertEqual(value["label"], "读取文件")
        self.assertEqual(subject_detail_for_label(value["label"], value["subject"]), "文件：report.docx")
        self.assertNotIn("private", json.dumps(value))
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(operation_subject("read", {"file_path": "C:/private/report.docx"}), value["subject"])

    def test_computer_actions_are_from_a_fixed_schema_and_include_only_the_application(self):
        for action, label in COMPUTER_ACTION_LABELS.items():
            with self.subTest(action=action):
                value = self.project(kind="computer", name="cua", subject={"kind": "computer", "action": action, "name": "记事本"})
                self.assertEqual(value["label"], label)
                self.assertEqual(subject_detail_for_label(label, value["subject"]), "应用：记事本")
        value = self.project(kind="computer", name="cua", subject={"kind": "computer", "action": "run secret code"})
        self.assertEqual(value["label"], "操作电脑")
        self.assertNotIn("subject", value)

    def test_malformed_or_private_subjects_are_omitted_not_forwarded(self):
        for subject in ([], {"kind": []}, {"kind": "file", "name": []}, {"kind": "file", "name": "C:/private/x"},
                        {"kind": "file", "name": "a\u202etxt"}, {"kind": "file", "name": "safe", "reasoning": "PRIVATE"},
                        {"kind": "file", "name": "api_key=PRIVATE"}, {"kind": "files", "count": True},
                        {"kind": "computer", "action": []}):
            with self.subTest(subject=subject):
                self.assertNotIn("subject", self.project(subject=subject))
        self.assertIsNone(normalize_subject("read", {"kind": "website", "name": "example.com"}))

    def test_command_description_and_child_lifecycle_are_concrete_but_not_payloads(self):
        value = self.project(name="pwsh", subject={"kind": "command", "name": "统计临时文件大小"})
        self.assertEqual(subject_detail_for_label(value["label"], value["subject"]), "统计临时文件大小")
        value = self.project(name="subagent_activity", subject={"kind": "agent", "action": "finish"})
        self.assertEqual(value["label"], "子任务已返回")
        self.assertIsNone(subject_detail_for_label("执行命令", {"kind": "computer", "action": "input", "name": "Fake"}))

    def test_commentary_is_explicit_completed_visible_text_and_has_no_raw_fields(self):
        value = self.project(kind="commentary", name="assistant_commentary", text="已找到文件，接下来核对内容。",
                             reasoning="PRIVATE_REASONING", arguments="PRIVATE_ARGUMENTS")
        self.assertEqual(value, {"id": "fixture-event", "kind": "commentary", "status": "completed", "label": "已找到文件，接下来核对内容。"})
        for invalid in ({"name": "reasoning"}, {"status": "running"}, {"text": []}, {"text": "x" * 3001}):
            data = frame(kind="commentary", name="assistant_commentary", text="visible")
            data.update(invalid)
            with self.subTest(invalid=list(invalid)), self.assertRaises(AgentError):
                AgentService._parse_frame(json.dumps(data).encode(), "fixture-task")

    def test_commentary_hides_reasoning_credentials_paths_and_code(self):
        raw = ('<think>PRIVATE_REASONING</think>现在核对报告。https://user:PRIVATE_PASS@example.com/a?key=PRIVATE_QUERY '
               'C:/private/PRIVATE_PATH.txt api_key=PRIVATE_KEY Bearer PRIVATE_TOKEN ```PRIVATE_CODE```')
        value = public_commentary(raw)
        self.assertIn("现在核对报告", value)
        self.assertIn("example.com", value)
        self.assertNotIn("PRIVATE", value)
        self.assertEqual(public_commentary("reasoning: PRIVATE"), "")
        self.assertEqual(public_commentary("<think>PRIVATE"), "")
        self.assertEqual(public_commentary(""), "")

    def test_quoted_credentials_and_display_control_characters_do_not_survive(self):
        for text in ('password: "PRIVATE_ALPHA PRIVATE_BETA" then continue',
                     "password: 'PRIVATE_ALPHA PRIVATE_BETA' then continue", '"password": "PRIVATE_ALPHA PRIVATE_BETA"',
                     'password: "PRIVATE_ALPHA PRIVATE_BETA', 'password: "PRIVATE_ALPHA PRIVATE_BETA\nnext line'):
            with self.subTest(text=text):
                self.assertNotIn("PRIVATE_", public_commentary(text))
        self.assertEqual(public_commentary("Checking \u202efile"), "Checking file")

    def test_current_credentials_are_redacted_from_names_and_commentary(self):
        secret = "SYNTHETIC_CREDENTIAL"
        redact = lambda text: text.replace(secret, "[已隐藏密钥]")
        value = public_activity(frame(name="pwsh", subject={"kind": "command", "name": "检查 " + secret}), redact)
        self.assertNotIn(secret, json.dumps(value))
        value = public_activity(frame(kind="commentary", name="assistant_commentary", text="处理 " + secret), redact)
        self.assertNotIn(secret, json.dumps(value))

    def test_archive_reload_and_renderer_keep_public_notes_not_hidden_details(self):
        with tempfile.TemporaryDirectory(prefix="aura-activity-") as directory:
            path = Path(directory) / "archive.sqlite3"
            store = ConversationStore(path)
            conversation_id = str(uuid4())
            trace = ExecutionTrace("fixture")
            trace.event("public-note", "commentary", "已找到文件，正在检查格式。", "completed", detail="PRIVATE_DETAIL")
            trace.event("tool-1", "tool", "读取文件", "completed", detail="文件：report.docx")
            trace.event("computer-1", "computer", "查看电脑画面", "failed", detail="应用：记事本")
            value = trace.finish("failed")
            row = store.append_message(conversation_id, "assistant", "报告无法读取。", "assistant", metadata={"execution": value})
            loaded = ConversationStore(path).read_message(conversation_id, row["id"])["metadata"]["execution"]
            rendered = safe_execution(loaded)
            notes = [event for event in rendered["events"] if event["kind"] == "commentary"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0]["label"], "已找到文件，正在检查格式。")
            self.assertNotIn("PRIVATE", json.dumps(rendered))
            self.assertEqual(rendered["events"][-2]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
