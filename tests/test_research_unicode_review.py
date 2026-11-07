"""Actual SDK child IPC must preserve Chinese/emoji independently of locale."""
import json
import unittest
from uuid import uuid4

from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents.research_delegation import ResearchAuthority
from tests import test_research_sdk as fixtures


class ResearchUnicodeIpcReviewTests(unittest.TestCase):
    setUp = fixtures.ResearchSdkTests.setUp
    tearDown = fixtures.ResearchSdkTests.tearDown
    make = fixtures.ResearchSdkTests.make
    specs = fixtures.ResearchSdkTests.specs

    def test_actual_child_chinese_and_emoji_reference_is_utf8_on_the_host_pipe(self):
        text = "鲸鱼娘资料 🐳：数值为 37；资料标识 AURA_UTF8_REFERENCE。"
        (self.root/"a.txt").write_bytes(text.encode("utf-8"))
        self.authority.close()
        self.authority = ResearchAuthority(WorkspaceFiles(self.root), parent_id=str(uuid4()),
            conversation_id=str(uuid4()), revision_id="unicode-owned", authorization_current=lambda: True)
        self.scope = self.authority.open_scope(["a.txt"])
        self.refs = [row["source_id"] for row in self.authority.manifest(self.scope)]
        def action(body):
            initial = fixtures.ResearchSdkTests.initial(body)
            actual = fixtures.ResearchSdkTests.actual_read(body)
            if actual is None: return "read_source", {"source_id": initial["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "已读取鲸鱼娘资料 🐳，数值是 37。", "citations": [
                {"read_id": actual["read_id"], "quote": actual["text"]}]}, ensure_ascii=False)
        group, model = self.make(action)
        values = group.run(self.scope, self.specs())
        self.assertTrue(group.cleanup_confirmed())
        self.assertEqual("reference_ready", values[0]["status"], values)
        self.assertEqual(text, values[0]["citations"][0]["quote"])
        self.assertIn("鲸鱼娘资料 🐳", values[0]["conclusion"])
        self.assertEqual(2, len(model.planner_calls))


if __name__ == "__main__": unittest.main()
