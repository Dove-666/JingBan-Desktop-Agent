"""Real SDK loop, synthetic planner/resolver/judge, all network denied.

These are control-boundary regressions, not live-model quality measurements.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
_test_streams = sys.stdout, sys.stderr
import worker
sys.stdout, sys.stderr = _test_streams
from browser_session import BrowserSession
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from task_harness import TaskHarness

ORIGINAL = 'Create report.txt containing exactly "HELLO".'
ANSWER = 'Created report.txt containing exactly HELLO.'


class RetryAndResumeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, choice, *, task=ORIGINAL, history=(), goals=None, revision="keep"):
        planners, judges, revisions, harnesses, frames = [], [], [], [], []
        class ObservedHarness(TaskHarness):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                harnesses.append(self)

        async def respond(provider, messages, **kwargs):
            try:
                snapshot = json.loads(messages[-1]["content"])
            except (ValueError, KeyError, TypeError):
                snapshot = {}
            if snapshot.get("schema") == "aura.goal_revision_snapshot.v1":
                revisions.append(snapshot)
                if revision == "invalid":
                    return LLMResponse(content='{"status":"okay"}')
                operations = [{"op": "keep", "requirement_id": item["id"]}
                              for item in snapshot["active_requirements"]]
                if revision in {"replace", "new_question"}:
                    correction = snapshot["host_corrections"][-1]
                    anchor = {"correction_id": correction["correction_id"], "quote": correction["text"]}
                    operations = [{"op": "supersede", "requirement_id": item["id"], "correction": anchor}
                                  for item in snapshot["active_requirements"]]
                    if revision == "replace":
                        operations.append({"op": "add", "requirement": {"id": "new-output", "kind": "artifact",
                            "path": "new.txt", "exact_text": "NEW"}, "correction": anchor})
                return LLMResponse(content=json.dumps({"schema": "aura.goal_revision_patch.v1",
                    **{key: snapshot[key] for key in ("timeline_id", "revision_id", "base_requirements_id")},
                    "status": "resolved", "operations": operations}))
            if snapshot.get("schema") == "aura.goal_snapshot.v1":
                judges.append(snapshot)
                ids = snapshot["evidence_ids"]
                # Deliberately optimistic: deterministic missing-file checks
                # must still reject this independent model's answer.
                return LLMResponse(content=json.dumps({"schema": "aura.goal_verdict.v1",
                    **{key: snapshot[key] for key in ("goal_id", "evidence_id", "answer_sha256")},
                    "status": "satisfied" if ids else "answer_only", "evidence_ids": ids}))
            index = len(planners)
            planners.append(messages)
            if index >= 20:
                raise AssertionError("finite synthetic provider cutoff reached")
            chosen = choice(index)
            if isinstance(chosen, str):
                return LLMResponse(content=chosen)
            name, arguments = chosen
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id=f"synthetic-{index}", name=name, arguments=arguments)], finish_reason="tool_calls")

        def no_network(*args, **kwargs):
            raise AssertionError("network forbidden")

        with tempfile.TemporaryDirectory(prefix="aura-retry-resume-") as owned:
            root = Path(owned)
            workspace = root / "files"
            request = {"id": "synthetic-retry-resume", "kind": "agent", "task": task,
                "context": "", "session_history": list(history),
                "config": {"api_key": "synthetic-not-sent", "base_url": "http://127.0.0.1:1/v1", "model": "offline"},
                "options": {"workspace": str(workspace), "timeout_seconds": 0, "max_steps": 0,
                    "tools_enabled": True, "computer_enabled": False, "checkpoint_required": False, "include_activity": True}}
            if goals is not None:
                request["goal_history"] = list(goals)
            with patch.object(worker, "_packaged_profile_root", return_value=root / "profile"), \
                 patch.object(worker, "TaskHarness", ObservedHarness), \
                 patch.object(worker, "emit", side_effect=lambda frame_type, request_id=None, **values: frames.append({"type": frame_type, **values})), \
                 patch.object(OpenAICompatProvider, "chat", respond), \
                 patch.object(OpenAICompatProvider, "chat_stream", respond), \
                 patch.object(BrowserSession, "available", return_value=False), \
                 patch.object(socket, "create_connection", no_network), \
                 patch.object(socket.socket, "connect", no_network):
                result = await worker.run_agent(request)
            files = {path.name: path.read_text(encoding="utf-8") for path in workspace.iterdir() if path.is_file()}
        return result, files, planners, judges, revisions, harnesses[0], frames

    async def test_repeated_invalid_and_alternating_calls_stop_without_global_step_limit(self):
        cases = [
            (lambda i: ("unavailable_tool", {}), 4),
            (lambda i: ("read_file", {"offset": 0}), 4),
            (lambda i: ("read_file", {"path": f"missing-{i % 2}.txt"}), 7),
            (lambda i: ("finish_task", {"status": "completed", "summary": f"Done, draft {i}"}), 4),
        ]
        for choice, attempts in cases:
            with self.subTest(attempts=attempts, first=choice(0)[0]):
                result, files, calls, judges, revisions, harness, frames = await self.run_worker(choice)
                self.assertEqual(result["status"], "needs_user", result)
                self.assertEqual(len(calls), attempts)
                self.assertTrue(harness.stalled)
                self.assertEqual(files, {})
                self.assertTrue(any(row["type"] == "activity" and row["status"] == "failed" for row in frames))
                if choice(0)[0] == "finish_task":
                    self.assertFalse(any(row["type"] == "activity" and row.get("name") == "finish_task"
                                         and row["status"] == "completed" for row in frames))

    async def test_bad_arguments_can_be_corrected_and_goal_really_completed(self):
        steps = [("write_file", {}), ("write_file", {"path": "report.txt", "content": "HELLO"}),
                 ("read_file", {"path": "report.txt"}), ("finish_task", {"status": "completed", "summary": ANSWER}), ANSWER]
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(lambda i: steps[min(i, len(steps)-1)])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {"report.txt": "HELLO"})
        self.assertFalse(harness.stalled)

    async def test_repeated_unchanged_reads_cannot_renew_failed_completion_budget(self):
        def choice(index):
            if index == 0:
                return "write_file", {"path": "report.txt", "content": "WRONG"}
            if index % 2:
                return "read_file", {"path": "report.txt"}
            return "finish_task", {"status": "completed", "summary": f"Done {index}"}
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(choice)
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(len(calls), 9, "Four rejected completions must stop despite fresh read IDs")
        self.assertTrue(harness.stalled)
        self.assertEqual(files, {"report.txt": "WRONG"})

    async def test_genuine_file_repair_after_rejected_checks_can_complete(self):
        steps = [("write_file", {"path": "report.txt", "content": "WRONG"})]
        for _ in range(3):
            steps.extend([("read_file", {"path": "report.txt"}),
                          ("finish_task", {"status": "completed", "summary": ANSWER})])
        steps.extend([("edit_file", {"path": "report.txt", "old_text": "WRONG", "new_text": "HELLO",
                                     "expected_hash": hashlib.sha256(b"WRONG").hexdigest()}),
                      ("read_file", {"path": "report.txt"}),
                      ("finish_task", {"status": "completed", "summary": ANSWER}), ANSWER])
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: steps[min(i, len(steps)-1)])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {"report.txt": "HELLO"})
        self.assertFalse(harness.stalled)

    async def test_continue_cannot_drop_missing_original_file(self):
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: ANSWER, task="继续", history=[{"role": "user", "content": ORIGINAL}])
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(files, {})
        self.assertTrue(judges)
        self.assertTrue(all(ORIGINAL in item["original_task"] for item in judges))
        self.assertTrue(all(item["requirements"] for item in judges))
        self.assertIn(ORIGINAL, calls[0][0]["content"])

    async def test_goal_journal_survives_compacted_native_replay(self):
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: ANSWER, task="继续", goals=[ORIGINAL, "保留原来的内容要求"],
            history=[{"role": "assistant", "content": "Older tool replay was compacted."}])
        self.assertEqual(result["status"], "needs_user", result)
        self.assertIn(ORIGINAL, calls[0][0]["content"])
        self.assertIn("保留原来的内容要求", judges[0]["original_task"])

    async def test_explicit_replacement_does_not_resurrect_cancelled_file(self):
        done = "Created new.txt containing NEW."
        steps = [("write_file", {"path": "new.txt", "content": "NEW"}),
                 ("read_file", {"path": "new.txt"}), ("finish_task", {"status": "completed", "summary": done}), done]
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: steps[min(i, len(steps)-1)], task="Cancel report.txt. Instead create new.txt containing exactly NEW.",
            goals=[ORIGINAL], revision="replace")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {"new.txt": "NEW"})
        self.assertEqual([item["path"] for item in judges[-1]["requirements"]], ["new.txt"])

    async def test_independent_new_question_does_not_require_old_output(self):
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: "4", task="New question: what is 2 + 2?", goals=[ORIGINAL], revision="new_question")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {})
        self.assertFalse(judges[-1]["requirements"])

    async def test_invalid_goal_resolution_stops_before_planner_or_effect(self):
        result, files, calls, judges, revisions, harness, frames = await self.run_worker(
            lambda i: ("write_file", {"path": "unwanted.txt", "content": "NO"}),
            task="继续", goals=[ORIGINAL], revision="invalid")
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(files, {})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
