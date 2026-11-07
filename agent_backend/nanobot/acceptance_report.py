"""Fixed, non-content diagnostics shared by worker, host and task benchmarks."""
from __future__ import annotations

ISSUE_CODES = frozenset({
    "EXECUTION_EVIDENCE_MISSING", "PLAN_UNFINISHED", "UNRESOLVED_TOOL_FAILURE",
    "ARTIFACT_VERIFICATION_FAILED", "GUI_GOAL_UNVERIFIED", "GOAL_REQUIREMENT_FAILED",
    "GOAL_EFFECT_UNRESOLVED", "GOAL_ARTIFACT_READBACK_FAILED", "GOAL_ARTIFACT_CHANGED",
    "GOAL_SNAPSHOT_INVALID", "GOAL_JUDGE_MISSING", "GOAL_JUDGE_INVALID", "GOAL_JUDGE_STALE",
    "GOAL_ANSWER_ONLY_INVALID", "GOAL_JUDGE_EVIDENCE_MISSING", "GOAL_NOT_SATISFIED", "GOAL_UNVERIFIED",
    "GOAL_FILE_UNAVAILABLE", "GOAL_FILE_FORMAT_MISMATCH", "GOAL_FILE_CHANGED",
    "GOAL_FILE_FORMAT_UNVERIFIED", "GOAL_FILE_TEXT_MISMATCH", "GOAL_FILE_CONTENT_MISSING",
    "GOAL_FILE_HASH_MISMATCH", "GOAL_FILE_NUMBER_MISMATCH", "GOAL_WRITE_NOT_OBSERVED",
    "GOAL_GUI_UNVERIFIED", "GOAL_REVISION_UNRESOLVED", "GOAL_REVISION_GUI_REVIEW_REQUIRED", "ACCEPTANCE_CHECK_FAILED",
    "WORKER_CLEANUP_UNCONFIRMED", "RESEARCH_CLEANUP_UNCONFIRMED", "DOCUMENT_REVIEW_CONVERSION_UNCONFIRMED",
    "PROJECT_EXECUTION_CLEANUP_UNCONFIRMED", "PROJECT_PUBLICATION_UNCONFIRMED",
    "DESKTOP_VISUAL_CLEANUP_UNCONFIRMED"})
JUDGE_STATUSES = frozenset({"satisfied", "answer_only", "not_satisfied", "unknown", "invalid", "stale",
                            "missing", "not_required", "not_run"})
_FIELDS = frozenset({"version", "passed", "issue_codes", "judge_status", "requirements_checked",
                    "requirements_passed", "artifacts_checked", "artifacts_passed"})


def project_assessment(assessment):
    goal = assessment.get("goal") or {}
    checks, artifacts = goal.get("requirements", []), assessment.get("artifacts", [])
    issues = [*assessment.get("issues", [])]
    for row in checks:
        issues.extend(row.get("issues", []))
    status = goal.get("judge_status", "not_run")
    return {"version": 1, "passed": assessment.get("passed") is True,
            "issue_codes": sorted({value if isinstance(value, str) and value in ISSUE_CODES
                                   else "ACCEPTANCE_CHECK_FAILED" for value in issues}),
            "judge_status": status if isinstance(status, str) and status in JUDGE_STATUSES else "invalid",
            "requirements_checked": min(len(checks), 128),
            "requirements_passed": min(sum(row.get("passed") is True for row in checks), 128),
            "artifacts_checked": min(len(artifacts), 128),
            "artifacts_passed": min(sum(row.get("passed") is True for row in artifacts), 128)}


def checked_assessment(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != _FIELDS or value["version"] != 1
            or type(value["version"]) is not int or type(value["passed"]) is not bool
            or not isinstance(value["judge_status"], str) or value["judge_status"] not in JUDGE_STATUSES
            or not isinstance(value["issue_codes"], list) or len(value["issue_codes"]) > len(ISSUE_CODES)
            or any(not isinstance(code, str) or code not in ISSUE_CODES for code in value["issue_codes"])
            or any(type(value[key]) is not int or not 0 <= value[key] <= 128 for key in
                   ("requirements_checked", "requirements_passed", "artifacts_checked", "artifacts_passed"))
            or value["requirements_passed"] > value["requirements_checked"]
            or value["artifacts_passed"] > value["artifacts_checked"]):
        raise ValueError("INVALID_ACCEPTANCE_REPORT")
    return dict(value, issue_codes=list(value["issue_codes"]))
