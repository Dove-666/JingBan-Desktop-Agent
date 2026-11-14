"""Small operation subjects for the local desktop, never raw tool arguments."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

FILE_TOOLS = frozenset({"read", "write", "edit", "glob", "grep", "read_image", "present", "read_file", "write_file", "edit_file", "read_document", "create_document",
                       "render_document", "verify_artifact", "analyze_csv", "describe_artifact",
                       "inspect_directory", "search_files"})
WEB_TOOLS = frozenset({"web_fetch", "browser_navigate"})


def operation_subject(name, arguments):
    if name in FILE_TOOLS:
        path = arguments.get("path")
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
    if not isinstance(subject, dict):
        return None
    if name == "update_plan" and set(subject) == {"kind", "count"}:
        count = subject["count"]
        return f"{count} 项步骤" if subject["kind"] == "plan" and type(count) is int and 0 <= count <= 32 else None
    if set(subject) != {"kind", "name"}:
        return None
    value = subject["name"]
    if (not isinstance(value, str) or not 1 <= len(value) <= 96 or value in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in "/\\:?<>\"")):
        return None
    if name in FILE_TOOLS and subject["kind"] == "file":
        return "文件：" + value
    if name in WEB_TOOLS and subject["kind"] == "website" and re.fullmatch(r"[A-Za-z0-9.-]+", value):
        return "网站：" + value
    return None


def subject_detail_for_label(label, subject):
    from .activity import TOOL_LABELS
    for name, known_label in TOOL_LABELS.items():
        if label == known_label:
            detail = subject_detail(name, subject)
            if detail:
                return detail
    return None
