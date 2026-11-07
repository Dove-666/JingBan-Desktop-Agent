"""Task plans and evidence gates around the existing nanobot execution loop.

The canonical_plan normalization algorithm is adapted from DeepSeek Harness
tool-todo at ce7073d51254c26cd63b22a60835b8f948deb447 (MIT, (c) 2026 DeepSeek).
See third_party/deepseek_harness/LICENSE and README.md. Evidence and completion
gates are AURA implementations; a readable file is not proof of an entire goal.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from safe_files import MAX_FILE_BYTES, WorkspaceFiles
from gui_verification import checked_report, unconfirmed_reply


class HarnessError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_plan(items):
    """Whole-list replacement, unique trimmed steps and one active executor."""
    if not isinstance(items, list) or len(items) > 32:
        raise HarnessError("INVALID_PLAN")
    result, seen, seen_ids, active = [], set(), set(), 0
    for item in items:
        if (not isinstance(item, dict) or not {"content", "status"} <= set(item)
                or set(item) - {"content", "status", "id", "reason"}
                or not isinstance(item["content"], str)
                or not isinstance(item["status"], str)
                or item["status"] not in {"pending", "in_progress", "completed", "cancelled", "superseded"}):
            raise HarnessError("INVALID_PLAN")
        content = item["content"].strip()
        if (not content or len(content) > 400
                or any(ord(char) < 32 and char not in "\t\n" for char in content)):
            raise HarnessError("INVALID_PLAN")
        if content in seen:
            raise HarnessError("DUPLICATE_PLAN_STEP")
        seen.add(content)
        active += item["status"] == "in_progress"
        step = {"content": content, "status": item["status"]}
        if "id" in item:
            identifier = item["id"]
            if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
                    or identifier in seen_ids):
                raise HarnessError("INVALID_PLAN_STEP_ID")
            seen_ids.add(identifier)
            step["id"] = identifier
        reason = item.get("reason", "")
        if (not isinstance(reason, str) or len(reason) > 400
                or any(ord(char) < 32 and char not in "\t\n" for char in reason)
                or item["status"] in {"cancelled", "superseded"} and not reason.strip()):
            raise HarnessError("INVALID_PLAN_REASON")
        if reason.strip():
            step["reason"] = reason.strip()
        result.append(step)
    if active > 1:
        raise HarnessError("MULTIPLE_ACTIVE_STEPS")
    return result


class TaskHarness:
    def __init__(self, workspace: Path, *, files=None, contracts=None, require_evidence=False,
                 goal_contract=None, goal_evidence=None):
        from artifact_verifier import ArtifactVerifier
        from goal_contract import GoalContract
        from goal_evidence import GoalEvidence
        if goal_contract is not None and not isinstance(goal_contract, GoalContract):
            raise HarnessError("INVALID_GOAL_CONTRACT")
        if goal_evidence is not None and not isinstance(goal_evidence, GoalEvidence):
            raise HarnessError("INVALID_GOAL_EVIDENCE")
        self.files = files if files is not None else WorkspaceFiles(workspace)
        self.goal_contract = goal_contract
        self.goal_evidence = goal_evidence if goal_evidence is not None else GoalEvidence()
        self._goal_verdict = None
        self.goal_diagnostic = None
        self._goal_answer = ""
        self._goal_pending_effects = []
        self.require_evidence = require_evidence
        self.evidence_tools = set()
        self.verifier = ArtifactVerifier(workspace, files=self.files)
        from csv_source_contract import CsvSourceContract
        self.contracts = tuple((CsvSourceContract(),) if contracts is None else contracts)
        if any(not callable(getattr(contract, "verify", None)) for contract in self.contracts):
            raise HarnessError("INVALID_ARTIFACT_CONTRACT")
        self.plan = []
        self.plan_history = []
        self._plan_revision = 0
        self.writes = set()
        self.written_hashes = {}
        self.written_sizes = {}
        self.verified_hashes = {}
        self.verified_source_hashes = {}
        self.failed_tools = set()
        self.failure_counts = {}
        self.failure_operations = {}
        self.failure_events = []
        self.failure_categories = {}
        self._explicit_required = set()
        self._consecutive_failure = None
        self._consecutive_failure_count = 0
        self.verification_requests = {}
        self.analyses = {}
        self.csv_reads = set()
        self.requested_status = None
        self.requested_summary = ""
        self.gui_required = False
        self.gui_verification = None
        self._gui_epoch = 0
        self._verified_gui_epoch = None

    def begin_gui_action(self):
        """Called by the host before effects, never by a model-visible tool."""
        self.gui_required = True
        self._gui_epoch += 1
        self.gui_verification = None
        self._verified_gui_epoch = None

    def record_gui_verification(self, value):
        self.gui_verification = checked_report(value)
        self._verified_gui_epoch = (self._gui_epoch if self.gui_verification
                                    and self.gui_verification["status"] == "verified" else None)
        if self._verified_gui_epoch is not None:
            self.resolve_effects({"computer_use", "desktop_act", "desktop_activate", "open_application"})
            # A fresh passing parent check supersedes earlier failed parent
            # checks. finish_task may invoke this same trusted checker itself;
            # it must not leave a stale required failure poisoning completion.
            for key in list(self.failed_tools):
                if key[0] == "verify_desktop_goal":
                    self.failed_tools.discard(key)
                    self.failure_categories.pop(key, None)
                    self._explicit_required.discard(key)
                    self._clear_failure_streaks({key})

    def _clear_failure_streaks(self, operations):
        """Forget only failure attempts superseded by a corresponding result."""
        recovered = [streak for streak, operation in self.failure_operations.items()
                     if operation in operations]
        for streak in recovered:
            self.failure_counts.pop(streak, None)
            self.failure_operations.pop(streak, None)
        if self._consecutive_failure in recovered:
            self._consecutive_failure, self._consecutive_failure_count = None, 0

    def resolve_effects(self, tool_names):
        """Host-only: called after an adapter confirms a fresh actual outcome.

        This is deliberately absent from the model tool catalogue. A successful
        unrelated tool or a model completion string cannot clear uncertainty.
        """
        resolved = set()
        for key in list(self.failed_tools):
            if self.failure_categories.get(key) == "uncertain" and key[0] in tool_names:
                self.failed_tools.discard(key)
                self.failure_categories.pop(key, None)
                resolved.add(key)
        self._clear_failure_streaks(resolved)

    @property
    def has_gui_action(self):
        return self._gui_epoch > 0

    @property
    def gui_goal_verified(self):
        return (self.gui_required and self.gui_verification is not None
                and self.gui_verification["status"] == "verified"
                and self._verified_gui_epoch == self._gui_epoch)

    def update_plan(self, steps, reason=""):
        plan = canonical_plan(steps)
        if not isinstance(reason, str) or len(reason) > 400:
            raise HarnessError("INVALID_PLAN_REASON")
        for step in plan:
            if "id" not in step:
                previous = next((old for old in self.plan if old["content"] == step["content"]), None)
                step["id"] = (previous["id"] if previous else
                              "step-" + hashlib.sha256(step["content"].encode()).hexdigest()[:16])
        identifiers = {step["id"] for step in plan}
        removed = []
        for old in self.plan:
            if old["id"] not in identifiers:
                removed.append({**old, **({"status": "superseded", "reason": reason.strip() or
                    "Replaced by the current plan; no action is implied."}
                    if old["status"] not in {"completed", "cancelled", "superseded"} else {})})
        self._plan_revision += 1
        self.plan_history.append({"revision": self._plan_revision, "reason": reason.strip(),
                                  "removed": deepcopy(removed), "steps": deepcopy(plan)})
        self.plan_history = self.plan_history[-32:]
        self.plan = plan
        return json.dumps({"revision": self._plan_revision, "steps": deepcopy(plan),
            "superseded": removed, "counts": {
            status: sum(item["status"] == status for item in plan)
            for status in ("pending", "in_progress", "completed", "cancelled", "superseded")}}, ensure_ascii=False)

    def _relative(self, path):
        relative = self.files.display_path(self.files.resolve(path))
        return os.path.normcase(relative).replace("\\", "/")

    def record_success(self, name, arguments, result):
        if name == "compute_project_artifact":
            if __package__: from .project_runtime import published_artifact
            else: from project_runtime import published_artifact
            if published_artifact(result) is None:
                raise HarnessError("PROJECT_PUBLICATION_RECEIPT_REQUIRED")
        if self.goal_contract is not None:
            self.goal_evidence.observe(name, arguments, result)
        if name not in {"update_plan", "finish_task", "ask_user_question", "request_file_access", "recall_context"}:
            self.evidence_tools.add(name)
        key = self._operation_key(name, arguments)
        if self.failure_categories.get(key) != "uncertain":
            self.failed_tools.discard(key)
            self.failure_categories.pop(key, None)
            self._explicit_required.discard(key)
        # A successful observation or plan update is not evidence that another
        # operation recovered. An exploratory candidate may be superseded by a
        # successful alternative of that tool; required/unknown results retain
        # their own identity. Unknown input needs host-only effect settlement.
        recovered = {operation for operation in self.failure_operations.values()
                     if operation[0] == name and operation not in self.failed_tools}
        if name in {"write_file", "edit_file", "create_document", "write_document", "render_document", "compute_project_artifact"}:
            recovered.update(operation for operation in self.failure_operations.values()
                             if operation == ("verify_artifact", key[1])
                             and self.failure_categories.get(operation) != "uncertain")
        if name == "calculate":
            superseded = {item for item in self.failed_tools if item[0] == name
                          and item not in self._explicit_required
                          and self.failure_categories.get(item) != "uncertain"}
            self.failed_tools -= superseded
            for item in superseded:
                self.failure_categories.pop(item, None)
            recovered.update(superseded)
        self._clear_failure_streaks(recovered)
        if name in {"write_file", "edit_file", "create_document", "write_document", "render_document", "compute_project_artifact"}:
            try:
                observation = json.loads(result)
            except (ValueError, TypeError):
                observation = {}
            written = (observation.get("written") if isinstance(observation, dict) else None) or arguments["path"]
            relative = self._relative(written)
            self.writes.add(relative)
            if isinstance(observation, dict) and isinstance(observation.get("sha256"), str):
                self.written_hashes[relative] = observation["sha256"]
                self.written_sizes[relative] = observation.get("bytes")
            elif isinstance(arguments.get("content"), str):
                raw = arguments["content"].encode("utf-8")
                self.written_hashes[relative] = hashlib.sha256(raw).hexdigest()
                self.written_sizes[relative] = len(raw)
            else:
                _, raw, digest = self.files.read_bytes(written)
                self.written_hashes[relative] = digest
                self.written_sizes[relative] = len(raw)
            self.verified_hashes.pop(relative, None)
        elif name == "read_file" and arguments["path"].casefold().endswith(".csv"):
            self.csv_reads.add(self._relative(arguments["path"]))
        elif name == "analyze_csv":
            value = json.loads(result)
            # A measured alternative source resolves exploratory analysis
            # attempts, not a host-declared mandatory missing source.
            if isinstance(value, dict) and re.fullmatch(r"[a-f0-9]{64}", value.get("source_sha256", "")):
                superseded = {item for item in self.failed_tools if item[0] == name
                              and item not in self._explicit_required
                              and self.failure_categories.get(item) != "uncertain"}
                self.failed_tools -= superseded
                for item in superseded:
                    self.failure_categories.pop(item, None)
                self._clear_failure_streaks(superseded)
            analysis_key = (self._relative(arguments["path"]), arguments["value_column"],
                            arguments.get("group_by", ""), arguments.get("delimiter", ","))
            self.analyses[analysis_key] = {
                "source_path": arguments["path"], "value_column": arguments["value_column"],
                "group_by": arguments.get("group_by", ""),
                "delimiter": arguments.get("delimiter", ","),
                "source_sha256": value["source_sha256"],
            }

    def _operation_key(self, name, arguments=None):
        if arguments is None:
            return name, "unknown"
        if name == "compute_project_artifact":
            try:
                identity = self._relative(arguments.get("output", {}).get("target", ""))
            except (ValueError, OSError, AttributeError):
                identity = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        elif name in {"read_file", "read_file_page", "read_document", "write_file", "edit_file",
                    "create_document", "write_document", "render_document", "analyze_csv", "verify_artifact"}:
            try:
                identity = self._relative(arguments.get("path", ""))
            except (ValueError, OSError):
                identity = hashlib.sha256(str(arguments.get("path", "")).encode()).hexdigest()
        else:
            identity = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return name, identity

    def record_failure(self, name, arguments=None, *, classification=None, code=""):
        exploratory = {"read_file", "read_file_page", "read_document", "search_files", "web_fetch",
            "web_search", "system_info", "inspect_system", "inspect_disk_usage", "inspect_directory",
            "discover_applications", "desktop_view", "desktop_windows", "desktop_observe",
            "browser_observe", "browser_tabs", "inspect_image", "inspect_document_page",
            "run_code", "update_plan", "finish_task"}
        effects = {"computer_use", "desktop_act", "open_application"}
        category = classification or ("exploratory" if name in exploratory else
                                     "uncertain" if name in effects else "required")
        if category not in {"exploratory", "required", "uncertain"}:
            raise HarnessError("INVALID_FAILURE_CLASSIFICATION")
        key = self._operation_key(name, arguments)
        if category != "exploratory":
            self.failed_tools.add(key)
            self.failure_categories[key] = category
            if classification == "required":
                self._explicit_required.add(key)
        normalized = dict(arguments) if arguments is not None else {}
        if "path" in normalized:
            normalized["path"] = key[1]
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        streak = (name, digest)
        self._consecutive_failure_count = self._consecutive_failure_count + 1 if streak == self._consecutive_failure else 1
        self._consecutive_failure = streak
        self.failure_counts = {streak: self._consecutive_failure_count}
        self.failure_operations = {streak: key}
        self.failure_events.append({"tool": name, "category": category, "code": str(code)[:64],
                                    "operation": digest, "consecutive": self._consecutive_failure_count})
        self.failure_events = self.failure_events[-128:]

    @property
    def stalled(self):
        return self._consecutive_failure_count >= 4

    def verify_artifact(self, path, kind="auto", source_path="", value_column="", group_by="", delimiter=",", expect_empty=False):
        if not all(isinstance(value, str) for value in (path, kind, source_path, value_column, group_by, delimiter)):
            raise HarnessError("INVALID_VERIFICATION_REQUEST")
        if type(expect_empty) is not bool:
            raise HarnessError("INVALID_VERIFICATION_REQUEST")
        request = {"kind": kind, "source_path": source_path,
                   "value_column": value_column, "group_by": group_by, "delimiter": delimiter}
        if expect_empty:
            request["expected_bytes"] = 0
        relative = self._relative(path)
        self.verification_requests[relative] = request
        value = self._verify(relative, request, refresh=True)
        if value.get("passed") and value.get("sha256"):
            self.verified_hashes[relative] = value["sha256"]
            if value.get("source_sha256"):
                self.verified_source_hashes[relative] = value["source_sha256"]
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _reject(report, issue):
        return {**report, "passed": False, "issues": [*report.get("issues", []), issue]}

    def _verify(self, path, request, *, refresh=False):
        empty = request.get("expected_bytes") == 0 or self.written_sizes.get(path) == 0
        options = {**request, **({"expected_bytes": 0} if empty else {})}
        minimum = self.verifier.verify(path, kind="auto", expected_bytes=0 if empty else None)
        report = self.verifier.verify(path, **options) if request.get("kind", "auto") != "auto" else minimum
        for contract in self.contracts:
            report = contract.verify(self, path, request, report, refresh=refresh)
        if not minimum.get("passed"):
            report = {**report, "passed": False,
                      "issues": list(dict.fromkeys([*minimum.get("issues", []), *report.get("issues", [])])),
                      "checks": [*minimum.get("checks", []), *report.get("checks", [])]}
        expected = self.written_hashes.get(path) or self.verified_hashes.get(path)
        if expected and report.get("sha256") != expected:
            report = self._reject(report, "ARTIFACT_CHANGED_AFTER_WRITE_OR_VERIFICATION")
        expected_source = self.verified_source_hashes.get(path)
        if expected_source and ((not refresh and report.get("source_sha256") != expected_source)
                                or not report.get("source_sha256")):
            report = self._reject(report, "SOURCE_CHANGED_AFTER_VERIFICATION")
        return report

    def _checks(self):
        return [{"path": path, **self._verify(path, self.verification_requests.get(path, {"kind": "auto"}))}
                for path in sorted(self.writes | self.verification_requests.keys())]

    def goal_snapshot(self, answer=None, *, pending_effects=None):
        """Host-only independent-judge input; never a model-visible tool.

        The original goal and actual observations are separate from the answer
        under review. Fresh readback invalidates receipts after external edits.
        """
        if self.goal_contract is None:
            raise HarnessError("GOAL_CONTRACT_MISSING")
        if answer is not None:
            if not isinstance(answer, str):
                raise HarnessError("INVALID_GOAL_ANSWER")
            self._goal_answer = answer
        if pending_effects is not None:
            if (not isinstance(pending_effects, (list, tuple)) or len(pending_effects) > 128
                    or any(not isinstance(item, str) or len(item) > 128 for item in pending_effects)):
                raise HarnessError("INVALID_GOAL_PENDING_EFFECTS")
            self._goal_pending_effects = list(pending_effects)
        return self.goal_contract.snapshot(self.files, self.goal_evidence,
            answer=self._goal_answer, artifacts=self._checks(),
            gui_goal_verified=self.gui_goal_verified, pending_effects=self._goal_pending_effects)

    def record_goal_verification(self, value):
        """Record only a host-created independent judge receipt, never a tool."""
        if self.goal_contract is None:
            raise HarnessError("GOAL_CONTRACT_MISSING")
        # Retain only data; validation produces static codes and never echoes a
        # provider exception, free-form justification or hidden reasoning.
        self._goal_verdict = deepcopy(value)
        return self.goal_contract.assess(self.goal_snapshot(), self._goal_verdict)

    def assess(self, *, answer=None):
        reports = self._checks()
        issues = []
        goal_report = None
        if self.goal_contract is not None:
            if answer is not None:
                if not isinstance(answer, str):
                    raise HarnessError("INVALID_GOAL_ANSWER")
                self._goal_answer = answer
            snapshot = self.goal_contract.snapshot(self.files, self.goal_evidence,
                answer=self._goal_answer, artifacts=reports,
                gui_goal_verified=self.gui_goal_verified, pending_effects=self._goal_pending_effects)
            goal_report = self.goal_contract.assess(snapshot, self._goal_verdict)
            issues.extend(goal_report["issues"])
        # A production goal contract supersedes the old tool-count heuristic.
        # It permits ordinary questions while requiring goal-relevant evidence
        # for actions. Keep the heuristic for legacy consumers without a goal.
        if self.require_evidence and self.goal_contract is None and not self.evidence_tools:
            issues.append("EXECUTION_EVIDENCE_MISSING")
        if any(step["status"] not in {"completed", "cancelled", "superseded"} for step in self.plan):
            issues.append("PLAN_UNFINISHED")
        if self.failed_tools:
            issues.append("UNRESOLVED_TOOL_FAILURE")
        if any(not item.get("passed") for item in reports):
            issues.append("ARTIFACT_VERIFICATION_FAILED")
        if self.gui_required and not self.gui_goal_verified:
            issues.append("GUI_GOAL_UNVERIFIED")
        return {"passed": not issues, "issues": issues, "artifacts": reports,
                "plan": deepcopy(self.plan), "failed_tools": sorted({item[0] for item in self.failed_tools}),
                "gui_goal_verified": self.gui_goal_verified,
                **({"goal": goal_report} if goal_report is not None else {}),
                "scope": "Declared artifact checks and fresh independent GUI goal verification; semantic visual checks remain model-dependent."}

    def finish_task(self, status, summary=""):
        if (not isinstance(status, str) or status not in {"completed", "needs_user"}
                or not isinstance(summary, str) or len(summary) > 4000):
            raise HarnessError("INVALID_TASK_FINISH")
        self.requested_status = status
        self.requested_summary = summary.strip()
        assessment = self.assess(answer=summary if self.goal_contract is not None else None)
        accepted = status != "completed" or assessment["passed"]
        return json.dumps({"accepted": accepted,
                           "status": status if accepted else "needs_user", **assessment,
                           **({"next_action": "Continue from current observations, repair the failed condition, "
                               "then call finish_task again. Do not replay unknown effects or mark unfinished "
                               "plan steps completed without evidence."} if not accepted else {})}, ensure_ascii=False)

    def failure_reply(self, assessment):
        """A concrete failure replaces an optimistic draft; no generic handoff."""
        diagnostic = self.goal_diagnostic or ""
        causes = {
            "GOAL_JUDGE_PROVIDER_AUTH_FAILED": "结果检查服务的密钥无效。",
            "GOAL_JUDGE_PROVIDER_RATE_LIMITED": "结果检查服务请求过于频繁。",
            "GOAL_JUDGE_PROVIDER_TIMEOUT": "结果检查服务响应超时。",
            "GOAL_JUDGE_PROVIDER_CONNECTION": "结果检查服务连接失败。",
            "GOAL_JUDGE_PROVIDER_SERVER_ERROR": "结果检查服务暂时故障。",
            "GOAL_JUDGE_FINISH_LENGTH": "结果检查服务的回复被截断。",
        }
        if diagnostic in causes:
            return "未完成：" + causes[diagnostic]
        issues = set(assessment.get("issues", []))
        if "UNRESOLVED_TOOL_FAILURE" in issues:
            from tool_catalog import TOOL_LABELS
            tools = [TOOL_LABELS.get(name, "任务操作") for name in assessment.get("failed_tools", [])] or ["任务操作"]
            return "未完成：" + "、".join(dict.fromkeys(tools[:3])) + "失败。"
        if "ARTIFACT_VERIFICATION_FAILED" in issues or "GOAL_ARTIFACT_CHANGED" in issues:
            return "未完成：生成文件未通过内容检查。"
        if "GOAL_REQUIREMENT_FAILED" in issues:
            return "未完成：指定的文件或操作结果未达到要求。"
        if "PLAN_UNFINISHED" in issues:
            pending = [step["content"] for step in self.plan if step["status"] in {"pending", "in_progress"}]
            return "未完成：" + "；".join(pending[:3])[:600].rstrip("。；") + "。"
        return "未完成：结果检查未通过。"

    def finalize(self, reply, fallback_status="completed"):
        assessment = self.assess(answer=reply if self.goal_contract is not None else None)
        # Existing GUI/user stop states outrank a later planner completion.
        status = fallback_status
        if status == "completed" and (self.requested_status == "needs_user" or not assessment["passed"]):
            status = "needs_user"
        if status == "needs_user" and not assessment["passed"]:
            if "GUI_GOAL_UNVERIFIED" in assessment["issues"]:
                # Do not keep an optimistic planner paragraph after a failed judge.
                reply = unconfirmed_reply(self.gui_verification)
            else:
                reply = self.failure_reply(assessment)
        return reply, status, assessment
