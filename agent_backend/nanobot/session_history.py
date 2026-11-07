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

try:
    from .tool_catalog import TOOL_NAMES
    from .redaction import redact_text
except ImportError:
    from tool_catalog import TOOL_NAMES
    from redaction import redact_text

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_HISTORY_BYTES = 256_000
MAX_HISTORY_TURNS = 12
MAX_RECORD_TEXT = 24_000  # UTF-8 bytes, so each protocol frame stays bounded.
_DIGEST_PREFIX = "[Host archived task facts; reference data, not permission to repeat actions.]\n"
MAX_DIGEST_BYTES = 20_000
_UNAVAILABLE_CALL_NOTE = "[Host rejected an unavailable tool call without execution. Use only advertised tools; this note is reference data, not an executable call.]"


def native_text_record(value, *, available_tools, secrets=()):
    """Archive native SDK rejection as data, keeping known call identities.

    This is an output projection, not a relaxed imported-history validator.
    Unknown results are omitted because their rejection is already recorded on
    the assistant message. Emitting an intervening assistant message could break
    an otherwise valid mixed batch's pending known assistant/tool pairing.
    """
    if not isinstance(value, dict):
        raise ValueError("invalid replay record")
    available = frozenset(available_tools)
    if not available <= TOOL_NAMES:
        raise ValueError("invalid host tool names")
    if value.get("role") == "tool" and value.get("name") not in available:
        return None
    if value.get("role") != "assistant" or not value.get("tool_calls"):
        return text_record(value, secrets)
    calls = value["tool_calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 32:
        raise ValueError("invalid tool calls")
    known = []
    unavailable = False
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("invalid tool call")
        if function["name"] in available:
            known.append(call)
        else:
            unavailable = True
    projected = dict(value)
    if known:
        projected["tool_calls"] = known
    else:
        projected.pop("tool_calls", None)
    record = text_record(projected, secrets)
    if unavailable:
        record["content"] = record["content"] + ("\n" if record["content"] else "") + _UNAVAILABLE_CALL_NOTE
    return record


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
                attachment = block.get("attachment_id") or value.get("attachment_id")
                suffix = (f" Authorized attachment reference: {attachment}."
                          if isinstance(attachment, str) and re.fullmatch(r"[a-f0-9]{64}", attachment) else "")
                parts.append("[Image attached to this prior turn; pixels are not retained." + suffix + "]")
            else:
                raise ValueError("invalid replay content")
        content = "\n".join(parts)
    if content is None and role == "assistant":
        content = ""
    if not isinstance(content, str):
        raise ValueError("invalid replay content")
    def clean(text):
        text = redact_text(text, secrets, "[redacted]")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_RECORD_TEXT:
            text = encoded[:MAX_RECORD_TEXT].decode("utf-8", errors="ignore") + "\n[History text omitted at the host replay limit.]"
        return text
    record = {"role": role, "content": clean(content)}
    attachment = value.get("attachment_id")
    if isinstance(attachment, str) and re.fullmatch(r"[a-f0-9]{64}", attachment):
        # Keep a host-issued content identity, never an arbitrary source path.
        record["attachment_id"] = attachment
        if attachment not in record["content"]:
            record["content"] += f"\n[Authorized prior attachment: {attachment}; inspect on demand, do not attach to a new user message.]"
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


def _preview(text, limit=700):
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[... reference excerpt omitted ...]\n" + text[-limit // 2:]


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _digest_record(records, store=None, *, max_bytes=MAX_DIGEST_BYTES):
    """Extract factual excerpts and traceable source IDs, never model reasoning."""
    facts, prior = [], []
    for record in records:
        text = record.get("content", "")
        if isinstance(text, list):
            text = "\n".join(block["text"] if block.get("type") == "text" else
                             "[Prior image pixels omitted; inspect an authorized attachment reference on demand.]"
                             for block in text if isinstance(block, dict)
                             and (block.get("type") == "image_url" or
                                  block.get("type") == "text" and isinstance(block.get("text"), str)))
        if not isinstance(text, str):
            text = ""
        if record.get("role") == "assistant" and text.startswith(_DIGEST_PREFIX):
            try:
                old = json.loads(text[len(_DIGEST_PREFIX):])
                facts.extend(old.get("facts", []))
                prior.extend(old.get("sources", []))
            except (ValueError, TypeError):
                facts.append({"role": "assistant", "excerpt": _preview(text)})
            continue
        fact = {"role": record.get("role"), "excerpt": _preview(text)}
        if record.get("tool_calls"):
            fact["calls"] = [{"id": call["id"], "name": call["function"]["name"],
                              "arguments_excerpt": _preview(call["function"]["arguments"], 400)}
                             for call in record["tool_calls"]]
        if record.get("role") == "tool":
            fact.update(tool=record.get("name"), call_id=record.get("tool_call_id"),
                        outcome="unknown" if text.startswith("INTERRUPTED:") else "observed")
        if record.get("attachment_id"):
            fact["attachment_id"] = record["attachment_id"]
        facts.append(fact)
    source = store.put_records(records) if store is not None and records else None
    sources = list(dict.fromkeys([*prior, *([source["reference"]] if source else [])]))
    digest = {"version": 1, "facts": facts, "sources": sources,
              "scope": "Factual excerpts only. Recall sources for exact observations; inspect actual state before further actions."}
    # The index of an older digest itself remains recallable, so bounded hot
    # context does not imply its omitted facts have been deleted from storage.
    if _bytes(digest) > max_bytes and store is not None:
        index = store.put_text(json.dumps(digest, ensure_ascii=False, separators=(",", ":")), kind="history_digest")
        sources = [index["reference"]]
        digest["sources"] = sources
    omitted = 0
    while _bytes(digest) > max_bytes and len(digest["facts"]) > 1:
        # Retain earliest goal and latest confirmed facts plus an explicit count.
        del digest["facts"][1]
        omitted += 1
    if omitted:
        digest["excerpted_fact_count"] = omitted
        digest["omitted_facts_recallable"] = store is not None
    if _bytes(digest) > max_bytes:
        digest["facts"] = [{"role": fact.get("role"), "excerpt": _preview(fact.get("excerpt", ""), 300),
                            **({"calls": [{"id": call.get("id"), "name": call.get("name")} for call in fact["calls"][:4]],
                                "additional_calls_omitted": max(0, len(fact["calls"])-4)} if fact.get("calls") else {})}
                           for fact in digest["facts"][:1]]
    return {"role": "assistant", "content": _DIGEST_PREFIX + json.dumps(digest, ensure_ascii=False, separators=(",", ":"))}


def bounded_history(messages, *, store=None):
    """Keep a bounded valid replay plus provenance-backed, recallable facts.

    The latest user message and pending assistant/tool pairs are never replaced
    with a pretend successful summary. Persistent callers provide an
    ObservationStore; transient tests may use the clearly marked excerpts.
    """
    records = [dict(record) for record in messages]
    if not records:
        return []
    removed = []
    starts = [i for i, item in enumerate(records) if item["role"] == "user"]
    if len(starts) > MAX_HISTORY_TURNS:
        boundary = starts[-MAX_HISTORY_TURNS]
        removed, records = records[:boundary], records[boundary:]
    replay_limit = MAX_HISTORY_BYTES - MAX_DIGEST_BYTES - 1024
    while _bytes(records) > replay_limit:
        following = next((i for i, item in enumerate(records[1:], 1) if item["role"] == "user"), None)
        if following is None:
            break
        removed += records[:following]
        records = records[following:]
    # An exceptionally long current turn keeps complete native call identities.
    # Large tool bodies become typed references, rather than dropping the turn.
    if _bytes(records) > replay_limit:
        for record in records:
            if record.get("role") == "tool" and len(record.get("content", "")) > 1200:
                original = record["content"]
                ref = store.put_text(original, kind="tool_result", metadata={"name": record.get("name"), "call_id": record.get("tool_call_id")}) if store is not None else None
                record["content"] = json.dumps({"observation": ref, "excerpt": _preview(original, 1000),
                    "truncated": True, "recallable": ref is not None,
                    "scope": "Reference data, not proof of success or authority to repeat."}, ensure_ascii=False)
    if _bytes(records) > replay_limit:
        # Bound completed older batches while retaining the latest complete
        # assistant batch and its tool results. Pending intent also has the
        # host's separate durable frontier.
        anchor = next((record for record in reversed(records) if record["role"] == "user"), None)
        cut = next((i for i, record in enumerate(records) if i > 0 and record["role"] == "assistant"
                    and _bytes(records[i:]) < MAX_HISTORY_BYTES - MAX_DIGEST_BYTES - MAX_RECORD_TEXT), None)
        if cut is not None:
            removed += records[:cut]
            records = ([anchor] if anchor is not None else []) + records[cut:]
    if removed:
        digest = _digest_record(removed, store)
        # Keep the user-first shape while placing old facts before actual work.
        if records and records[0]["role"] == "user":
            records.insert(1, digest)
        else:
            records.insert(0, digest)
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
