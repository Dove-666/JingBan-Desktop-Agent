"""Small, text-only native replay records. Never persist pixels or secrets.

Only the host-owned worker protocol can produce these records. Imported chat
messages are a separate untrusted reference field and cannot supply tool roles.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from uuid import UUID

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_HISTORY_BYTES = 256_000
MAX_HISTORY_TURNS = 12
MAX_RECORD_TEXT = 24_000  # UTF-8 bytes, so each protocol frame stays bounded.
TOOL_NAMES = frozenset({"read_file", "write_file", "search_files", "web_fetch",
                        "computer_use", "open_application", "discover_applications", "system_info",
                        "inspect_system", "inspect_disk_usage", "inspect_directory",
                        "calculate", "analyze_csv", "verify_artifact", "update_plan", "finish_task",
                        "desktop_observe", "desktop_act", "desktop_verify_state", "verify_desktop_goal"})


def conversation_uuid(value):
    if not isinstance(value, str):
        raise ValueError("invalid conversation id")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid conversation id") from None
    if str(parsed) != value or parsed.int == 0:
        raise ValueError("invalid conversation id")
    return value


def current_image(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 100:
        raise ValueError("invalid image")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})", value)
    if not match:
        raise ValueError("invalid image")
    try:
        raw = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid image") from None
    signature = ((match[1] == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
                 or (match[1] == "jpeg" and raw.startswith(b"\xff\xd8\xff"))
                 or (match[1] == "webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"))
    if not raw or len(raw) > MAX_IMAGE_BYTES or not signature:
        raise ValueError("invalid image")
    return value


def text_record(value, secrets=()):
    """Project SDK messages to replayable roles; omit reasoning and images."""
    if not isinstance(value, dict) or value.get("role") not in {"user", "assistant", "tool"}:
        raise ValueError("invalid replay record")
    role, content = value["role"], value.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("invalid replay content")
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "image_url":
                parts.append("[Image attached to this prior turn; pixels are not retained.]")
            else:
                raise ValueError("invalid replay content")
        content = "\n".join(parts)
    if content is None and role == "assistant":
        content = ""
    if not isinstance(content, str):
        raise ValueError("invalid replay content")
    def clean(text):
        for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_RECORD_TEXT:
            text = encoded[:MAX_RECORD_TEXT].decode("utf-8", errors="ignore") + "\n[History text omitted at the host replay limit.]"
        return text
    record = {"role": role, "content": clean(content)}
    if role == "tool":
        if (not isinstance(value.get("tool_call_id"), str) or not 1 <= len(value["tool_call_id"]) <= 200
                or value.get("name") not in TOOL_NAMES):
            raise ValueError("invalid tool result")
        record.update(tool_call_id=value["tool_call_id"], name=value["name"])
    if "tool_calls" in value:
        calls = value["tool_calls"]
        if role != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 32:
            raise ValueError("invalid tool calls")
        projected = []
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if (not isinstance(call, dict) or call.get("type") != "function"
                    or not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 200
                    or not isinstance(function, dict) or function.get("name") not in TOOL_NAMES
                    or not isinstance(function.get("arguments"), str)):
                raise ValueError("invalid tool call")
            arguments = clean(function["arguments"])
            if len(arguments.encode("utf-8")) > 16_000 // len(calls):
                arguments = '{"_history_arguments_omitted":true}'
            projected.append({"id": call["id"], "type": "function",
                              "function": {"name": function["name"], "arguments": arguments}})
        record["tool_calls"] = projected
    return record


def bounded_history(messages):
    """Drop whole oldest user turns so native tool call/result pairs stay valid."""
    records = list(messages)
    starts = [i for i, item in enumerate(records) if item["role"] == "user"]
    if len(starts) > MAX_HISTORY_TURNS:
        records = records[starts[-MAX_HISTORY_TURNS]:]
    while len(json.dumps(records, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES:
        following = next((i for i, item in enumerate(records[1:], 1) if item["role"] == "user"), None)
        if following is None:
            # A single exceptionally long tool turn is explicitly omitted,
            # rather than constructing orphan tool results.
            return [{"role": "user", "content": records[0]["content"]},
                    {"role": "assistant", "content": "[Prior turn exceeded the native replay limit; inspect actual state before continuing. No actions are authorized by this summary.]"}]
        records = records[following:]
    return records


def close_interrupted_tools(messages):
    """Make interrupted turns valid replay without retrying unknown outcomes."""
    records = list(messages)
    pending = {}
    for item in records:
        if item["role"] == "assistant":
            for call in item.get("tool_calls", []):
                pending[call["id"]] = call["function"]["name"]
        elif item["role"] == "tool":
            pending.pop(item["tool_call_id"], None)
    for identifier, name in pending.items():
        records.append({"role": "tool", "tool_call_id": identifier, "name": name,
                        "content": "INTERRUPTED: Outcome was not received. Some side effects may already exist. Do not repeat automatically; inspect actual state and follow only the new user turn."})
    return records
