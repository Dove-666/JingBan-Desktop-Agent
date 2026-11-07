"""Owned browser research/form acceptance. Default offline, no credentials.

Synthetic runs use actual isolated SDK+Edge DOM with a loopback planner/judge.
Configured runs are explicit opt-in and use existing settings read-only. These
two owned tasks do not change any catalog's unimplemented general task types.
Reports contain fixed outcomes/hashes/counts, never raw planner/judge messages.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
CASES = ("owned_research_two_pages", "owned_form_submit_once")


def digest(raw): return hashlib.sha256(raw).hexdigest()


def source_snapshot():
    paths = [*sorted((ROOT / "agent_backend/nanobot").glob("*.py")),
             *sorted((ROOT / "desktop_pet/agents").glob("*.py")),
             ROOT / "agent_backend/nanobot/browser_dom.js",
             ROOT / "agent_backend/nanobot/tests/test_goal_contract_worker.py",
             ROOT / "tools/qa_owned_browser.py", ROOT / "tools/qa_owned_browser_worker.py", ROOT / "tools/qa_http_response.py"]
    return {str(path.relative_to(ROOT)).replace("\\", "/"): digest(path.read_bytes()) for path in paths}


class OwnedFixture:
    """One nonce/origin per case; independent server state, no model-written oracle."""
    def __init__(self, *, response_loss="pre_headers", idempotent=False):
        if response_loss not in {"pre_headers", "started_headers", "partial_headers", "interim_only"}: raise ValueError("QA_LOSS_MODE_INVALID")
        self.response_loss, self.idempotent = response_loss, idempotent
        self.nonce = secrets.token_hex(16)
        self.prefix = "/" + self.nonce + "/"
        self.lock = threading.Lock()
        self.events, self.posts, self.documents = [], [], {}
        self.effects = 0
        self.receipt = secrets.token_hex(12)
        self.values = {"alpha": "A-" + secrets.token_hex(5), "beta": "B-" + secrets.token_hex(5)}
        self.expected_fields = {"name": "Whale QA " + self.nonce[:6], "note": "Owned synthetic form " + self.nonce[-6:]}
        if idempotent: self.expected_fields["request_token"] = self.nonce
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, status, body="", **headers):
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                for name, value in headers.items(): self.send_header(name.replace("_", "-"), value)
                self.end_headers()
                try: self.wfile.write(raw)
                except ConnectionError: pass
            def do_GET(self):
                with owner.lock: owner.events.append(("GET", self.path))
                route = self.path[len(owner.prefix):] if self.path.startswith(owner.prefix) else None
                if route in {"research/a", "research/b"}:
                    key = "alpha" if route.endswith("a") else "beta"
                    body = f"<html><title>Owned source {key}</title><body><h1>Owned source {key}</h1><p>{key}={owner.values[key]}</p></body></html>"
                elif route == "form":
                    body = ("<html><title>Owned form</title><body><form method='POST' action='" + owner.prefix + "submit'>"
                        "<label for='name'>Name</label><input id='name' name='name' required>"
                        "<label for='note'>Note</label><input id='note' name='note' required>"
                        "<button type='submit'>Submit once</button></form></body></html>")
                elif route == "lost_form":
                    body = ("<html><title>Owned lost-response form</title><body><form method='POST' action='" + owner.prefix + "submit_lost'>"
                        + ("<input type='hidden' name='request_token' value='" + owner.nonce + "'>" if owner.idempotent else "") +
                        "<label for='name'>Name</label><input id='name' name='name'>"
                        "<label for='note'>Note</label><input id='note' name='note'>"
                        "<button type='submit'>Submit once</button></form></body></html>")
                elif route == "receipt" and owner.posts:
                    body = "<html><title>Independent receipt</title><body><h1>Saved receipt</h1><p>receipt=" + owner.receipt + "</p></body></html>"
                elif route == "redirect_port":
                    self.reply(302, Location=owner.blocked_origin + owner.prefix + "forbidden"); return
                elif route == "redirect_path":
                    self.reply(302, Location=owner.origin + "/outside"); return
                else:
                    self.reply(404, "Missing owned fixture route"); return
                with owner.lock: owner.documents[self.path] = digest(body.encode())
                self.reply(200, body)
            def do_POST(self):
                try: size = int(self.headers.get("Content-Length", "0"))
                except ValueError: self.reply(400); return
                if not 0 < size <= 4096: self.reply(413); return
                route = self.path[len(owner.prefix):] if self.path.startswith(owner.prefix) else None
                if route not in {"submit", "submit_lost"}: self.reply(404); return
                fields = parse_qs(self.rfile.read(size).decode("utf-8"), strict_parsing=True)
                with owner.lock:
                    owner.events.append(("POST", self.path))
                    owner.posts.append({key: value[:] for key, value in fields.items()})
                    # This models an application-owned idempotency contract,
                    # never an inference from equal inputs or browser results.
                    if not owner.idempotent or (fields.get("request_token") == [owner.nonce] and owner.effects == 0):
                        owner.effects += 1
                if route == "submit_lost":
                    if owner.response_loss == "partial_headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nIncomplete"); self.wfile.flush()
                    elif owner.response_loss == "interim_only":
                        self.wfile.write(b"HTTP/1.1 103 Early Hints\r\n\r\n"); self.wfile.flush()
                    if owner.response_loss == "started_headers":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", "1024")
                        self.end_headers()
                        self.wfile.write(b"<html><body>partial"); self.wfile.flush()
                    self.close_connection = True
                    try: self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    self.connection.close(); return
                self.reply(303, Location=owner.origin + owner.prefix + "receipt")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.blocked = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.blocked.daemon_threads = True
        self.blocked.block_on_close = False
        self.blocked_origin = f"http://127.0.0.1:{self.blocked.server_port}"
        self.threads = []
        for server in (self.server, self.blocked):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            thread.start(); self.threads.append(thread)

    def url(self, route): return self.origin + self.prefix + route

    def snapshot(self):
        with self.lock:
            return {"events": self.events[:], "posts": [dict(row) for row in self.posts], "documents": dict(self.documents),
                    "effect_count": self.effects}

    def close(self):
        for server in (self.server, self.blocked): server.shutdown(); server.server_close()
        for thread in self.threads: thread.join(3)


def observations(history):
    found = []
    for row in history:
        if row.get("role") != "tool" or not str(row.get("name", "")).startswith("browser_"): continue
        try: value = json.loads(row["content"])
        except (ValueError, TypeError, KeyError): continue
        if isinstance(value, dict):
            if isinstance(value.get("url"), str): found.append(value)
            if isinstance(value.get("post_observation"), dict): found.append(value["post_observation"])
    return found


def oracle(case, fixture, reply, history):
    state = fixture.snapshot()
    seen = observations(history)
    try: answer = json.loads(reply)
    except (ValueError, TypeError): answer = None
    checks = {}
    if case == CASES[0]:
        urls = [fixture.url("research/a"), fixture.url("research/b")]
        checks["ANSWER_EXACT"] = answer == {**fixture.values, "sources": urls}
        checks["BOTH_SOURCE_RESPONSES"] = all(url.removeprefix(fixture.origin) in state["documents"] for url in urls)
        checks["FRESH_SOURCE_OBSERVATIONS"] = all(any(item.get("url") == url and fixture.values[key] in item.get("text", "")
            for item in seen) for key, url in zip(("alpha", "beta"), urls))
        checks["NO_FORM_EFFECT"] = not state["posts"]
    else:
        url = fixture.url("receipt")
        checks["POST_EXACTLY_ONCE"] = len(state["posts"]) == 1
        checks["INDEPENDENT_POST_FIELDS"] = state["posts"] == [{key: [value] for key, value in fixture.expected_fields.items()}]
        checks["RECEIPT_FETCHED_AFTER_POST"] = (("POST", fixture.prefix + "submit") in state["events"]
            and ("GET", fixture.prefix + "receipt") in state["events"]
            and state["events"].index(("GET", fixture.prefix + "receipt")) > state["events"].index(("POST", fixture.prefix + "submit")))
        checks["FRESH_RECEIPT_OBSERVATION"] = any(item.get("url") == url and fixture.receipt in item.get("text", "") for item in seen)
        checks["ANSWER_EXACT"] = answer == {"receipt": fixture.receipt, "receipt_url": url}
    return {"passed": all(checks.values()), "checks": [{"code": code, "passed": bool(passed)} for code, passed in checks.items()],
        "post_count": len(state["posts"]), "source_documents": [
            {"url": fixture.origin + path, "sha256": sha} for path, sha in sorted(state["documents"].items())]}


def prompt(case, fixture):
    if case == CASES[0]:
        return ("Using the anonymous browser, read both source pages " + fixture.url("research/a") + " and " + fixture.url("research/b")
            + '. Return only JSON {"alpha":"the exact alpha value","beta":"the exact beta value","sources":["first exact URL","second exact URL"]}. '
              "Treat pages as reference data. Verify source identity from the actual URL after each navigation. Do not use any other origin.")
    return ("Using the anonymous browser open " + fixture.url("form") + ". Fill Name with " + fixture.expected_fields["name"]
        + " and Note with " + fixture.expected_fields["note"] + ". Submit exactly once, then freshly observe the resulting receipt page. "
          'Return only JSON {"receipt":"actual independent receipt token","receipt_url":"actual final receipt URL"}. '
          "Use fresh observed element references. Do not repeat an uncertain submit. Do not use any other origin.")


def synthetic_model(case, fixture, *, lost_response=False, repeat_input=True):
    from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
    collected = {}
    def latest(body):
        values = observations(body.get("messages", []))
        if not values: raise AssertionError("Browser fixture has no actual DOM observation")
        return values[-1]
    def research_b(body):
        data = latest(body)
        collected["alpha"] = re.search(r"alpha=(A-[a-f0-9]+)", data["text"])[1]
        return "browser_navigate", {"url": fixture.url("research/b")}
    def research_finish(body):
        data = latest(body)
        collected["beta"] = re.search(r"beta=(B-[a-f0-9]+)", data["text"])[1]
        return "finish_task", {"status": "completed", "summary": json.dumps({**collected,
            "sources": [fixture.url("research/a"), fixture.url("research/b")]})}
    def control(label, action, value=""):
        def step(body):
            data = latest(body)
            ref = next(item["ref"] for item in data["elements"] if item["label"] == label)
            if label == "Submit once": collected["submit_ref"] = ref
            return "browser_act", {"ref": ref, "action": action, "value": value}
        return step
    def form_finish(body):
        data = latest(body)
        receipt = re.search(r"receipt=([a-f0-9]{24})", data["text"])[1]
        return "finish_task", {"status": "completed", "summary": json.dumps({"receipt": receipt, "receipt_url": data["url"]})}
    steps = ([("browser_navigate", {"url": fixture.url("research/a")}), research_b, research_finish, "Unchecked rewrite"]
        if case == CASES[0] else [("browser_navigate", {"url": fixture.url("form")}),
            control("Name", "fill", fixture.expected_fields["name"]), control("Note", "fill", fixture.expected_fields["note"]),
            control("Submit once", "click"), ("browser_observe", {}), form_finish, "Unchecked rewrite"])
    if lost_response:
        steps = [("browser_navigate", {"url": fixture.url("lost_form")}),
            control("Name", "fill", fixture.expected_fields["name"]), control("Note", "fill", fixture.expected_fields["note"]),
            control("Submit once", "click"),
            lambda _: ("browser_act", {"ref": collected["submit_ref"], "action": "click"}),
            ("finish_task", {"status": "completed", "summary": "The submit response is unknown."}),
            "Submission is unresolved. Do not repeat it."]
        if not repeat_input: steps.pop(4)

    class IndependentBrowserModel(SeparateGoalModel):
        def judge(self, snapshot):
            rows = snapshot["facts"]["observed"]["observations"]
            history = [{"role": "tool", "name": row["tool"], "content": (
                row.get("observation", {}).get("result") if isinstance(row.get("observation", {}).get("result"), str)
                else json.dumps(row.get("observation", {}).get("result", {})))} for row in rows]
            checked = oracle(case, fixture, snapshot.get("answer", ""), history)
            # The snapshot binds the candidate answer by hash rather than always
            # including raw answer text. Browser evidence still must be real.
            evidence_checks = [row["passed"] for row in checked["checks"] if row["code"] != "ANSWER_EXACT"]
            return json.dumps({"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
                "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                "status": "satisfied" if all(evidence_checks) else "not_satisfied", "evidence_ids": snapshot["evidence_ids"]})

    return IndependentBrowserModel(steps, judge_mode="optimistic")


def run_case(case, provider="synthetic", configured=None, timeout=90, *, lost_response=False, repeat_input=True,
             response_loss="pre_headers", idempotent=False, response_guard=False):
    from unittest.mock import patch
    from desktop_pet.agents import AgentService, AgentRequest, AgentError, AgentSettings
    from desktop_pet.settings_manager import ApiSettings
    from desktop_pet.agents import service as host
    before = source_snapshot()
    fixture, model, service = OwnedFixture(response_loss=response_loss, idempotent=idempotent), None, AgentService()
    worker_handles, tool_ids = [], set()
    started = time.monotonic()
    result = None
    adapter_sha = None
    audit = {}
    status, error = "error", None
    workspace_root = ROOT / "agent_workspace"
    workspace_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="owned-browser-qa-", dir=workspace_root) as owned:
            owned = Path(owned)
            workspace = owned / "workspace"; workspace.mkdir()
            if provider == "synthetic":
                from agent_backend.nanobot.tests.test_worker import PYTHON
                model = synthetic_model(case, fixture, lost_response=lost_response, repeat_input=repeat_input)
                api, key = ApiSettings(model.base_url, "synthetic-browser-qa", 20), "synthetic-owned-browser-key"
                settings = replace(AgentSettings(), python_executable=str(PYTHON))
            else:
                api, key, settings = configured.api, configured.key, configured.settings
            settings = replace(settings, enabled=True, tools_enabled=True, computer_enabled=False,
                timeout_seconds=timeout, max_steps=0, workspace=str(workspace))
            entry = owned / "owned_worker.py"
            audit_path = owned / "host_browser_audit.json"
            entry.write_text("import sys\nsys.path.insert(0," + repr(str(ROOT / "tools")) + ")\n"
                "from qa_owned_browser_worker import worker_main\nworker_main(" + repr(fixture.origin) + "," + repr(fixture.prefix) + "," + repr(str(audit_path))
                + "," + repr(response_guard) + ")\n", encoding="utf-8")
            adapter_sha = digest(entry.read_bytes())
            cid = str(uuid4())
            def activity(frame):
                if frame.get("kind") == "tool" and frame.get("status") == "running": tool_ids.add(frame.get("id"))
            try:
                with patch.object(host, "_WORKER_PATH", entry):
                    task = prompt(case, fixture).replace(fixture.url("form"), fixture.url("lost_form")) if lost_response else prompt(case, fixture)
                    result = service.execute(settings, key, AgentRequest(task, "agent"),
                        {"api": api, "messages": [], "conversation_id": cid}, event_callback=activity,
                        worker_lifecycle_callback=worker_handles.append)
                checked = oracle(case, fixture, result.reply, service._sessions.get(cid, []))
                closed = bool(worker_handles and service.validate_worker_closure(worker_handles[0], service.worker_closure(worker_handles[0])))
                status = "passed" if result.status == "completed" and checked["passed"] and closed else "failed"
                if lost_response:
                    state = fixture.snapshot()
                    checks = {"POST_EXACTLY_ONCE": len(state["posts"]) == 1,
                        "INDEPENDENT_POST_FIELDS": state["posts"] == [{key: [value] for key, value in fixture.expected_fields.items()}],
                        "OUTCOME_REMAINS_UNRESOLVED": result.status == "needs_user",
                        "WHOLE_TASK_NOT_ACCEPTED": result.acceptance is not None and result.acceptance.get("passed") is False,
                        "EXACT_WORKER_CLOSED": closed}
                    if idempotent:
                        checks.pop("POST_EXACTLY_ONCE")
                        checks["AT_LEAST_ONE_POST_REQUEST"] = len(state["posts"]) >= 1
                        checks["INDEPENDENT_POST_FIELDS"] = bool(state["posts"]) and all(
                            row == {key: [value] for key, value in fixture.expected_fields.items()} for row in state["posts"])
                        checks["ONE_IDEMPOTENT_LOGICAL_EFFECT"] = state["effect_count"] == 1
                    checked = {"passed": all(checks.values()), "checks": [{"code": code, "passed": bool(value)} for code, value in checks.items()],
                               "post_count": len(state["posts"])}
                    status = "passed" if checked["passed"] else "failed"
            except AgentError as exc:
                error = exc.code if exc.code in host._ERROR_MESSAGES else "QA_AGENT_ERROR"
                checked = oracle(case, fixture, "", service._sessions.get(cid, []))
                closed = bool(worker_handles and service.validate_worker_closure(worker_handles[0], service.worker_closure(worker_handles[0])))
            if audit_path.is_file():
                candidate = json.loads(audit_path.read_text())
                audit = {name: candidate.get(name) for name in ("dom_click_evaluations", "network_post_events", "fetch_post_events",
                                                              "gateway_unknown_responses", "browser_cleanup_confirmed")}
    except Exception:
        error, checked, closed = "QA_EXECUTION_FAILED", {"passed": False, "checks": []}, False
    finally:
        service.shutdown()
        fixture.close()
        if model is not None: model.close()
    after = source_snapshot()
    stable = before == after
    if not stable: status, error = "error", "QA_SOURCE_CHANGED"
    if model is not None and model.errors: status, error = "error", "QA_SYNTHETIC_PROTOCOL_FAILED"
    if configured is not None and not configured.unchanged(): status, error = "error", "QA_SETTINGS_CHANGED"
    guard_id = "owned_form_idempotent_loss_guard" if idempotent else "owned_form_" + response_loss + "_loss_guard"
    return {"task_id": guard_id if lost_response else case,
        "suite": "guard" if lost_response else "owned_browser_control_chain" if provider == "synthetic" else "owned_browser_real_model",
        "provider": provider, "model": "synthetic-browser-qa" if provider == "synthetic" else configured.model,
        "status": status, "error_code": error, "agent_status": result.status if result else None,
        "duration_seconds": round(time.monotonic() - started, 3), "tool_count": len(tool_ids),
        "independent_oracle": checked, "worker_closed": closed, "real_model_attempted": provider == "configured" and not lost_response,
        "effect_count": fixture.snapshot()["effect_count"], "browser_host_audit": audit,
        "response_loss_phase": response_loss if lost_response else None, "application_idempotency": idempotent,
        "qa_only_http_response_guard": response_guard,
        "owned_adapter_sha256": adapter_sha,
        "source_snapshot": {"scope": "declared_sources_on_disk_not_loaded_binary_attestation", "before": before, "after": after, "unchanged": stable},
        "raw_model_data_saved": False}


def save_report(path, report):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(report, handle, indent=2); handle.write("\n"); temporary = handle.name
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "synthetic", "configured"), default="offline")
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/owned-browser-qa.json")
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 900: parser.error("Timeout must be 5..900 seconds")
    report = {"schema": "aura.owned_browser_qa.v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "provider": args.provider, "separate_from_existing_catalog": True, "cases": []}
    configured = None
    if args.provider == "configured":
        from tools.run_agent_benchmarks import ConfiguredExecutor
        configured = ConfiguredExecutor(args.timeout)
    for case in CASES if args.case == "all" else (args.case,):
        row = ({"task_id": case, "status": "not_run", "real_model_attempted": False} if args.provider == "offline"
               else run_case(case, args.provider, configured, args.timeout))
        report["cases"].append(row)
        save_report(args.output, report)
        print(json.dumps({"task_id": case, "status": row["status"], "oracle_pass": row.get("independent_oracle", {}).get("passed")}))
    return 0 if args.provider == "offline" or all(row["status"] == "passed" for row in report["cases"]) else 1


if __name__ == "__main__": raise SystemExit(main())
