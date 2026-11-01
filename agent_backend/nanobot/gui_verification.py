"""Host-owned GUI completion evidence; tool/model assertions cannot mint a pass.

The separate judge design was inspected in Browser Use's _judge_trace at
b49a68e4060acb226da8f784194fef301c53a6d7. Unlike its telemetry-only judgement,
AURA makes a fresh passing judgement a mandatory GUI completion gate.
No upstream code is copied into this module.
"""
from __future__ import annotations

from datetime import datetime, timezone

UNCONFIRMED_REPLY = "尚未确认操作目标已达成，任务未标记为完成。已执行的操作保留，未自动重复点击。"
REPORT_KEYS = {"status", "method", "reason", "checked_at"}

# Public explanations are selected by exact, host-known codes. Neither model
# prose nor arbitrary report fields may become a diagnostic shown to the user.
_UNCONFIRMED_REASONS = {
    "GOAL_STATE_NOT_MET": ("failed", "结果核对未通过：当前画面中的目标状态仍未达到要求。"),
    "GOAL_NOT_OBSERVABLE": ("unverified", "当前画面不足以确认目标已经完成，操作是否生效仍待确认。"),
    "TARGET_IDENTITY_UNCONFIRMED": ("unverified", "当前画面无法确认操作的是你指定的对象，结果仍待确认。"),
    "TEMPORAL_EVIDENCE_MISSING": ("unverified", "缺少能确认目标顺序的画面证据，尚不能确认操作的是你指定的第几个对象。"),
    "VERIFICATION_CAPTURE_FAILED": ("unverified", "操作后未能取得可用的新画面，暂时无法核对结果。"),
    "VERIFICATION_RESPONSE_INVALID": ("unverified", "结果核对没有得到有效答复，暂时无法确认操作是否成功。"),
    "VERIFICATION_SERVICE_UNAVAILABLE": ("unverified", "结果核对服务暂时不可用，操作是否生效仍待确认。"),
    "VERIFICATION_NOT_RUN": ("unverified", "操作已停止，尚未完成独立的结果核对。"),
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
    return selected[1] + "任务未标记为完成，没有为了核对结果而再次点击。"


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
