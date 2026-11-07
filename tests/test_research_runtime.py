"""Unregistered runtime helper: real SDK children, host controls and cache gates."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot.research_runtime import ResearchRuntime, ResearchRuntimeError
from agent_backend.nanobot.safe_files import WorkspaceFiles
from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import PYTHON, REPO


@unittest.skipUnless(PYTHON.is_file(), "actual isolated SDK required")
class ResearchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="research-runtime-")
        self.root = Path(self.owned.name)
        self.workspace = self.root / "sources"; self.workspace.mkdir()
        (self.workspace / "a.txt").write_text("Alpha runtime fact 17.", encoding="utf-8")
        (self.workspace / "b.txt").write_text("Beta runtime fact 29.", encoding="utf-8")
        (self.root / "forbidden.txt").write_text("Not in delegated scope.", encoding="utf-8")
        self.models, self.runtimes = [], []
        self.revision, self.call, self.authorized = "host-revision-1", "host-call-1", True
        self.cancel, self.host_guard = threading.Event(), threading.RLock()

    async def asyncTearDown(self):
        for runtime in self.runtimes: await runtime.aclose()
        for model in self.models:
            model.close(); self.assertEqual([], model.errors)
        self.owned.cleanup()

    @staticmethod
    def model_step(body):
        first = json.loads(body["messages"][1]["content"])
        if [row["function"]["name"] for row in body["tools"]] != ["read_source"]:
            raise AssertionError("Child acquired another tool or recursive delegation")
        rows = [row for row in body["messages"] if row.get("role") == "tool" and row.get("name") == "read_source"]
        if not rows: return "read_source", {"source_id": first["sources"][0]["source_id"]}
        read = json.loads(rows[-1]["content"])
        return json.dumps({"conclusion": "Actual bounded research reference.",
            "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})

    def make(self, action=None):
        model = SeparateGoalModel([action or self.model_step] * 40, judge_mode="optimistic")
        self.models.append(model)
        runtime = ResearchRuntime(files=WorkspaceFiles(self.workspace), parent_run_id=str(uuid4()), conversation_id=str(uuid4()),
            current_goal_revision=lambda: self.revision, authorization_current=lambda: self.authorized,
            parent_cancellation=self.cancel, call_identity=lambda: self.call, authority_guard=lambda: self.host_guard,
            config={"base_url": model.base_url, "model": "synthetic-runtime-research", "api_key": "SYNTHETIC_RUNTIME_PRIVATE_KEY"},
            python_executable=str(PYTHON), deadline_seconds=15)
        self.runtimes.append(runtime)
        return runtime, model

    async def test_actual_read_references_and_duplicate_recheck_preserve_original_hash_without_new_model_request(self):
        runtime, model = self.make()
        assignments = [{"task": "Read alpha and cite its fact.", "paths": ["a.txt"]}]
        first = await runtime.delegate_research(assignments)
        self.assertEqual("reference_ready", first["status"], first)
        self.assertTrue(first["reference_only"])
        self.assertFalse(first["claims_verified"] or first["parent_goal_completed"])
        self.assertEqual(2, len(model.planner_calls))
        again = await runtime.delegate_research(assignments)
        self.assertTrue(again["duplicate"] and again["original_observations_reused"] and again["source_versions_rechecked"])
        self.assertEqual(first["children"][0]["citations"], again["children"][0]["citations"])
        self.assertEqual(2, len(model.planner_calls))
        (self.workspace / "a.txt").write_text("Changed since the original observation.", encoding="utf-8")
        stale = await runtime.delegate_research(assignments)
        self.assertEqual("RESEARCH_SOURCE_CHANGED", stale["code"])
        self.assertTrue(stale["duplicate"])
        self.assertEqual([], stale["children"])
        self.assertEqual(2, len(model.planner_calls))

    async def test_model_cannot_supply_host_identity_or_expand_parent_file_grants(self):
        runtime, model = self.make()
        with self.assertRaises(ResearchRuntimeError):
            await runtime.delegate_research([{"task": "Fake parent identity", "paths": ["a.txt"], "parent_run_id": str(uuid4())}])
        denied = await runtime.delegate_research([{"task": "Read outside current grant.", "paths": [str(self.root / "forbidden.txt")]}])
        self.assertEqual("unverified", denied["status"])
        self.assertEqual([], denied["children"])
        self.assertEqual([], model.planner_calls)

    async def test_same_host_call_with_modified_assignments_is_not_replayed(self):
        runtime, model = self.make()
        await runtime.delegate_research([{"task": "First authorized research", "paths": ["a.txt"]}])
        second = await runtime.delegate_research([{"task": "Changed under the same call", "paths": ["b.txt"]}])
        self.assertEqual("RESEARCH_CALL_IDENTITY_CONFLICT", second["code"])
        self.assertEqual(2, len(model.planner_calls))

    async def test_goal_revision_change_cancels_real_child_and_discards_late_reference(self):
        entered, release = threading.Event(), threading.Event()
        def action(_): entered.set(); release.wait(10); return "Late old-goal reference must not be adopted."
        runtime, model = self.make(action)
        task = asyncio.create_task(runtime.delegate_research([{"task": "Read old-goal source", "paths": ["a.txt"]}]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 8))
            with self.host_guard:
                self.revision = "host-revision-2"
                runtime.invalidate()
            result = await asyncio.wait_for(task, 8)
            self.assertEqual("RESEARCH_PARENT_INACTIVE", result["code"])
            self.assertEqual([], result["children"])
            self.assertEqual(1, len(model.planner_calls))
            self.assertTrue(all(entry.group.cleanup_confirmed() for entry in runtime._entries.values()))
        finally: release.set()

    async def test_parent_cancel_waits_for_worker_closure_and_propagates_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def action(_): entered.set(); release.wait(10); return "Unvalidated late model answer"
        runtime, model = self.make(action)
        task = asyncio.create_task(runtime.delegate_research([{"task": "Read source", "paths": ["a.txt"]}]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 8))
            self.cancel.set()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 8)
            self.assertTrue(all(entry.group.cleanup_confirmed() for entry in runtime._entries.values()))
            self.assertEqual(1, len(model.planner_calls))
        finally: release.set()

    async def test_cancelled_unknown_call_is_cached_but_new_host_call_can_continue_after_exact_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        first = [True]
        def action(body):
            if first[0]:
                first[0] = False; entered.set(); release.wait(10); return "Late first request"
            return self.model_step(body)
        runtime, model = self.make(action)
        assignments = [{"task": "Original readonly source request", "paths": ["a.txt"]}]
        task = asyncio.create_task(runtime.delegate_research(assignments))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 8))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(1, len(model.planner_calls))
            duplicate = await runtime.delegate_research(assignments)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual("cancelled", duplicate["status"])
            self.assertEqual(1, len(model.planner_calls))
            release.set()
            self.call = "host-call-2"
            fresh = await runtime.delegate_research([{"task": "A new explicit host research intent", "paths": ["b.txt"]}])
            self.assertEqual("reference_ready", fresh["status"], fresh)
            self.assertEqual(3, len(model.planner_calls))
        finally: release.set()

    async def test_real_isolated_parent_sdk_can_import_and_run_helper_without_qt(self):
        model = SeparateGoalModel([self.model_step] * 10, judge_mode="optimistic")
        self.models.append(model)
        script = self.root / "owned_parent.py"
        script.write_text("import sys,json,asyncio,threading\nfrom uuid import uuid4\n"
            + "sys.path.insert(0," + repr(str(REPO / "agent_backend/nanobot")) + ")\n"
            + "from research_runtime import ResearchRuntime\nfrom safe_files import WorkspaceFiles\n"
            + "data=json.loads(sys.stdin.readline())\nasync def main():\n"
              "    lock=threading.RLock()\n"
              "    runtime=ResearchRuntime(files=WorkspaceFiles(data['root']),parent_run_id=str(uuid4()),conversation_id=str(uuid4()),"
              "current_goal_revision=lambda:'trusted-r1',authorization_current=lambda:True,parent_cancellation=threading.Event(),"
              "call_identity=lambda:'trusted-call-1',authority_guard=lambda:lock,config=data['config'],deadline_seconds=15)\n"
              "    try:\n        result=await runtime.delegate_research(data['assignments'])\n"
              "    finally:\n        proof=await runtime.aclose()\n"
              "    print(json.dumps({'result':result,'proof':proof,'qt_loaded':any(name.startswith('PySide6') for name in sys.modules)}))\n"
              "asyncio.run(main())\n", encoding="utf-8")
        payload = {"root": str(self.workspace), "config": {"base_url": model.base_url, "model": "isolated-parent-runtime", "api_key": "SYNTHETIC_RUNTIME_PRIVATE_KEY"},
            "assignments": [{"task": "Read alpha.", "paths": ["a.txt"]}, {"task": "Read beta.", "paths": ["b.txt"]}]}
        def execute():
            return subprocess.run([str(PYTHON), "-I", "-B", "-u", str(script)], input=json.dumps(payload),
                text=True, encoding="utf-8", capture_output=True, timeout=25,
                creationflags=0x08000000 if sys.platform == "win32" else 0)
        process = await asyncio.to_thread(execute)
        self.assertEqual(0, process.returncode, "Isolated parent helper failed")
        result = json.loads(process.stdout)
        self.assertFalse(result["qt_loaded"])
        self.assertTrue(result["proof"]["children_closed"])
        self.assertEqual("reference_ready", result["result"]["status"], result["result"])
        self.assertEqual(2, len(result["result"]["children"]))
        self.assertEqual(4, len(model.planner_calls))
        self.assertNotIn("SYNTHETIC_RUNTIME_PRIVATE_KEY", process.stdout + process.stderr)

    async def test_repeated_tool_cancel_waits_for_exact_cleanup_and_preserves_cancel(self):
        from desktop_pet.agents.research_delegation import ResearchGroup
        entered, release = threading.Event(), threading.Event()
        cleanup_entered, cleanup_release = threading.Event(), threading.Event()
        def action(_): entered.set(); release.wait(10); return "Late owned response"
        runtime, model = self.make(action)
        settle = ResearchGroup._settle_owned
        def delayed(group, run):
            cleanup_entered.set()
            if not cleanup_release.wait(8): return False
            return settle(group, run)
        with patch.object(ResearchGroup, "_settle_owned", delayed):
            task = asyncio.create_task(runtime.delegate_research([{"task": "Read alpha", "paths": ["a.txt"]}]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 8))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(cleanup_entered.wait, 4))
                task.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(task.done(), "Cancellation cannot claim exact child cleanup already finished")
                with self.assertRaisesRegex(ResearchRuntimeError, "RESEARCH_CHILDREN_ACTIVE"):
                    runtime.assert_parent_ready()
            finally: cleanup_release.set(); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(all(row.group.cleanup_confirmed() for row in runtime._entries.values()))
        self.assertEqual(1, len(model.planner_calls))

    async def test_repeated_close_cancel_joins_actual_children_then_preserves_cancel(self):
        from desktop_pet.agents.research_delegation import ResearchGroup
        entered, release = threading.Event(), threading.Event()
        cleanup_entered, cleanup_release = threading.Event(), threading.Event()
        def action(_): entered.set(); release.wait(10); return "Late owned response"
        runtime, model = self.make(action)
        settle = ResearchGroup._settle_owned
        def delayed(group, run):
            cleanup_entered.set()
            if not cleanup_release.wait(8): return False
            return settle(group, run)
        with patch.object(ResearchGroup, "_settle_owned", delayed):
            task = asyncio.create_task(runtime.delegate_research([{"task": "Read alpha", "paths": ["a.txt"]}]))
            closer = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 8))
                closer = asyncio.create_task(runtime.aclose())
                self.assertTrue(await asyncio.to_thread(cleanup_entered.wait, 4))
                closer.cancel(); await asyncio.sleep(.02); closer.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(closer.done(), "Repeated Stop cannot finish host closure while child cleanup is paused")
            finally: cleanup_release.set(); release.set()
            with self.assertRaises(asyncio.CancelledError): await closer
            result = await task
        self.assertEqual("RESEARCH_PARENT_INACTIVE", result["code"])
        self.assertEqual([], result["children"])
        self.assertTrue((await runtime.aclose())["children_closed"])
        self.assertEqual(1, len(model.planner_calls))

    async def test_unknown_exact_owner_blocks_new_work_until_actual_cleanup_reconcile(self):
        from desktop_pet.agents import service as host
        from desktop_pet.agents import research_delegation as research
        allowed, actual = threading.Event(), host._ProcessTree
        class DelayedProof:
            def __init__(self, process): self.actual = actual(process)
            def close(self): self.actual.close()
            def confirm_closed(self, *, timeout_seconds):
                return allowed.is_set() and self.actual.confirm_closed(timeout_seconds=timeout_seconds)
        runtime, model = self.make()
        assignments = [{"task": "Read alpha", "paths": ["a.txt"]}]
        try:
            with patch.object(host, "_ProcessTree", DelayedProof):
                with self.assertRaisesRegex(ResearchRuntimeError, "RESEARCH_CLEANUP_UNCONFIRMED"):
                    await runtime.delegate_research(assignments)
            group = next(iter(runtime._unknown_groups))
            self.assertIn(group, research._GROUP_OWNERS)
            with self.assertRaisesRegex(ResearchRuntimeError, "RESEARCH_CLEANUP_UNCONFIRMED"):
                runtime.assert_parent_ready()
            self.call = "host-fresh-call-after-unknown"
            with self.assertRaisesRegex(ResearchRuntimeError, "RESEARCH_CLEANUP_UNCONFIRMED"):
                await runtime.delegate_research(assignments)
            self.assertEqual(2, len(model.planner_calls))
        finally:
            allowed.set()
            self.assertTrue((await runtime.reconcile_children())["children_closed"])
        self.assertNotIn(group, research._GROUP_OWNERS)
        runtime.assert_parent_ready()
        self.call = "host-call-1"
        duplicate = await runtime.delegate_research(assignments)
        self.assertEqual("RESEARCH_CLEANUP_UNCONFIRMED", duplicate["code"])
        self.assertEqual(2, len(model.planner_calls), "A previous unknown call must not rerun")
        self.call = "host-fresh-call-after-proof"
        fresh = await runtime.delegate_research(assignments)
        self.assertEqual("reference_ready", fresh["status"], fresh)
        self.assertEqual(4, len(model.planner_calls))

    async def test_mixed_failed_child_does_not_skip_freshness_of_valid_cached_references(self):
        def action(body):
            first = json.loads(body["messages"][1]["content"])
            if first["task"] == "Forge reference":
                return json.dumps({"conclusion": "Forged claim", "citations": [{"read_id": "read_" + "0" * 32, "quote": "fake"}]})
            return self.model_step(body)
        runtime, model = self.make(action)
        assignments = [{"task": "Read alpha", "paths": ["a.txt"]}, {"task": "Forge reference", "paths": ["b.txt"]}]
        result = await runtime.delegate_research(assignments)
        self.assertEqual("unverified", result["status"])
        self.assertEqual(1, result["verified_reference_children"])
        self.assertEqual(3, len(model.planner_calls))
        again = await runtime.delegate_research(assignments)
        self.assertTrue(again["source_versions_rechecked"])
        (self.workspace / "a.txt").write_text("Changed valid child's observed source", encoding="utf-8")
        stale = await runtime.delegate_research(assignments)
        self.assertEqual("RESEARCH_SOURCE_CHANGED", stale["code"])
        self.assertEqual([], stale["children"])
        self.assertEqual(3, len(model.planner_calls))

    async def test_revision_changed_during_source_enrollment_cannot_start_any_child(self):
        from desktop_pet.agents.research_delegation import ResearchAuthority
        entered, release = threading.Event(), threading.Event()
        runtime, model = self.make()
        opening = ResearchAuthority.open_scope
        def paused(authority, paths):
            entered.set()
            if not release.wait(8): raise AssertionError("Owned source enrollment was not released")
            return opening(authority, paths)
        with patch.object(ResearchAuthority, "open_scope", paused):
            task = asyncio.create_task(runtime.delegate_research([{"task": "Old goal", "paths": ["a.txt"]}]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 4))
                with self.host_guard:
                    self.revision = "trusted-next-revision"
                    runtime.invalidate()
            finally: release.set()
            result = await task
        self.assertEqual("RESEARCH_PARENT_INACTIVE", result["code"])
        self.assertEqual([], model.planner_calls)
        self.assertTrue(all(row.group is None for row in runtime._entries.values()))

    async def test_payload_cache_eviction_is_not_permission_to_replay_old_call(self):
        # Pure failure enrollments exercise bounded payload storage without
        # launching 17 model requests. Identity tombstones keep replay blocked.
        runtime, model = self.make()
        assignments = [{"task": "Read absent file", "paths": ["absent.txt"]}]
        for index in range(17):
            self.call = "trusted-call-" + str(index)
            result = await runtime.delegate_research(assignments)
            self.assertEqual("RESEARCH_SOURCE_UNAVAILABLE", result["code"])
        self.assertEqual(16, len(runtime._entries))
        self.assertEqual(17, len(runtime._seen))
        self.call = "trusted-call-0"
        old = await runtime.delegate_research(assignments)
        self.assertEqual("RESEARCH_REFERENCE_EVICTED", old["code"])
        self.assertEqual([], model.planner_calls)

    async def test_authority_revoked_after_background_validation_before_async_adoption_drops_references(self):
        runtime, model = self.make()
        ready, release = threading.Event(), threading.Event()
        validate = runtime._references
        def paused(*args, **kwargs):
            value = validate(*args, **kwargs)
            ready.set()
            if not release.wait(8): raise AssertionError("Owned adoption boundary not released")
            return value
        with patch.object(runtime, "_references", paused):
            task = asyncio.create_task(runtime.delegate_research([{"task": "Read alpha", "paths": ["a.txt"]}]))
            try:
                self.assertTrue(await asyncio.to_thread(ready.wait, 8))
                with self.host_guard:
                    self.authorized = False
                    runtime.invalidate()
            finally: release.set()
            result = await task
        self.assertEqual("RESEARCH_PARENT_INACTIVE", result["code"])
        self.assertEqual([], result["children"])
        self.assertEqual(2, len(model.planner_calls))

    async def test_cleanup_reconcile_cannot_claim_closed_while_an_unrelated_live_child_is_active(self):
        entered, release = threading.Event(), threading.Event()
        def action(body):
            entered.set(); release.wait(8)
            return self.model_step(body)
        runtime, model = self.make(action)
        task = asyncio.create_task(runtime.delegate_research([{"task": "Read alpha", "paths": ["a.txt"]}]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 8))
            with self.assertRaisesRegex(ResearchRuntimeError, "RESEARCH_CHILDREN_ACTIVE"):
                await runtime.reconcile_children()
        finally: release.set()
        self.assertEqual("reference_ready", (await task)["status"])
        self.assertTrue((await runtime.reconcile_children())["children_closed"])
        self.assertEqual(2, len(model.planner_calls))


if __name__ == "__main__": unittest.main()
