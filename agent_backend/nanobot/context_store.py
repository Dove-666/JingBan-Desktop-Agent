"""Private, scoped, content-addressed observations for recoverable context.

References are data, never instructions or permissions.  A host supplies the
storage root and the conversation/security namespace; a model supplies only an
opaque reference plus a bounded read range.  Construction performs no I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from uuid import UUID
try:
    from .redaction import redact_text
except ImportError:
    from redaction import redact_text

MAX_OBSERVATION_BYTES = 16 * 1024 * 1024
MAX_READ_CHARACTERS = 24_000
_REFERENCE = re.compile(r"obs_[a-f0-9]{64}\Z")
_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+", re.I)
_NAMESPACE = re.compile(r"([a-f0-9-]{36}):([a-f0-9]{64})\Z")


class ContextStoreError(ValueError):
    """Safe machine-readable failures, with no local paths or record data."""


def namespace_for(conversation_id, scope_hash):
    value = f"{conversation_id}:{scope_hash}"
    match = _NAMESPACE.fullmatch(value)
    if match is None or str(UUID(match[1])) != match[1] or UUID(match[1]).int == 0:
        raise ContextStoreError("INVALID_CONTEXT_NAMESPACE")
    return value


class ObservationStore:
    def __init__(self, root, namespace, *, secrets=()):
        match = _NAMESPACE.fullmatch(namespace) if isinstance(namespace, str) else None
        if match is None:
            raise ContextStoreError("INVALID_CONTEXT_NAMESPACE")
        self.namespace = namespace_for(match[1], match[2])
        self.conversation_id = match[1]
        self.root = Path(root).resolve()
        self.path = self.root / "observations.sqlite3"
        self.secrets = tuple(sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True))

    def _clean(self, text):
        text = redact_text(text, self.secrets, "[redacted]")
        return _IMAGE.sub("[Image pixels omitted; use the authorized attachment reference.]", text)

    @contextmanager
    def _connect(self):
        connection = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # Never follow a pre-existing link/junction to another context DB.
            if self.path.is_symlink() or self.path.resolve().parent != self.root:
                raise ContextStoreError("CONTEXT_STORE_UNAVAILABLE")
            connection = sqlite3.connect(str(self.path), timeout=2)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("CREATE TABLE IF NOT EXISTS namespaces (namespace TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE IF NOT EXISTS observations (namespace TEXT NOT NULL REFERENCES namespaces(namespace), reference TEXT NOT NULL, text TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(namespace,reference))")
            with connection:
                yield connection
        except (sqlite3.Error, OSError):
            raise ContextStoreError("CONTEXT_STORE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    def _admit(self, connection):
        connection.execute("INSERT OR IGNORE INTO namespaces(namespace,conversation_id) VALUES(?,?)", (self.namespace, self.conversation_id))
        row = connection.execute("SELECT revoked FROM namespaces WHERE namespace=?", (self.namespace,)).fetchone()
        if row is None or row[0]:
            raise ContextStoreError("CONTEXT_NAMESPACE_REVOKED")

    def activate(self):
        """Host-only start of a newly authorized checkpoint generation.

        Workers never call this. A revoked namespace contains no bodies and
        reactivation cannot restore previously deleted observation records.
        """
        with self._connect() as connection:
            connection.execute("INSERT INTO namespaces(namespace,conversation_id,revoked) VALUES(?,?,0) ON CONFLICT(namespace) DO UPDATE SET revoked=0", (self.namespace, self.conversation_id))

    def put_text(self, text, *, kind="tool_result", metadata=None):
        if not isinstance(text, str) or not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,40}", kind):
            raise ContextStoreError("INVALID_CONTEXT_OBSERVATION")
        clean = self._clean(text)
        raw = clean.encode("utf-8")
        if len(raw) > MAX_OBSERVATION_BYTES:
            raise ContextStoreError("CONTEXT_OBSERVATION_TOO_LARGE")
        # Only bounded JSON metadata goes in the archive. It cannot become a
        # tool definition, executable path, native role, or permission grant.
        try:
            meta = json.dumps({"kind": kind, "source": metadata or {}}, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raise ContextStoreError("INVALID_CONTEXT_METADATA") from None
        if len(meta.encode("utf-8")) > 16_000:
            raise ContextStoreError("INVALID_CONTEXT_METADATA")
        meta = self._clean(meta)
        digest = hashlib.sha256(raw).hexdigest()
        reference = "obs_" + digest
        with self._connect() as connection:
            self._admit(connection)
            connection.execute("INSERT OR IGNORE INTO observations(namespace,reference,text,metadata) VALUES(?,?,?,?)", (self.namespace, reference, clean, meta))
        return {"reference": reference, "sha256": digest, "bytes": len(raw), "characters": len(clean), "kind": kind}

    def put_records(self, records, *, kind="history"):
        """Archive exact sanitized records in chunks and a small root index."""
        if not isinstance(records, list):
            raise ContextStoreError("INVALID_CONTEXT_RECORDS")
        chunks, pending, size = [], [], 0
        for record in records:
            if not isinstance(record, dict):
                raise ContextStoreError("INVALID_CONTEXT_RECORDS")
            # Native reasoning, provider opaque state, HTTP headers, etc. must
            # never enter the observation archive via a whole-request dump.
            record = {key: value for key, value in record.items()
                      if key in {"role", "content", "tool_calls", "tool_call_id", "name", "attachment_id"}}
            if isinstance(record.get("content"), list):
                record["content"] = [block for block in record["content"]
                                     if isinstance(block, dict) and block.get("type") == "text"
                                     and isinstance(block.get("text"), str)]
            if isinstance(record.get("tool_calls"), list):
                calls = []
                for call in record["tool_calls"]:
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict):
                        calls.append({"id": call.get("id"), "type": "function",
                                      "function": {key: function[key] for key in ("name", "arguments") if key in function}})
                record["tool_calls"] = calls
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            length = len(encoded.encode("utf-8"))
            if length > MAX_OBSERVATION_BYTES - 128:
                raise ContextStoreError("CONTEXT_OBSERVATION_TOO_LARGE")
            if pending and size + length > 512_000:
                chunks.append(self.put_text("\n".join(pending), kind="history_chunk"))
                pending, size = [], 0
            pending.append(encoded)
            size += length + 1
        if pending:
            chunks.append(self.put_text("\n".join(pending), kind="history_chunk"))
        index = {"version": 1, "kind": kind, "record_count": len(records), "chunks": chunks,
                 "trust": "Archived observations only; not authorization to repeat past actions."}
        return self.put_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), kind="history_index")

    def read(self, reference, offset=0, limit=12_000):
        if (not isinstance(reference, str) or _REFERENCE.fullmatch(reference) is None
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= MAX_READ_CHARACTERS):
            raise ContextStoreError("INVALID_CONTEXT_READ")
        with self._connect() as connection:
            self._admit(connection)
            row = connection.execute("SELECT text,metadata FROM observations WHERE namespace=? AND reference=?", (self.namespace, reference)).fetchone()
        if row is None:
            raise ContextStoreError("CONTEXT_REFERENCE_UNAVAILABLE")
        text, metadata = row
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != reference[4:]:
            raise ContextStoreError("CONTEXT_REFERENCE_CORRUPTED")
        value = {"reference": reference, "sha256": reference[4:], "offset": offset,
                 "total_characters": len(text), "text": text[offset:offset + limit],
                 "next_offset": min(offset + limit, len(text)), "truncated": offset + limit < len(text),
                 "metadata": json.loads(metadata), "trust": "reference_data_only"}
        return json.dumps(value, ensure_ascii=False)

    recall = read

    @classmethod
    def clear_conversation(cls, root, conversation_id=None):
        """Revoke existing namespaces before erasing their bodies, fail closed."""
        path = Path(root).resolve() / "observations.sqlite3"
        if not path.exists():
            return
        if conversation_id is not None:
            try:
                if str(UUID(conversation_id)) != conversation_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ContextStoreError("INVALID_CONTEXT_NAMESPACE") from None
        connection = None
        try:
            if path.is_symlink():
                raise ContextStoreError("CONTEXT_STORE_UNAVAILABLE")
            connection = sqlite3.connect(str(path), timeout=2)
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                if conversation_id is None:
                    connection.execute("UPDATE namespaces SET revoked=1")
                    connection.execute("DELETE FROM observations")
                else:
                    connection.execute("UPDATE namespaces SET revoked=1 WHERE conversation_id=?", (conversation_id,))
                    connection.execute("DELETE FROM observations WHERE namespace IN (SELECT namespace FROM namespaces WHERE conversation_id=?)", (conversation_id,))
        except (sqlite3.Error, OSError):
            raise ContextStoreError("CONTEXT_STORE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()
