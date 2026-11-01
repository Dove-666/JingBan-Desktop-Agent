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
    result, seen, active = [], set(), 0
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"content", "status"}
                or not isinstance(item["content"], str)
                or not isinstance(item["status"], str)
                or item["status"] not in {"pending", "in_progress", "completed"}):
            raise HarnessError("INVALID_PLAN")
        content = item["content"].strip()
        if (not content or len(content) > 400
                or any(ord(char) < 32 and char not in "\t\n" for char in content)):
            raise HarnessError("INVALID_PLAN")
        if content in seen:
            raise HarnessError("DUPLICATE_PLAN_STEP")
        seen.add(content)
        active += item["status"] == "in_progress"
        result.append({"content": content, "status": item["status"]})
    if active > 1:
        raise HarnessError("MULTIPLE_ACTIVE_STEPS")
    return result


class TaskHarness:
    def __init__(self, workspace: Path):
        from artifact_verifier import ArtifactVerifier
        self.files = WorkspaceFiles(workspace)
        self.verifier = ArtifactVerifier(workspace)
        self.plan = []
        self.writes = set()
        self.written_hashes = {}
        self.verified_hashes = {}
        self.verified_source_hashes = {}
        self.failed_tools = set()
        self.failure_counts = {}
        self.failure_operations = {}
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

    @property
    def gui_goal_verified(self):
        return (self.gui_required and self.gui_verification is not None
                and self.gui_verification["status"] == "verified"
                and self._verified_gui_epoch == self._gui_epoch)

    def update_plan(self, steps):
        plan = canonical_plan(steps)
        # Refusing to silently erase an unfinished checklist makes a missing
        # input visible until the model explicitly ends as needs_user.
        retained = {step["content"] for step in plan}
        if any(step["status"] != "completed" and step["content"] not in retained for step in self.plan):
            raise HarnessError("UNFINISHED_PLAN_CANNOT_BE_REMOVED")
        self.plan = plan
        return json.dumps({"steps": deepcopy(plan), "counts": {
            status: sum(item["status"] == status for item in plan)
            for status in ("pending", "in_progress", "completed")}}, ensure_ascii=False)

    def _relative(self, path):
        relative = self.files.resolve(path).relative_to(self.files.root).as_posix()
        return os.path.normcase(relative).replace("\\", "/")

    def record_success(self, name, arguments, result):
        # Only the same operation can resolve a failure. Reading another file
        # cannot turn a missing required input into a successful observation.
        key = self._operation_key(name, arguments)
        self.failed_tools.discard(key)
        recovered = [streak for streak, operation in self.failure_operations.items() if operation == key]
        if name == "write_file":
            recovered += [streak for streak, operation in self.failure_operations.items()
                          if operation == ("verify_artifact", key[1])]
        for streak in recovered:
            self.failure_counts.pop(streak, None)
            self.failure_operations.pop(streak, None)
        if name == "calculate":
            self.failed_tools = {item for item in self.failed_tools if item[0] != name}
            self.failure_counts = {key: count for key, count in self.failure_counts.items() if key[0] != name}
            self.failure_operations = {key: value for key, value in self.failure_operations.items() if key[0] != name}
        if name == "write_file":
            relative = self._relative(arguments["path"])
            self.writes.add(relative)
            self.written_hashes[relative] = hashlib.sha256(arguments["content"].encode("utf-8")).hexdigest()
            self.verified_hashes.pop(relative, None)
        elif name == "read_file" and arguments["path"].casefold().endswith(".csv"):
            self.csv_reads.add(self._relative(arguments["path"]))
        elif name == "analyze_csv":
            value = json.loads(result)
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
        if name in {"read_file", "write_file", "analyze_csv", "verify_artifact"}:
            try:
                identity = self._relative(arguments.get("path", ""))
            except (ValueError, OSError):
                identity = hashlib.sha256(str(arguments.get("path", "")).encode()).hexdigest()
        else:
            identity = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return name, identity

    def record_failure(self, name, arguments=None):
        if name not in {"update_plan", "finish_task"}:
            key = self._operation_key(name, arguments)
            self.failed_tools.add(key)
            normalized = dict(arguments) if arguments is not None else {}
            if "path" in normalized:
                normalized["path"] = key[1]
            # A corrected CSV column or a stronger validation mode is a new
            # attempt, even though it resolves the same path-level blocker.
            digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            streak = (name, digest)
            self.failure_counts[streak] = self.failure_counts.get(streak, 0) + 1
            self.failure_operations[streak] = key

    @property
    def stalled(self):
        # This is an identical-failure guard, not a successful step budget.
        return any(count >= 4 for count in self.failure_counts.values())

    def verify_artifact(self, path, kind="auto", source_path="", value_column="", group_by="", delimiter=","):
        if not all(isinstance(value, str) for value in (path, kind, source_path, value_column, group_by, delimiter)):
            raise HarnessError("INVALID_VERIFICATION_REQUEST")
        request = {"kind": kind, "source_path": source_path,
                   "value_column": value_column, "group_by": group_by, "delimiter": delimiter}
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
        minimum = self.verifier.verify(path, kind="auto")
        report = self.verifier.verify(path, **request) if request.get("kind", "auto") != "auto" else minimum
        try:
            if path.casefold().endswith(".json"):
                with self.files.resolve(path).open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                target = json.loads(raw.decode("utf-8-sig")) if len(raw) <= MAX_FILE_BYTES else None
            else:
                target = None
        except (ValueError, OSError, UnicodeError):
            target = None
        is_summary = isinstance(target, dict) and {"category_totals", "total", "record_count"} <= target.keys()
        source_path = request.get("source_path", "")
        analysis = None
        selected_request = None
        if source_path:
            try:
                relative_source = self._relative(source_path)
            except (ValueError, OSError):
                return self._reject(report, "SOURCE_OUTSIDE_WORKSPACE")
            candidates = [(key, value) for key, value in self.analyses.items() if key[0] == relative_source]
            if request.get("value_column"):
                selected_key = (relative_source, request["value_column"], request.get("group_by", ""),
                                request.get("delimiter", ","))
                analysis = self.analyses.get(selected_key)
                selected_request = {"kind": "csv_summary", "source_path": source_path,
                    "value_column": request["value_column"], "group_by": request.get("group_by", ""),
                    "delimiter": request.get("delimiter", ",")}
            elif len(candidates) == 1:
                analysis = candidates[0][1]
            elif candidates:
                return self._reject(report, "SOURCE_CONFIGURATION_REQUIRED")
        elif is_summary and len(self.analyses) == 1:
            analysis = next(iter(self.analyses.values()))
        elif is_summary and len(self.analyses) > 1:
            report = self._reject(report, "SOURCE_SELECTION_REQUIRED")
        elif is_summary and self.csv_reads:
            report = self._reject(report, "SOURCE_ANALYSIS_REQUIRED")
        # A weak explicit check cannot downgrade already available evidence.
        if is_summary and selected_request:
            report = self.verifier.verify(path, **selected_request)
        elif is_summary and analysis:
            report = self.verifier.verify(path, kind="csv_summary", source_path=analysis["source_path"],
                value_column=analysis["value_column"], group_by=analysis["group_by"], delimiter=analysis["delimiter"])
        elif is_summary and source_path:
            report = self.verifier.verify(path, kind="csv_summary", source_path=source_path,
                value_column=request.get("value_column", ""), group_by=request.get("group_by", ""),
                delimiter=request.get("delimiter", ","))
        if analysis and (is_summary or request.get("kind") == "csv_summary"):
            source = self.files.resolve(analysis["source_path"])
            try:
                with source.open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                unchanged = (len(raw) <= MAX_FILE_BYTES and
                             hashlib.sha256(raw).hexdigest() == analysis["source_sha256"])
            except OSError:
                unchanged = False
            if not unchanged:
                report = self._reject(report, "SOURCE_CHANGED_AFTER_ANALYSIS")
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

    def assess(self):
        reports = self._checks()
        issues = []
        if any(step["status"] != "completed" for step in self.plan):
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
                "scope": "Declared artifact checks and fresh independent GUI goal verification; semantic visual checks remain model-dependent."}

    def finish_task(self, status, summary=""):
        if (not isinstance(status, str) or status not in {"completed", "needs_user"}
                or not isinstance(summary, str) or len(summary) > 4000):
            raise HarnessError("INVALID_TASK_FINISH")
        self.requested_status = status
        self.requested_summary = summary.strip()
        assessment = self.assess()
        accepted = status != "completed" or assessment["passed"]
        return json.dumps({"accepted": accepted,
                           "status": status if accepted else "needs_user", **assessment}, ensure_ascii=False)

    def finalize(self, reply, fallback_status="completed"):
        assessment = self.assess()
        # Existing GUI/user stop states outrank a later planner completion.
        status = fallback_status
        if status == "completed" and (self.requested_status == "needs_user" or not assessment["passed"]):
            status = "needs_user"
        if status == "needs_user" and not assessment["passed"]:
            if "GUI_GOAL_UNVERIFIED" in assessment["issues"]:
                # Do not keep an optimistic planner paragraph after a failed judge.
                reply = unconfirmed_reply(self.gui_verification)
            else:
                reply = "任务尚未通过验收：存在未完成步骤、工具失败或产物校验问题。\n" + reply
        return reply, status, assessment
