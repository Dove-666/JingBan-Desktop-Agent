"""Registered parent tool, actual SDK parent/children and owned model HTTP only."""
import hashlib
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import Client, PYTHON, REPO

ANSWER = "Alpha=17; Beta=29."
ASSIGNMENTS = [{"task": "Read alpha and cite its fact", "paths": ["a.txt"]},
               {"task": "Read beta and cite its fact", "paths": ["b.txt"]}]


def child_request(body):
    return [row["function"]["name"] for row in body.get("tools", [])] == ["read_source"]


def child_step(body):
    first = json.loads(body["messages"][1]["content"])
    reads = [row for row in body["messages"] if row.get("role") == "tool" and row.get("name") == "read_source"]
    if not reads: return "read_source", {"source_id": first["sources"][0]["source_id"]}
    read = json.loads(reads[-1]["content"])
    return json.dumps({"conclusion": "Reference-only child result", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})


def parent_step(body):
    names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
    if "delegate_research" not in names:
        if "delegate_research" not in [row["function"]["name"] for row in body["tools"]]:
            raise AssertionError("Real parent has no delegated research tool")
        return "delegate_research", {"assignments": ASSIGNMENTS}
    reads = names.count("read_file")
    if reads < 2: return "read_file", {"path": ["a.txt", "b.txt"][reads]}
    if "finish_task" not in names: return "finish_task", {"status": "completed", "summary": ANSWER}
    return ANSWER


@unittest.skipUnless(PYTHON.is_file(), "actual isolated SDK required")
class ResearchParentWorkerTests(unittest.TestCase):
    def setUp(self):
        root = REPO / "agent_workspace"; root.mkdir(exist_ok=True)
        self.owned = tempfile.TemporaryDirectory(prefix="research-parent-", dir=root)
        self.root = Path(self.owned.name)
        self.workspace = self.root / "files"; self.workspace.mkdir()
        (self.workspace / "a.txt").write_text("Alpha=17", encoding="utf-8")
        (self.workspace / "b.txt").write_text("Beta=29", encoding="utf-8")
        self.models, self.clients, self.release = [], [], []

    def tearDown(self):
        for event in self.release: event.set()
        for client in self.clients: self.assertEqual("", client.close())
        for model in self.models: model.close(); self.assertEqual([], model.errors)
        self.owned.cleanup()

    def start(self, action=None, *, wrapper=None, task=None, request_id=None, cid=True, judge_mode="optimistic", office_renderer=None):
        action = action or (lambda body: child_step(body) if child_request(body) else parent_step(body))
        model = SeparateGoalModel([action] * 60, judge_mode=judge_mode)
        self.models.append(model)
        worker_path = None
        if wrapper:
            worker_path = self.root / ("worker_" + uuid4().hex + ".py")
            worker_path.write_text("import sys,asyncio\nsys.path.insert(0," + repr(str(REPO)) + ")\n"
                + "sys.path.insert(0," + repr(str(REPO / "agent_backend/nanobot")) + ")\nimport worker\n"
                + wrapper, encoding="utf-8")
        client = Client(worker_path=worker_path); self.clients.append(client)
        identity = request_id or uuid4().hex
        payload = {"type": "run", "id": identity, "kind": "agent", "context": "",
            "task": task or "Read a.txt and b.txt and report their integer facts. You may delegate read-only research, then check the sources yourself.",
            "config": {"base_url": model.base_url, "model": "owned-parent-and-readonly-children", "api_key": "SYNTHETIC_PARENT_RESEARCH_PRIVATE_KEY"},
            "options": {"workspace": str(self.workspace), "tools_enabled": True, "computer_enabled": False,
                "checkpoint_required": False, "include_activity": True, "max_steps": 0, "timeout_seconds": 25}}
        if cid: payload["conversation_id"] = str(uuid4())
        if office_renderer is not None: payload["config"]["office_renderer"] = office_renderer
        client.send(payload)
        return client, model, identity

    @staticmethod
    def parents(model): return [body for body in model.planner_calls if not child_request(body)]

    @staticmethod
    def delegated(body):
        row = next(row for row in body["messages"] if row.get("role") == "tool" and row.get("name") == "delegate_research")
        return json.loads(row["content"])

    def test_real_parent_delegates_two_isolated_children_rechecks_sources_and_finishes_with_independent_oracle(self):
        client, model, _ = self.start()
        result = client.terminal(timeout=35)
        self.assertEqual("final", result["type"], result)
        self.assertEqual("completed", result["status"], result)
        self.assertEqual(ANSWER, result["reply"])
        parent = self.parents(model)
        self.assertEqual(5, len(parent))
        self.assertEqual(4, len(model.planner_calls) - len(parent))
        delegated = self.delegated(parent[1])
        self.assertEqual("reference_ready", delegated["status"], delegated)
        self.assertFalse(delegated["claims_verified"] or delegated["parent_goal_completed"])
        self.assertEqual(2, len(delegated["children"]))
        expected = {hashlib.sha256((self.workspace / name).read_bytes()).hexdigest(): (self.workspace / name).read_text(encoding="utf-8")
                    for name in ("a.txt", "b.txt")}
        for child in delegated["children"]:
            citation = child["citations"][0]
            self.assertEqual(expected[citation["source_sha256"]], citation["quote"])
            self.assertTrue(child["worker_closed"])
        self.assertEqual({"a.txt", "b.txt"}, {path.name for path in self.workspace.iterdir()})
        snapshot = json.loads(model.judge_calls[0]["messages"][-1]["content"])
        names = [row["tool"] for row in snapshot["facts"]["observed"]["observations"]]
        self.assertEqual(2, names.count("read_file"), "Child summaries must not synthesize parent read observations")
        self.assertEqual(1, names.count("delegate_research"))
        self.assertTrue(result["acceptance"]["passed"])
        self.assertNotIn("SYNTHETIC_PARENT_RESEARCH_PRIVATE_KEY", json.dumps(client.all_frames))

    def test_actual_host_service_nested_worker_children_close_with_exact_parent_receipt(self):
        from desktop_pet.agents import AgentService, AgentSettings, AgentRequest
        model = SeparateGoalModel([lambda body: child_step(body) if child_request(body) else parent_step(body)] * 40,
            judge_mode="optimistic")
        self.models.append(model)
        service = AgentService(checkpoint_path=self.root / "owned-checkpoint.json")
        handles = []
        try:
            settings = replace(AgentSettings(), python_executable=str(PYTHON), workspace=str(self.workspace),
                enabled=True, tools_enabled=True, computer_enabled=False, timeout_seconds=40, max_steps=0)
            result = service.execute(settings, "SYNTHETIC_PARENT_RESEARCH_PRIVATE_KEY",
                AgentRequest("Read a.txt and b.txt and report their integer facts. Delegate the two sources and check them yourself.", "agent"),
                {"api": {"base_url": model.base_url, "model": "owned-host-research"}, "messages": [], "conversation_id": str(uuid4())},
                worker_lifecycle_callback=handles.append)
            self.assertEqual("completed", result.status)
            self.assertEqual(ANSWER, result.reply)
            self.assertTrue(result.acceptance["passed"])
            self.assertEqual(1, len(handles))
            self.assertTrue(service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
            self.assertEqual(5, len(self.parents(model)))
            self.assertEqual(4, sum(child_request(body) for body in model.planner_calls))
        finally: service.shutdown()

    def test_real_host_grant_install_and_child_scope_use_the_identical_authority_guard(self):
        external, proof = self.root / "selected-source.txt", self.root / "grant-guard-proof.json"
        external.write_text("Granted=41", encoding="utf-8")
        wrapper = (
            "from research_runtime import ResearchRuntime\nfrom pathlib import Path\nimport json\n"
            "init=ResearchRuntime.__init__\n"
            "def checked_init(self,**kwargs):\n"
            "    if kwargs['authority_guard']() is not worker._authority_guard: raise RuntimeError('WRONG_HOST_GUARD')\n"
            "    return init(self,**kwargs)\n"
            "ResearchRuntime.__init__=checked_init\n"
            "grant=worker.WorkspaceFiles.add_grant\n"
            "def checked_grant(self,value):\n"
            "    if not worker._authority_guard._is_owned(): raise RuntimeError('GRANT_WITHOUT_HOST_GUARD')\n"
            "    result=grant(self,value)\n"
            "    Path(" + repr(str(proof)) + ").write_text(json.dumps({'grant_guarded':True}))\n"
            "    return result\n"
            "worker.WorkspaceFiles.add_grant=checked_grant\nasyncio.run(worker.main())\n")
        def action(body):
            if child_request(body): return child_step(body)
            names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
            if "request_file_access" not in names:
                return "request_file_access", {"path": str(external), "access": "read", "directory": False, "reason": "Read the exact host-selected source"}
            if "delegate_research" not in names:
                return "delegate_research", {"assignments": [{"task": "Read the newly authorized exact source", "paths": [str(external)]}]}
            if "read_file" not in names: return "read_file", {"path": str(external)}
            if "finish_task" not in names: return "finish_task", {"status": "completed", "summary": "Granted=41"}
            return "Granted=41"
        client, model, identity = self.start(action, wrapper=wrapper,
            task="Read the exact host-selected source after requesting its read grant and report its fact.")
        deadline = time.monotonic() + 25
        while True:
            frame = client.frames.get(timeout=max(.1, deadline - time.monotonic()))
            if frame["type"] == "interaction":
                self.assertEqual("file_access", frame["kind"])
                client.send({"type": "interaction_reply", "id": identity, "interaction_id": frame["interaction_id"],
                    "answer": {"allowed": True, "grant": {"path": str(external), "access": "read", "directory": False}}})
            if frame["type"] in {"final", "error"}:
                result = frame
                break
        self.assertEqual("completed", result.get("status"), result)
        self.assertEqual("Granted=41", result["reply"])
        self.assertTrue(json.loads(proof.read_text())["grant_guarded"])
        delegated = self.delegated(next(body for body in self.parents(model)
            if any(row.get("name") == "delegate_research" for row in body["messages"])))
        self.assertEqual(hashlib.sha256(external.read_bytes()).hexdigest(), delegated["children"][0]["citations"][0]["source_sha256"])

    def test_legacy_nonuuid_or_missing_cid_does_not_get_research_tool(self):
        def plain(body):
            self.assertFalse(child_request(body))
            self.assertNotIn("delegate_research", [row["function"]["name"] for row in body["tools"]])
            return "Plain answer"
        for parameters in ({"request_id": "legacy-qa-nonuuid"}, {"cid": False}):
            with self.subTest(parameters=parameters):
                client, model, _ = self.start(plain, task="Say Plain answer", judge_mode="answer", **parameters)
                result = client.terminal()
                self.assertEqual("completed", result.get("status"), result)
                self.assertEqual(1, len(model.planner_calls))

    def test_child_reference_does_not_satisfy_missing_parent_output(self):
        def action(body):
            if child_request(body): return child_step(body)
            names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
            if "delegate_research" not in names: return "delegate_research", {"assignments": ASSIGNMENTS}
            if "finish_task" not in names: return "finish_task", {"status": "completed", "summary": "Created report.txt"}
            return "Created report.txt"
        client, model, _ = self.start(action, task="Create report.txt containing exactly DONE after reading a.txt and b.txt.")
        result = client.terminal()
        self.assertEqual("needs_user", result.get("status"), result)
        self.assertFalse((self.workspace / "report.txt").exists())
        self.assertFalse(result["acceptance"]["passed"])

    def test_child_cannot_call_write_or_recursive_delegate_even_when_parent_has_both(self):
        def action(body):
            if not child_request(body): return parent_step(body)
            tools = [row for row in body["messages"] if row.get("role") == "tool"]
            if not tools: return "delegate_research", {"assignments": ASSIGNMENTS}
            if len(tools) == 1: return "write_file", {"path": "forbidden.txt", "content": "not allowed"}
            return child_step(body)
        client, model, _ = self.start(action)
        result = client.terminal()
        self.assertEqual("completed", result.get("status"), result)
        self.assertFalse((self.workspace / "forbidden.txt").exists())
        for body in model.planner_calls:
            if child_request(body): self.assertEqual(["read_source"], [row["function"]["name"] for row in body["tools"]])

    def test_parent_cancel_joins_actual_children_and_does_not_reissue_model_requests(self):
        entered, release = threading.Event(), threading.Event(); self.release.append(release)
        arrivals, guard = [], threading.Lock()
        def action(body):
            if not child_request(body): return parent_step(body)
            with guard:
                arrivals.append(True)
                if len(arrivals) == 2: entered.set()
            release.wait(10)
            return "Never adopt this late reference"
        proof = self.root / "exact-close-proof.json"
        wrapper = (
            "from research_runtime import ResearchRuntime\n"
            "original=ResearchRuntime.aclose\n"
            "async def close(self):\n"
            "    result=await original(self)\n"
            "    import pathlib,json\n"
            "    pathlib.Path(" + repr(str(proof)) + ").write_text(json.dumps({'closed':result['children_closed'],'groups':len(self._entries)}))\n"
            "    return result\n"
            "ResearchRuntime.aclose=close\nasyncio.run(worker.main())\n")
        client, model, identity = self.start(action, wrapper=wrapper)
        self.assertTrue(entered.wait(12), "Both actual children did not reach model HTTP")
        client.send({"type": "cancel", "id": identity})
        result = client.terminal(timeout=15)
        self.assertEqual("cancelled", result.get("status"), result)
        self.assertTrue(json.loads(proof.read_text())["closed"])
        self.assertEqual(1, len(self.parents(model)))
        self.assertEqual(2, len(arrivals))

    def test_unknown_child_cleanup_prevents_same_batch_write_next_parent_model_and_completed_delivery(self):
        allowed, proof = self.root / "allow-cleanup", self.root / "reconciled.json"
        wrapper = (
            "from desktop_pet.agents import service\nfrom pathlib import Path\nimport json\n"
            "actual=service._ProcessTree\n"
            "class Unconfirmed:\n"
            "    def __init__(self,process): self.actual=actual(process)\n"
            "    def close(self): self.actual.close()\n"
            "    def confirm_closed(self,*,timeout_seconds):\n"
            "        return Path(" + repr(str(allowed)) + ").exists() and self.actual.confirm_closed(timeout_seconds=timeout_seconds)\n"
            "service._ProcessTree=Unconfirmed\n"
            "from nanobot.providers.openai_compat_provider import OpenAICompatProvider\n"
            "from nanobot.providers.base import ToolCallRequest\n"
            "original_chat=OpenAICompatProvider.chat_stream\n"
            "async def multi(self,messages,**kwargs):\n"
            "    result=await original_chat(self,messages,**kwargs)\n"
            "    if any(call.name=='delegate_research' for call in result.tool_calls):\n"
            "        result.tool_calls.append(ToolCallRequest(id='owned-same-batch-write',name='write_file',arguments={'path':'late-write.txt','content':'MUST_NOT_EXECUTE'}))\n"
            "    return result\n"
            "OpenAICompatProvider.chat_stream=multi\n"
            "async def main():\n"
            "    await worker.main()\n"
            "    for _ in range(500):\n"
            "        if Path(" + repr(str(allowed)) + ").exists(): break\n"
            "        await asyncio.sleep(.02)\n"
            "    result=await worker._running_research.reconcile_children()\n"
            "    Path(" + repr(str(proof)) + ").write_text(json.dumps(result))\n"
            "asyncio.run(main())\n")
        client, model, _ = self.start(wrapper=wrapper)
        try:
            result = client.terminal(timeout=25)
            self.assertEqual("needs_user", result.get("status"), result)
            self.assertIn("RESEARCH_CLEANUP_UNCONFIRMED", result["acceptance"]["issue_codes"])
            self.assertFalse(result["acceptance"]["passed"])
            self.assertEqual(1, len(self.parents(model)), "Unknown child cleanup allowed another parent model request")
            self.assertEqual([], model.judge_calls)
            self.assertFalse((self.workspace / "late-write.txt").exists(), "A later tool in the same SDK batch bypassed unknown child cleanup")
        finally: allowed.write_text("host-owned-fixture-release")
        client.process.wait(timeout=8)
        self.assertTrue(json.loads(proof.read_text())["children_closed"])

    def test_goal_correction_stops_old_children_before_resolver_or_next_parent_request(self):
        entered, release = threading.Event(), threading.Event(); self.release.append(release)
        count, guard = [], threading.Lock()
        def action(body):
            if child_request(body):
                with guard:
                    count.append(True)
                    if len(count) == 2: entered.set()
                release.wait(10)
                return "Late old-goal output"
            names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
            if "delegate_research" not in names: return "delegate_research", {"assignments": ASSIGNMENTS}
            old = self.delegated(body)
            self.assertEqual("RESEARCH_PARENT_INACTIVE", old["code"])
            self.assertEqual([], old["children"])
            if "finish_task" not in names: return "finish_task", {"status": "completed", "summary": "REVISION_OK"}
            return "REVISION_OK"
        client, model, identity = self.start(action)
        def revise(snapshot):
            latest = snapshot["host_corrections"][-1]
            return json.dumps({"schema": "aura.goal_revision_patch.v1", "timeline_id": snapshot["timeline_id"],
                "revision_id": snapshot["revision_id"], "base_requirements_id": snapshot["base_requirements_id"],
                "status": "resolved", "operations": [{"op": "supersede", "requirement_id": row["id"],
                    "correction": {"correction_id": latest["correction_id"], "quote": latest["text"]}} for row in snapshot["active_requirements"]]})
        model.revise = revise
        self.assertTrue(entered.wait(12))
        client.send({"type": "steer", "id": identity, "text": "Stop reading the old sources. Instead answer exactly REVISION_OK."})
        result = client.terminal(timeout=15)
        self.assertEqual("completed", result.get("status"), result)
        self.assertEqual("REVISION_OK", result["reply"])
        self.assertEqual(1, len(model.revision_calls))
        self.assertEqual(2, len(count))
        self.assertEqual(3, len(self.parents(model)))


if __name__ == "__main__": unittest.main()
