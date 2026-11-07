"""Bounded, host-fed observations for independent goal verification.

This is an evidence projection, not a permission system or a model-visible tool.
Only successful real tool callbacks belong here. Plans, finish summaries and
the candidate answer are never execution evidence. All recorded content remains
untrusted data and must not be interpreted as judge instructions.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from redaction import redact_value
from tool_catalog import CATALOG

NON_EVIDENCE_TOOLS = frozenset({"update_plan", "finish_task", "ask_user_question",
                                "request_file_access", "recall_context"})
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILE_WRITERS = frozenset({"write_file", "edit_file", "create_document", "write_document", "render_document", "compute_project_artifact"})


def _file_write_witness(name, source):
    """Keep the small actual write receipt when large arguments are clipped.

    This is derived only from a successful host tool result, never from a path
    in planner arguments or prose. It proves a versioned write, not correctness
    of omitted content or the whole task.
    """
    if name not in _FILE_WRITERS:
        return None
    value = source.get("result")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return None
    if not isinstance(value, dict):
        return None
    path, sha, size = value.get("written"), value.get("sha256"), value.get("bytes")
    if (not isinstance(path, str) or not 1 <= len(path) <= 4096
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or type(size) is not int or not 0 <= size <= 16 * 1024 * 1024):
        return None
    return {"source": "host_successful_file_write", "path": path, "sha256": sha, "bytes": size}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class GoalEvidence:
    """Real observations with immutable IDs, bounded previews and source hashes.

    The host supplies known secrets. A successful tool result is not proof of
    relevance to the user's task; that is decided by deterministic requirements
    and the independent judge. Dropped records and clipped text are explicit.
    """

    def __init__(self, *, secrets=(), max_records=64, max_characters=131072):
        if (type(max_records) is not int or not 1 <= max_records <= 256
                or type(max_characters) is not int or not 4096 <= max_characters <= 1048576):
            raise ValueError("INVALID_GOAL_EVIDENCE_LIMIT")
        if isinstance(secrets, str):
            secrets = (secrets,)
        if not isinstance(secrets, (list, tuple)) or any(not isinstance(value, str) for value in secrets):
            raise ValueError("INVALID_GOAL_EVIDENCE_SECRETS")
        self._secrets = tuple(secrets)
        self._max_records = max_records
        self._max_characters = max_characters
        self._records = []
        self._sequence = 0
        self._dropped = 0
        self._effect_count = 0
        self._characters = 0
        self._chain = digest([])

    def observe(self, name, args, result, *, effect=None):
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(args, dict):
            raise ValueError("INVALID_GOAL_OBSERVATION")
        if name in NON_EVIDENCE_TOOLS:
            return None
        if name == "compute_project_artifact":
            if __package__: from .project_runtime import published_artifact
            else: from project_runtime import published_artifact
            if published_artifact(result) is None:
                raise ValueError("PROJECT_PUBLICATION_RECEIPT_REQUIRED")
            # Host authority is checked before projecting ordinary immutable
            # text. SDK/history deep copies must never receive the capability.
            result = str(result)
        if not isinstance(result, (str, dict, list, bool, int, float, type(None))):
            raise ValueError("INVALID_GOAL_OBSERVATION")
        spec = CATALOG.get(name)
        actual_effect = spec.effect if spec is not None else "unknown"
        # A caller may supply an effect for a non-catalog tool. Known tools can
        # never be relabelled read-only to make answer_only admissible.
        if effect is not None:
            if effect not in {"none", "file", "browser", "desktop", "process", "unknown"}:
                raise ValueError("INVALID_GOAL_OBSERVATION_EFFECT")
            actual_effect = actual_effect if spec is not None else effect
        try:
            source = redact_value({"arguments": args, "result": result}, self._secrets)
            encoded = canonical_json(source)
        except (TypeError, ValueError, RecursionError, OverflowError):
            raise ValueError("INVALID_GOAL_OBSERVATION") from None
        self._sequence += 1
        self._effect_count += actual_effect != "none"
        source_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self._chain = digest({"previous": self._chain, "sequence": self._sequence,
                              "tool": name, "effect": actual_effect, "source_sha256": source_hash})
        # Bound individual observations as well as the complete projection. Full
        # hashes bind the receipt even when a judge sees only a marked preview.
        limit = min(24000, self._max_characters // 2)
        clipped = len(encoded) > limit
        record = {"id": f"tool-{self._sequence}-{source_hash[:16]}", "tool": name,
                  "effect": actual_effect, "source_sha256": source_hash,
                  "source_characters": len(encoded), "truncated": clipped}
        witness = _file_write_witness(name, source)
        if witness is not None:
            record["file_write"] = witness
        if clipped:
            record["preview"] = encoded[:limit]
        else:
            record["observation"] = source
        size = len(canonical_json(record))
        self._records.append(record)
        self._characters += size
        while len(self._records) > self._max_records or self._characters > self._max_characters:
            self._characters -= len(canonical_json(self._records.pop(0)))
            self._dropped += 1
        return record["id"]

    @property
    def has_effects(self):
        return self._effect_count > 0

    def redact(self, value):
        """Use the same host-known secret projection for file-readback facts."""
        return redact_value(value, self._secrets)

    def snapshot(self):
        return {"schema": "aura.goal_evidence.v1", "sequence": self._sequence,
                "chain_sha256": self._chain, "effect_count": self._effect_count,
                "dropped_records": self._dropped, "observations": deepcopy(self._records)}
