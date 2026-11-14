"""Append-only, redacted execution events; UI and checkpoints remain projections."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from desktop_pet.redaction import redact_value


class ExecutionJournal:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=1)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS agent_events_by_task ON agent_events(task_id,seq)")
            db.execute("CREATE INDEX IF NOT EXISTS agent_events_by_conversation ON agent_events(conversation_id,seq)")
            with db:
                yield db
        finally:
            db.close()

    def append(self, conversation_id, task_id, kind, data, *, secrets=()):
        if not isinstance(kind, str) or kind not in {
                "task_started", "message", "tool_activity", "checkpoint", "interaction",
                "answer", "steering", "diagnostic", "task_finished"}:
            raise ValueError("INVALID_EXECUTION_EVENT")
        encoded = json.dumps(redact_value(data, secrets, "[已隐藏密钥]"),
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 128_000:
            raise ValueError("EXECUTION_EVENT_TOO_LARGE")
        with self._connect() as db:
            db.execute("INSERT INTO agent_events(conversation_id,task_id,kind,data,created_at) VALUES(?,?,?,?,?)",
                       (conversation_id, task_id, kind, encoded,
                        datetime.now(timezone.utc).isoformat(timespec="milliseconds")))

    def read(self, conversation_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("INVALID_EXECUTION_PAGE")
        with self._connect() as db:
            rows = db.execute("SELECT seq,task_id,kind,data,created_at FROM agent_events WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (conversation_id, after, limit)).fetchall()
        return [{"seq": row[0], "task_id": row[1], "kind": row[2],
                 "data": json.loads(row[3]), "created_at": row[4]} for row in rows]

    def clear(self, conversation_id=None):
        if not self.path.exists():
            return
        with self._connect() as db:
            if conversation_id is None:
                db.execute("DELETE FROM agent_events")
            else:
                db.execute("DELETE FROM agent_events WHERE conversation_id=?", (conversation_id,))
