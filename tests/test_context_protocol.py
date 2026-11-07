"""Archive-preserving context exchange; no provider or personal files needed."""
import json
import unittest
from uuid import uuid4

from desktop_pet.conversations.context_protocol import build_context_envelope, compact_provider_messages


class ContextProtocolTests(unittest.TestCase):
    def history(self, count=8):
        cid = str(uuid4())
        return cid, [{"id": str(uuid4()), "conversation_id": cid,
                      "role": "user" if i % 2 == 0 else "assistant",
                      "content": f"record-{i}:" + "鲸鱼" * 500, "metadata": {}} for i in range(count)]

    def test_bounded_working_set_keeps_provenance_and_archive_unmodified(self):
        cid, rows = self.history()
        before = json.dumps(rows)
        envelope = build_context_envelope(cid, rows, target_bytes=4096)
        self.assertTrue(envelope["reference_data_only"])
        self.assertFalse(envelope["summary"]["verified"])
        self.assertEqual(envelope["generation"], rows[0]["id"])
        self.assertEqual(envelope["through_message_id"], rows[-1]["id"])
        self.assertEqual(envelope["recent_message_ids"], [rows[-1]["id"]])
        self.assertEqual(envelope["omitted_count"], 7)
        self.assertIn("未覆盖全部旧信息", envelope["summary"]["text"])
        self.assertEqual(before, json.dumps(rows))
        self.assertNotIn("recent_messages", envelope)

    def test_scope_change_or_source_edit_changes_revision(self):
        cid, rows = self.history(2)
        original = build_context_envelope(cid, rows)
        rows[0]["content"] += "corrected"
        self.assertNotEqual(original["revision"], build_context_envelope(cid, rows)["revision"])
        rows[1]["conversation_id"] = str(uuid4())
        with self.assertRaises(ValueError):
            build_context_envelope(cid, rows)

    def test_incomplete_records_cannot_become_completed_model_memory(self):
        cid, rows = self.history(2)
        rows[-1]["metadata"]["request_status"] = "interrupted"
        envelope = build_context_envelope(cid, rows)
        self.assertNotIn(rows[-1]["id"], envelope["recent_message_ids"])
        self.assertEqual(envelope["through_message_id"], rows[-1]["id"])

    def test_future_queued_task_is_not_a_recalled_or_summarized_completed_turn(self):
        cid, rows = self.history(3)
        rows[-1].update(content="FUTURE_QUEUED_INSTRUCTION", metadata={"request_status": "queued"})
        envelope = build_context_envelope(cid, rows, target_bytes=1024)
        self.assertNotIn(rows[-1]["id"], envelope["recent_message_ids"])
        self.assertNotIn("FUTURE_QUEUED_INSTRUCTION", json.dumps(envelope))
        self.assertEqual(envelope["through_message_id"], rows[-1]["id"])

    def test_expired_revoked_and_secret_memories_do_not_leak(self):
        cid, rows = self.history(1)
        memories = [{"id": "active", "memory": "test-secret"},
                    {"id": "expired", "memory": "expired-data", "expiration_date": "2000-01-01"},
                    {"id": "revoked", "memory": "revoked-data", "metadata": {"status": "inactive"}}]
        result = build_context_envelope(cid, rows, memories=memories,
            redact=lambda text: text.replace("test-secret", "[redacted]"))
        self.assertEqual([row["id"] for row in result["memories"]], ["active"])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_capacity_recovery_keeps_current_image_and_system_whole(self):
        current = {"role": "user", "content": [{"type": "text", "text": "current"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,CURRENT"}}]}
        old_image = {"role": "user", "content": [{"type": "text", "text": "old"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}}]}
        system = {"role": "system", "content": "system-role"}
        history = [{"role": "user", "content": f"record-{i}" + "large" * 2000} for i in range(8)]
        rows = [system, old_image, *history, current]
        smaller = compact_provider_messages(rows, target_bytes=4096)
        self.assertEqual(smaller[0], system)
        self.assertEqual(smaller[-1], current)
        self.assertLess(len(json.dumps(smaller)), len(json.dumps(rows)))
        self.assertNotIn("base64,OLD", json.dumps(smaller))
        self.assertIn("原始记录仍保存在本地", smaller[1]["content"])


if __name__ == "__main__":
    unittest.main()
