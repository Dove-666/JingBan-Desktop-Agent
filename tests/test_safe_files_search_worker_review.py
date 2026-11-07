"""Actual host -> SDK worker -> registered file tools, with an owned fake API."""
import hashlib
import json
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.agents import AgentRequest, AgentService, AgentSettings
from agent_backend.nanobot.tests.fixture_goal_judge import judge_message
from tests.mock_api import MockApiServer


ROOT = Path(__file__).resolve().parents[1]
SDK_PYTHON = ROOT / "agent_backend/nanobot/.venv/Scripts/python.exe"


def tool_reply(name, arguments, identity):
    return {"id": "owned-search-fixture", "object": "chat.completion", "created": 0,
            "model": "owned-fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": identity, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]}}]}


class FixtureJudgePlans(Queue):
    """Keep the SDK's independent guard channel outside the planner script.

    The judge is an explicitly permissive existing test double. This test
    independently verifies real tool results, not model judgement quality.
    """
    def __init__(self, api):
        super().__init__()
        self.api, self.judged_request_ids = api, set()

    def get(self, *args, **kwargs):
        body = self.api.requests[-1]["body"]
        message = judge_message(body)
        if message is None:
            return super().get(*args, **kwargs)
        self.judged_request_ids.add(id(body))
        return {"delay": 0, "status": 200, "content": "", "body": {
            "id": "owned-fixture-judge", "object": "chat.completion", "created": 0,
            "model": "owned-fixture", "choices": [{"index": 0,
            "message": message, "finish_reason": "stop"}]}}


@unittest.skipUnless(SDK_PYTHON.is_file(), "isolated SDK environment missing")
class SearchWorkerReviewTests(unittest.TestCase):
    def test_real_registry_forwards_file_path_line_offset_hash_and_read_line_units(self):
        workspace_parent = (ROOT/"agent_workspace").resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="search-worker-review-", dir=workspace_parent) as td:
            workspace = Path(td).resolve(strict=True)
            self.assertEqual(workspace_parent, workspace.parent)
            target = workspace/"notes.txt"
            original = "".join(f"needle {i}\n" for i in range(25)).encode("utf-8")
            target.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            with MockApiServer() as api:
                api.plans = FixtureJudgePlans(api)
                api.enqueue(body=tool_reply("search_files", {"query": "needle", "path": "notes.txt"}, "owned-search-first"))
                api.enqueue(body=tool_reply("search_files", {"query": "needle", "path": "notes.txt",
                    "line_offset": 20, "expected_hash": digest}, "owned-search-next"))
                api.enqueue(body=tool_reply("search_files", {"query": "needle", "path": "notes.txt",
                    "line_offset": 20, "expected_hash": "0"*64}, "owned-search-stale"))
                api.enqueue(body=tool_reply("search_files", {"query": "needle", "path": "notes.txt",
                    "line_offset": 20, "expected_hash": digest}, "owned-search-recovered"))
                api.enqueue(body=tool_reply("read_file", {"path": "notes.txt", "unit": "lines",
                    "offset": 24, "limit": 1, "expected_hash": digest}, "owned-read-line"))
                api.enqueue(content="The final line is needle 24. The stale source version was rejected; the file was not modified.")
                service = AgentService()
                try:
                    result = service.execute(AgentSettings(python_executable=str(SDK_PYTHON),
                        workspace=str(workspace), timeout_seconds=30), "FAKE_OWNED_SEARCH_KEY",
                        AgentRequest("Read notes.txt and report its last matching line without modifying any files.", "agent"),
                        {"api": {"base_url": api.base_url, "model": "owned-fixture"},
                         "conversation_id": str(uuid4()), "messages": []})
                finally:
                    service.shutdown()
                self.assertEqual("completed", result.status, result.reply)
                self.assertIn("needle 24", result.reply)
                planner_requests = [request for request in api.requests
                                    if id(request["body"]) not in api.plans.judged_request_ids]
                self.assertEqual(6, len(planner_requests))
                self.assertEqual(1, len(api.plans.judged_request_ids))
                schema = {item["function"]["name"]: item["function"]["parameters"]
                          for item in planner_requests[0]["body"]["tools"]}
                self.assertIn("line_offset", schema["search_files"]["properties"])
                self.assertIn("expected_hash", schema["search_files"]["properties"])
                self.assertIn("0-based", schema["read_file"]["properties"]["offset"]["description"])
                observations = {message["tool_call_id"]: message["content"]
                    for message in planner_requests[-1]["body"]["messages"] if message["role"] == "tool"}
                first = json.loads(observations["owned-search-first"])["matches"][0]
                self.assertEqual(list(range(1, 21)), [hit["line"] for hit in first["line_matches"]])
                self.assertEqual(digest, first["sha256"])
                second = json.loads(observations["owned-search-next"])["matches"][0]
                self.assertEqual(list(range(21, 26)), [hit["line"] for hit in second["line_matches"]])
                self.assertIsNone(second["next_line_offset"])
                self.assertIn("FILE_CHANGED", observations["owned-search-stale"])
                line = json.loads(observations["owned-read-line"])
                self.assertEqual("needle 24\n", line["text"])
                self.assertEqual(digest, line["sha256"])
            self.assertEqual(original, target.read_bytes())


if __name__ == "__main__":
    unittest.main()
