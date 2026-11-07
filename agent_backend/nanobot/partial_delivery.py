"""Bounded partial file delivery after an interrupted task, never task success.

Only host-recorded successful writes with fresh version/format/source checks
are eligible. This produces reports, not file capabilities or delivery copies.
The normal host ArtifactStore still rechecks conversation, current grants and
hashes. No model is called, no tool is replayed and no effect is resolved.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

try:
    from .acceptance_report import project_assessment
except ImportError:
    from acceptance_report import project_assessment

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MODEL_CODES = {
    "invalid_api_key": "MODEL_REQUEST_AUTH_FAILED", "authentication_error": "MODEL_REQUEST_AUTH_FAILED",
    "rate_limit_exceeded": "MODEL_REQUEST_RATE_LIMITED", "context_length_exceeded": "MODEL_REQUEST_CONTEXT_LIMIT",
    "context_window_exceeded": "MODEL_REQUEST_CONTEXT_LIMIT", "max_context_length_exceeded": "MODEL_REQUEST_CONTEXT_LIMIT",
    "model_not_found": "MODEL_REQUEST_CONFIG_INVALID", "timeout": "MODEL_REQUEST_TIMEOUT",
}
_FAILURE_CODES = frozenset({"MODEL_REQUEST_FAILED", "MODEL_REQUEST_AUTH_FAILED", "MODEL_REQUEST_RATE_LIMITED",
    "MODEL_REQUEST_UNAVAILABLE", "MODEL_REQUEST_TIMEOUT", "MODEL_REQUEST_CONTEXT_LIMIT", "MODEL_REQUEST_CONFIG_INVALID",
    "SDK_TURN_FAILED_UNKNOWN_CAUSE", "EXECUTION_STEP_LIMIT", "EXECUTION_STOPPED", "EXECUTION_STOP_REASON_UNKNOWN",
    "EXECUTION_CANCELLED", "EXECUTION_TIMEOUT", "EXECUTION_CONTEXT_LIMIT", "EXECUTION_EMPTY_REPLY", "GUI_EXECUTION_FAILED",
    "WORKER_CLEANUP_UNCONFIRMED"})
FAILURE_DIAGNOSTIC_CODES = _FAILURE_CODES
PARTIAL_SCOPE = "partial_file_readback_and_declared_checks_only; whole_task_incomplete"
PARTIAL_REPLY = "任务执行中断，整体任务尚未完成。已保留并交付通过格式与版本核对的部分文件；未为交付这些文件再次请求模型或重放操作。"


@dataclass(frozen=True)
class FailureDiagnostic:
    boundary: str
    code: str

    def __post_init__(self):
        if (not isinstance(self.boundary, str) or self.boundary not in {"model_request", "sdk_turn", "host_stop", "gui"}
                or not isinstance(self.code, str) or self.code not in _FAILURE_CODES):
            raise ValueError("INVALID_FAILURE_DIAGNOSTIC")


def _attribute(value, name):
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def model_request_failure(value=None):
    """Call at the actual provider boundary; never inspect str/body/content."""
    status = _attribute(value, "error_status_code")
    if type(status) is not int:
        status = _attribute(value, "status_code")
    if type(status) is int and status in {401, 403}:
        code = "MODEL_REQUEST_AUTH_FAILED"
    elif type(status) is int and status == 429:
        code = "MODEL_REQUEST_RATE_LIMITED"
    elif type(status) is int and 500 <= status <= 599:
        code = "MODEL_REQUEST_UNAVAILABLE"
    elif isinstance(value, TimeoutError) or _attribute(value, "error_kind") == "timeout":
        code = "MODEL_REQUEST_TIMEOUT"
    else:
        token = _attribute(value, "error_code")
        code = _MODEL_CODES.get(token, "MODEL_REQUEST_FAILED") if isinstance(token, str) else "MODEL_REQUEST_FAILED"
    return FailureDiagnostic("model_request", code)


class FailureRecorder:
    """Keep only fixed diagnostics for the latest actual planner request.

    A recovered earlier provider error must not be blamed for a later SDK
    failure. The SDK fallback explicitly describes the observation boundary,
    not an invented root cause. Raw exceptions/responses are never retained.
    """
    def __init__(self):
        self._sequence = 0
        self._latest_failure = None

    def begin_model_request(self):
        self._sequence += 1
        self._latest_failure = None
        return self._sequence

    def model_failed(self, sequence, value=None):
        diagnostic = model_request_failure(value)
        if type(sequence) is int and sequence == self._sequence:
            self._latest_failure = diagnostic
        return diagnostic

    def model_succeeded(self, sequence):
        if type(sequence) is int and sequence == self._sequence:
            self._latest_failure = None

    def turn_failed(self):
        return self._latest_failure or FailureDiagnostic("sdk_turn", "SDK_TURN_FAILED_UNKNOWN_CAUSE")


def stop_failure(reason):
    codes = {"max_iterations": "EXECUTION_STEP_LIMIT", "max_steps": "EXECUTION_STEP_LIMIT",
             "cancelled": "EXECUTION_CANCELLED", "timeout": "EXECUTION_TIMEOUT",
             "context_limit": "EXECUTION_CONTEXT_LIMIT", "context_window_exceeded": "EXECUTION_CONTEXT_LIMIT",
             "empty_reply": "EXECUTION_EMPTY_REPLY", "stopped": "EXECUTION_STOPPED"}
    return FailureDiagnostic("host_stop", codes.get(reason, "EXECUTION_STOP_REASON_UNKNOWN")
                             if isinstance(reason, str) else "EXECUTION_STOP_REASON_UNKNOWN")


def partial_delivery(harness, diagnostic, *, authorization_current, unknown_effects=False):
    """Return a strict needs_user candidate or None, preserving original errors.

    User cancellation, revoked authority, no actual writes, malformed reports,
    changed versions and failed file requirements cannot manufacture a partial
    attachment. Other goal/GUI/effect failures remain visible as blockers; they
    do not prevent access to independently verified, already-written files.
    """
    if (not isinstance(diagnostic, FailureDiagnostic) or not callable(authorization_current)
            or type(unknown_effects) is not bool):
        raise ValueError("INVALID_PARTIAL_DELIVERY_REQUEST")
    def authorized():
        try:
            return authorization_current() is True
        except Exception:
            return False
    if diagnostic.code == "EXECUTION_CANCELLED" or not authorized():
        return None
    writes = getattr(harness, "writes", None)
    if not isinstance(writes, set) or not writes:
        return None
    try:
        assessment = harness.assess()
    except Exception:
        return None
    goal_checks = (assessment.get("goal") or {}).get("requirements", [])
    artifacts = []
    for path in sorted(writes)[:64]:
        if not authorized():
            return None
        try:
            expected = harness.written_hashes.get(path)
            if not isinstance(expected, str) or not _HASH.fullmatch(expected):
                continue
            report = harness._verify(path, harness.verification_requests.get(path, {"kind": "auto"}))
            if report.get("passed") is not True or report.get("sha256") != expected:
                continue
            failed_file_requirement = any(item.get("path") and harness._relative(item["path"]) == path
                and item.get("passed") is not True for item in goal_checks)
            if failed_file_requirement:
                continue
            _, _, current = harness.files.read_bytes(path)
            if current != expected or not authorized():
                continue
            artifacts.append({"path": path, "passed": True, "sha256": current, "scope": PARTIAL_SCOPE})
        except Exception:
            continue
    if not artifacts or not authorized():
        return None
    # This is a partial file result regardless of a stale previous goal pass.
    # Never upgrade stop, GUI, effect, plan, or whole-goal verification status.
    assessment["passed"] = False
    assessment["issues"] = list(assessment.get("issues", []))
    if "ACCEPTANCE_CHECK_FAILED" not in assessment["issues"]:
        assessment["issues"].append("ACCEPTANCE_CHECK_FAILED")
    if unknown_effects and "GOAL_EFFECT_UNRESOLVED" not in assessment["issues"]:
        assessment["issues"].append("GOAL_EFFECT_UNRESOLVED")
    return {"reply": PARTIAL_REPLY, "emotion": "neutral", "status": "needs_user",
            "reason_code": diagnostic.code, "completion_scope": "partial_files",
            "artifacts": artifacts, "acceptance": project_assessment(assessment)}
