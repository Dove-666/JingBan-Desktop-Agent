"""Small operation subjects for the local desktop, never raw tool arguments."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

FILE_TOOLS = frozenset({"read", "write", "edit", "apply_patch", "ls", "glob", "grep", "read_image", "present", "read_file", "write_file", "edit_file", "read_document", "create_document",
                       "render_document", "verify_artifact", "analyze_csv", "describe_artifact",
                       "inspect_directory", "search_files"})
WEB_TOOLS = frozenset({"web_fetch", "browser_navigate"})
COMPUTER_TOOLS = frozenset({"cua", "cua_run_code"})
AGENT_TOOLS = frozenset({"subagent", "subagent_fork", "subagent_activity", "subagent_send", "subagent_wait", "subagent_stop"})


def _public_name(value):
    return (isinstance(value, str) and 1 <= len(value) <= 96 and value not in {".", ".."}
            and not any(ord(char) < 32 or ord(char) == 127 or unicodedata.category(char) == "Cf" for char in value)
            and not any(char in value for char in "/\\:?<>\"`{};=")
            and not re.search(r"\b(?:sk-[\w-]{8,}|Bearer\s|api[_ -]?key|access[_ -]?token|password)\b", value, re.I))


def normalize_subject(name, subject, *, redact=lambda text: text):
    """Validate the small DSH projection again at the process boundary."""
    from .activity import COMPUTER_ACTION_LABELS, AGENT_ACTION_LABELS
    if not isinstance(subject, dict) or not isinstance(subject.get("kind"), str):
        return None
    if set(subject) == {"kind", "count"}:
        count = subject["count"]
        if type(count) is not int:
            return None
        if name in {"update_plan", "todo_write"} and subject["kind"] == "plan" and 0 <= count <= (1000 if name == "todo_write" else 32):
            return {"kind": "plan", "count": count}
        if name in FILE_TOOLS and subject["kind"] == "files" and 1 <= count <= 10000:
            return {"kind": "files", "count": count}
        return None
    if subject.get("kind") in {"computer", "agent"}:
        kind = subject["kind"]
        allowed = COMPUTER_ACTION_LABELS if kind == "computer" else AGENT_ACTION_LABELS
        if (set(subject) - {"kind", "action", "name"} or "action" not in subject
                or not isinstance(subject["action"], str) or subject["action"] not in allowed
                or name not in (COMPUTER_TOOLS if kind == "computer" else AGENT_TOOLS)):
            return None
        result = {"kind": kind, "action": subject["action"]}
        if "name" in subject:
            value = redact(subject["name"]) if isinstance(subject["name"], str) else None
            if not _public_name(value):
                return None
            result["name"] = value
        return result
    if set(subject) != {"kind", "name"}:
        return None
    value = redact(subject["name"]) if isinstance(subject["name"], str) else None
    if not _public_name(value):
        return None
    kind = subject["kind"]
    if (name in FILE_TOOLS and kind == "file"
            or name in WEB_TOOLS and kind == "website" and re.fullmatch(r"[A-Za-z0-9.-]+", value)
            or name in {"pwsh", "bash"} and kind == "command"):
        return {"kind": kind, "name": value}
    return None


def operation_subject(name, arguments):
    if name in FILE_TOOLS:
        path = arguments.get("file_path", arguments.get("path"))
        if isinstance(path, str):
            value = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
            subject = {"kind": "file", "name": value}
            return subject if subject_detail(name, subject) else None
    if name in WEB_TOOLS:
        try:
            host = urlsplit(arguments.get("url", "")).hostname
        except (TypeError, ValueError):
            host = None
        if isinstance(host, str):
            subject = {"kind": "website", "name": host}
            return subject if subject_detail(name, subject) else None
    if name == "update_plan" and isinstance(arguments.get("steps"), list):
        count = len(arguments["steps"])
        if 0 <= count <= 32:
            return {"kind": "plan", "count": count}
    return None


def subject_detail(name, subject):
    """Revalidate at the host boundary; reject paths, query text and payloads."""
    subject = normalize_subject(name, subject)
    if not subject:
        return None
    kind = subject["kind"]
    if kind == "plan":
        return f"{subject['count']} 项步骤"
    if kind == "files":
        return f"{subject['count']} 个文件"
    prefix = {"file": "文件：", "website": "网站：", "computer": "应用：", "agent": "子任务：", "command": ""}
    return prefix[kind] + subject["name"] if subject.get("name") else None


def subject_detail_for_label(label, subject):
    from .activity import TOOL_LABELS, COMPUTER_ACTION_LABELS, AGENT_ACTION_LABELS
    if isinstance(subject, dict) and isinstance(subject.get("action"), str):
        if subject.get("kind") == "computer" and COMPUTER_ACTION_LABELS.get(subject["action"]) == label:
            return subject_detail("cua", subject)
        if subject.get("kind") == "agent" and AGENT_ACTION_LABELS.get(subject["action"]) == label:
            return subject_detail("subagent_activity", subject)
    for name, known_label in TOOL_LABELS.items():
        if label == known_label:
            detail = subject_detail(name, subject)
            if detail:
                return detail
    return None
