"""Host-owned GUI completion evidence; tool/model assertions cannot mint a pass.

The separate judge design was inspected in Browser Use's _judge_trace at
b49a68e4060acb226da8f784194fef301c53a6d7. Unlike its telemetry-only judgement,
AURA makes a fresh passing judgement a mandatory GUI completion gate.
No upstream code is copied into this module.
"""
from __future__ import annotations

from datetime import datetime, timezone

UNCONFIRMED_REPLY = "未完成：没有取得有效的操作结果记录。"
REPORT_KEYS = {"status", "method", "reason", "checked_at"}


def checked_feedback(value, *, secrets=()):
    """Bound independent observations for the planner, not a permission grant.

    The public UI still receives fixed status labels. Details remain private
    tool observations and can describe a missing postcondition for recovery.
    """
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {"reason", "checks", "reobserve", "source"}
            or not isinstance(value.get("reason"), str)
            or value.get("reason") not in _UNCONFIRMED_REASONS and value.get("reason") not in {"GOAL_STATE_CONFIRMED", "NO_PROGRESS"}
            or not isinstance(value.get("checks", []), list) or len(value.get("checks", [])) > 32):
        raise ValueError("GUI_VERIFICATION_FEEDBACK_INVALID")
    checks = []
    for item in value.get("checks", []):
        if (not isinstance(item, dict) or set(item) != {"requirement", "observed", "passed"}
                or type(item["passed"]) is not bool
                or any(not isinstance(item[key], str) or not 1 <= len(item[key].strip()) <= 1500
                       or any(ord(c) < 32 and c not in "\n\t" for c in item[key]) for key in ("requirement", "observed"))):
            raise ValueError("GUI_VERIFICATION_FEEDBACK_INVALID")
        cleaned = dict(item)
        for key in ("requirement", "observed"):
            for secret in secrets:
                if isinstance(secret, str) and secret:
                    cleaned[key] = cleaned[key].replace(secret, "[redacted]")
        checks.append(cleaned)
    return {"reason": value["reason"], "checks": checks,
            "reobserve": value.get("reason") != "GOAL_STATE_CONFIRMED",
            "source": "independent_visual_observation"}

# Public explanations are selected by exact, host-known codes. Neither model
# prose nor arbitrary report fields may become a diagnostic shown to the user.
_UNCONFIRMED_REASONS = {
    "GOAL_STATE_NOT_MET": ("failed", "未完成：操作后的目标状态未达到要求。"),
    "GOAL_NOT_OBSERVABLE": ("unverified", "未完成：当前画面缺少用于核对结果的信息。"),
    "TARGET_IDENTITY_UNCONFIRMED": ("unverified", "未完成：没有取得你指定的对象的识别信息。"),
    "TEMPORAL_EVIDENCE_MISSING": ("unverified", "未完成：缺少目标顺序记录，无法定位指定的第几个对象。"),
    "VERIFICATION_CAPTURE_FAILED": ("unverified", "未完成：操作后截图失败。"),
    "VERIFICATION_RESPONSE_INVALID": ("unverified", "未完成：结果检查服务没有返回有效答复。"),
    "VERIFICATION_SERVICE_UNAVAILABLE": ("unverified", "未完成：结果检查服务暂时不可用。"),
    "VERIFICATION_NOT_RUN": ("unverified", "未完成：操作在检查结果前停止。"),
}


def unconfirmed_reply(verification):
    """Describe only a validated fixed reason; never imply a manual handoff.

    A failed visible postcondition differs from missing evidence. Unknown,
    malformed, contradictory and success reports all retain the neutral safe
    fallback; this function cannot create completion evidence.
    """
    try:
        value = checked_report(verification)
    except (ValueError, TypeError, OverflowError):
        return UNCONFIRMED_REPLY
    if value is None:
        return UNCONFIRMED_REPLY
    selected = _UNCONFIRMED_REASONS.get(value["reason"])
    if selected is None or value["status"] != selected[0]:
        return UNCONFIRMED_REPLY
    return selected[1]


def utc_now():
    return datetime.now(timezone.utc)


def report(status="unverified", *, method="visual", reason="GUI_GOAL_UNVERIFIED"):
    return {"status": status, "method": method, "reason": reason,
            "checked_at": utc_now().isoformat(timespec="milliseconds")}


def native_launch_report(original_task, observed_name, verified):
    """A visible, wrong application cannot satisfy a single-app launch goal."""
    from windows_apps import parse_open_request, normalized_name
    expected = parse_open_request(original_task)
    if (verified is True and expected is not None and isinstance(observed_name, str)
            and normalized_name(observed_name) == normalized_name(expected)):
        return report("verified", method="native", reason="APP_WINDOW_VERIFIED")
    return None


def checked_report(value, *, started_at=None, now=None):
    """Validate worker-owned evidence shape and freshness, without coercion."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != REPORT_KEYS
            or not isinstance(value["status"], str)
            or value["status"] not in {"verified", "failed", "unverified"}
            or not isinstance(value["method"], str)
            or value["method"] not in {"visual", "uia", "native"}
            or not isinstance(value["reason"], str) or not value["reason"].strip()
            or len(value["reason"]) > 2000
            or any(ord(c) < 32 and c not in "\n\t" for c in value["reason"])
            or not isinstance(value["checked_at"], str) or len(value["checked_at"]) > 40):
        raise ValueError("GUI_VERIFICATION_INVALID")
    try:
        checked = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("GUI_VERIFICATION_INVALID") from None
    if checked.tzinfo is None or checked.utcoffset() is None:
        raise ValueError("GUI_VERIFICATION_INVALID")
    current = now or utc_now()
    if (checked - current).total_seconds() > 2:
        raise ValueError("GUI_VERIFICATION_INVALID")
    if started_at is not None and (started_at - checked).total_seconds() > 2:
        raise ValueError("GUI_VERIFICATION_STALE")
    return dict(value)


def result_fields(gui_used, verification, *, status="completed"):
    return {"gui_used": bool(gui_used),
            "gui_goal_verified": bool(status == "completed" and gui_used and verification and verification["status"] == "verified"),
            "verification": verification}
