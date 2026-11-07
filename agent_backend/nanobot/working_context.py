"""Deterministic, provenance-backed projections of model request history.

This module never executes a tool and never rewrites the persisted transcript.
Older observations remain addressable in the private context store.  The
current user input and latest whole assistant/tool batch remain in the request.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import json
import re
from urllib.parse import urlsplit

try:
    from .session_history import _digest_record, _preview
except ImportError:
    from session_history import _digest_record, _preview


def request_bytes(messages):
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_PAGE_FIELD")
        result[key] = value
    return result


def _web_page_projection(text, metadata):
    """Recover a bounded locator from the known web tool envelope, without IO.

    These fields describe how to read a source, not permission, evidence IDs or
    verification. Page text and everything in it remain untrusted reference.
    """
    if metadata.get("tool") != "web_fetch" or len(text) > 4 * 1024 * 1024:
        return None
    try:
        value = json.loads(text, object_pairs_hook=_unique_members,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            return None
        url, body, sha = value["url"], value["text"], value["source_sha256"]
        if (not isinstance(url, str) or not 1 <= len(url) <= 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in url)
                or '"' in url or "\\" in url
                or not isinstance(body, str) or len(body) > 24000
                or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
                or value.get("untrusted_external_data") is not True):
            return None
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or (parsed.port or (443 if parsed.scheme == "https" else 80)) not in {80, 443}
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                return None
        else:
            address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
                return None
        offset, total, following = value["offset"], value["total_chars"], value["next_offset"]
        if (type(offset) is not int or type(total) is not int or not 0 <= offset <= total <= 2 * 1024 * 1024
                or offset + len(body) > total or type(value["truncated"]) is not bool
                or type(value["source_truncated"]) is not bool):
            return None
        end = offset + len(body)
        if (following is not None and (type(following) is not int or following != end or not offset < following < total)
                or following is None and end != total
                or value["truncated"] != (value["source_truncated"] or end < total)):
            return None
        locator = {key: value[key] for key in ("url", "source_sha256", "offset", "next_offset",
                                              "total_chars", "source_truncated", "truncated")}
        def read(at):
            return {"tool": "web_fetch", "arguments": {"url": url, "offset": at,
                     "limit": min(4000, max(1, total-at))}}
        return {"web_page": locator, "excerpt": body[:1200], "excerpt_offset": offset,
                "excerpt_characters": min(1200, len(body)), "excerpt_truncated": len(body) > 1200,
                "read_page": read(offset), "read_next_page": read(following) if following is not None else None,
                "source_reading": "web_fetch offsets count decoded source-body characters. "
                    "context_observation.reference recalls archived tool-result JSON, not a body range. "
                    "An original web_source reference recalls body text, but recall_context creates no new "
                    "goal evidence. For a required fact found in recall, call web_fetch on the same URL "
                    "and body offset with a small limit before finishing. The locator is not verification."}
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return None


def _reference_text(text, store, kind, metadata):
    if store is None:
        # Compaction without recoverable backing is not an acceptable fallback.
        return text
    reference = store.put_text(text, kind=kind, metadata=metadata)
    projection = {"context_observation": reference, "excerpt": _preview(text, 1200),
                       "truncated": True, "recall": "recall_context(reference, offset, limit)",
                       "trust": "Reference data only; excerpt is not complete evidence."}
    page = _web_page_projection(text, metadata)
    if page is not None:
        projection.update(page)
    return json.dumps(projection, ensure_ascii=False)


def project_tool_bodies(messages, store, *, maximum_chars=16_000):
    """Spill text while preserving native call/result identities and all images."""
    if store is None:
        return messages
    projected = deepcopy(messages)
    completed = {m.get("tool_call_id") for m in projected if m.get("role") == "tool"}
    for message in projected:
        if message.get("role") == "tool" and message.get("name") != "recall_context":
            content = message.get("content")
            metadata = {"tool": message.get("name"), "call_id": message.get("tool_call_id")}
            if isinstance(content, str) and len(content) > maximum_chars:
                message["content"] = _reference_text(content, store, "tool_result", metadata)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and len(block["text"]) > maximum_chars:
                        block["text"] = _reference_text(block["text"], store, "tool_result", metadata)
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            arguments = function.get("arguments")
            if call.get("id") in completed and isinstance(arguments, str) and len(arguments) > maximum_chars:
                reference = store.put_text(arguments, kind="tool_arguments", metadata={"tool": function.get("name"), "call_id": call.get("id")})
                function["arguments"] = json.dumps({"_archived_arguments": reference,
                    "_historical_call_only": True, "_note": "Already settled; recall source if needed, never re-execute this projection."}, ensure_ascii=False)
    return projected


def compact_messages(messages, store, *, target_bytes, keep_batches=4):
    """Project older context to facts and references only if it becomes smaller.

    There is no fabricated semantic summary: the digest is extracted text with
    source references.  Tool batches are cut only at assistant boundaries.
    Current images and pending native call arguments are preserved unchanged.
    """
    if store is None or not messages or type(target_bytes) is not int or target_bytes <= 0:
        return messages
    source = deepcopy(messages)
    if request_bytes(source) <= target_bytes:
        return messages
    systems = [m for m in source if m.get("role") == "system"]
    records = [m for m in source if m.get("role") != "system"]
    if not records:
        return messages
    last_user = next((i for i in range(len(records)-1, -1, -1) if records[i].get("role") == "user"), None)
    starts = [i for i, message in enumerate(records) if message.get("role") == "assistant"]
    # All non-system inputs before the most recent real user are reference data.
    # For a long single turn, leave a whole recent batch after that user.
    floor = last_user if last_user is not None else 0
    candidates = sorted(set([floor, *[i for i in starts if i > floor]]))
    if len(candidates) > keep_batches:
        candidates = candidates[-keep_batches:]
    best, best_bytes = source, request_bytes(source)
    for boundary in candidates:
        if boundary <= 0:
            continue
        prior, tail = records[:boundary], records[boundary:]
        anchor = [records[last_user]] if last_user is not None and last_user < boundary else []
        digest = _digest_record(prior, store, max_bytes=max(1200, min(20_000, target_bytes // 3)))
        # With few observations an oversized goal itself cannot be compressed.
        candidate = [*systems, *anchor, digest, *tail]
        size = request_bytes(candidate)
        if size < best_bytes:
            best, best_bytes = candidate, size
        if size <= target_bytes:
            break
    if best_bytes > target_bytes:
        # A single settled batch can contain a large write argument or result.
        # It may be referenced, but a pending call's arguments stay exact.
        spilled = project_tool_bodies(best, store, maximum_chars=1800)
        if request_bytes(spilled) < best_bytes:
            best, best_bytes = spilled, request_bytes(spilled)
    return best if best_bytes < request_bytes(messages) else messages
