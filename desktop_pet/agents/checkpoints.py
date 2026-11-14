"""Durable, bounded native Agent checkpoints, separate from chat and Mem0.

Only host-projected text is stored. Model thoughts, screenshot bytes and keys
never belong here. A checkpoint commits before its acknowledgement authorizes
the worker to dispatch the next model request or top-level tool body.

The semantic flush ordering follows DeepSeek Harness's MIT-licensed
session-checkpoint-policy; see third_party/deepseek_harness/README.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4

from agent_backend.nanobot.session_history import (
    MAX_HISTORY_BYTES, bounded_history, close_interrupted_tools, conversation_uuid, text_record,
)
from agent_backend.nanobot.context_store import ObservationStore, namespace_for
from agent_backend.nanobot.redaction import redact_text
from agent_backend.nanobot.goal_history import GoalHistoryError, checked_goal_history, legacy_goal_history


SCHEMA_VERSION = 3
MAX_FRONTIER_BYTES = 64_000
STATUSES = frozenset({"running", "completed", "verified", "unverified", "partial",
                      "needs_user", "failed", "cancelled", "timeout", "interrupted",
                      "verification_failed", "blocked"})
_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+", re.I)
_RECOVERY_NOTE = (
    "[Host checkpoint recovery: the previous process ended during a task. "
    "Completed side effects may remain and pending outcomes are unknown. "
    "Inspect actual state before continuing; do not repeat actions automatically. "
    "Only the latest user message authorizes new actions.]"
)


class CheckpointError(RuntimeError):
    """Do not reflect SQLite paths, driver exceptions, data or credentials."""

    def __init__(self):
        super().__init__("任务检查点无法可靠读写；本次任务未继续派发，请检查本地数据目录后重试。")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def scope_fingerprint(settings, api, *, secrets=(), generation="", permissions=None):
    """Bind facts to permissions/workspace/account, independently of model route.

    The digest is the only persisted identity. Keys are hashed as part of this
    high-entropy identity rather than retained as separate credential fields.
    Changing provider/account/permissions cannot silently reuse native tools.
    """
    if generation is not None and not isinstance(generation, str):
        raise ValueError("invalid conversation generation")
    if generation:
        conversation_uuid(generation)
    public = asdict(settings) if is_dataclass(settings) else dict(settings)
    api = asdict(api) if is_dataclass(api) else dict(api)
    source = {
        "version": 2,
        "workspace": os.path.normcase(str(Path(public.get("workspace") or ".").resolve())),
        "backend": public.get("backend"),
        "tools_enabled": public.get("tools_enabled"),
        "computer_enabled": public.get("computer_enabled"),
        "gui_base_url": str(public.get("gui_base_url", "")).rstrip("/"),
        "api_base_url": str(api.get("base_url", "")).rstrip("/"),
        "conversation_generation": generation or "",
        "resource_permissions": permissions,
        "credential_identities": [hashlib.sha256(value.encode("utf-8")).hexdigest()
                                  for value in secrets if isinstance(value, str) and value],
    }
    execution_identity = api.get("project_execution_identity", "")
    if (not isinstance(execution_identity, str) or execution_identity
            and re.fullmatch(r"[a-f0-9]{64}", execution_identity) is None):
        raise ValueError("invalid project execution identity")
    if execution_identity:
        source["project_execution_identity"] = execution_identity
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def legacy_scope_fingerprint(settings, api, *, secrets=(), generation=""):
    """Exact pre-upgrade identity, only for a same-configuration migration.

    The host may supply this to load; changing key/endpoint/permissions still
    makes the old digest differ. Unknown old routes are never guessed.
    """
    public = asdict(settings) if is_dataclass(settings) else dict(settings)
    api = asdict(api) if is_dataclass(api) else dict(api)
    source = {"version": 1,
              "workspace": os.path.normcase(str(Path(public.get("workspace") or ".").resolve())),
              "backend": public.get("backend"), "tools_enabled": public.get("tools_enabled"),
              "computer_enabled": public.get("computer_enabled"), "gui_provider": public.get("gui_provider"),
              "gui_coordinate_profile": public.get("gui_coordinate_profile"),
              "gui_base_url": str(public.get("gui_base_url", "")).rstrip("/"), "gui_model": public.get("gui_model"),
              "api_base_url": str(api.get("base_url", "")).rstrip("/"), "api_model": api.get("model"),
              "conversation_generation": generation or "",
              "credential_identities": [hashlib.sha256(value.encode("utf-8")).hexdigest()
                                        for value in secrets if isinstance(value, str) and value]}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _project_records(messages, secrets=()):
    """Project before applying a replay budget so pending intent is visible."""
    if not isinstance(messages, list):
        raise ValueError("invalid history")

    def clean(value):
        if isinstance(value, str):
            value = redact_text(value, secrets, "[redacted]")
            return _IMAGE.sub("[Image payload not retained.]", value)
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return [text_record(clean(value), secrets) for value in messages]


def sanitized_history(messages, secrets=(), *, store=None):
    """Drop non-replay fields and binary data before a single SQLite write."""
    return bounded_history(close_interrupted_tools(_project_records(messages, secrets)), store=store)


def durable_frontier(messages, history, secrets=()):
    """Retain current tool intent even when whole-turn replay is omitted.

    This is a safety record, not another model history budget. At most one
    native assistant batch (32 calls, 16 KB projected arguments) is outstanding.
    Unknown tool outcomes keep their intent until the next user-authorized turn.
    """
    records = _project_records(messages, secrets)
    start = next((index for index in range(len(records) - 1, -1, -1)
                  if records[index]["role"] == "user"), 0)
    current = records[start:]
    pending = {}
    for record in current:
        if record["role"] == "assistant":
            for call in record.get("tool_calls", []):
                previous = pending.get(call["id"])
                if previous is not None and previous != call:
                    raise ValueError("ambiguous pending tool identity")
                pending[call["id"]] = call
        elif record["role"] == "tool":
            # This marker is host-generated for an uncertain side effect. It
            # closes the model wire pair, not the durable execution intent.
            if not record["content"].startswith("INTERRUPTED: Outcome was not received."):
                pending.pop(record["tool_call_id"], None)
    if len(pending) > 32:
        raise ValueError("pending frontier too large")
    replay_calls = {call["id"]: call for record in history if record["role"] == "assistant"
                    for call in record.get("tool_calls", [])}
    omitted = len(json.dumps(close_interrupted_tools(current), ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES
    frontier = {"version": 1, "pending_calls": list(pending.values()), "history_omitted": omitted,
                "pending_replay_omitted": any(replay_calls.get(identifier) != call for identifier, call in pending.items())}
    encoded = json.dumps(frontier, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_FRONTIER_BYTES:
        raise ValueError("pending frontier exceeds durable limit")
    return frontier


def _frontier_recovery_note(task_id, frontier):
    if not frontier.get("history_omitted") and not frontier.get("pending_replay_omitted"):
        return ""
    pending = frontier.get("pending_calls", [])
    calls = ", ".join(f"{call['function']['name']} ({call['id']})" for call in pending)
    return (f"[Host durable frontier for task {task_id}: native replay omitted part of the current turn. "
            + (f"Recorded dispatched-or-pending tools: {calls}. Their outcomes may be unknown. " if calls
               else "Earlier tool outcomes may no longer be present in replay. ")
            + "Inspect actual state before taking further actions; do not repeat automatically. "
              "Only the latest user message authorizes continuation.]")


class CheckpointStore:
    """Short-lived connections; constructing a store performs no I/O."""

    def __init__(self, path):
        self.path = Path(path)
        self.owner = uuid4().hex
        self._lock = threading.RLock()

    @property
    def context_root(self):
        return self.path.parent / "agent_context"

    def observation_store(self, conversation_id, scope_hash, *, secrets=()):
        self._identity(conversation_id, scope_hash)
        return ObservationStore(self.context_root, namespace_for(conversation_id, scope_hash), secrets=secrets)

    @property
    def _deletion_directory(self):
        return self.path.with_name(self.path.name + ".deletions")

    def _record_deletion(self, conversation_id):
        """A failed SQLite erase remains invalid after a process restart.

        Each accepted clear has its own marker, avoiding a later clear being
        removed by an earlier transaction. The host's chat generation remains
        the final isolation layer if the entire directory is unwritable.
        """
        try:
            directory = self._deletion_directory
            directory.mkdir(parents=True, exist_ok=True)
            stem = "all" if conversation_id is None else conversation_id
            marker = directory / (stem + "_" + uuid4().hex + ".delete")
            with marker.open("xb") as stream:
                stream.write(b"1")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise CheckpointError() from None

    def _pending_deletions(self):
        directory = self._deletion_directory
        if not directory.exists():
            return []
        markers = []
        for marker in directory.iterdir():
            found = re.fullmatch(r"(all|[a-f0-9-]{36})_[a-f0-9]{32}\.delete", marker.name)
            if found:
                cid = None if found[1] == "all" else conversation_uuid(found[1])
                markers.append((marker, cid))
        return markers

    @contextmanager
    def _connect(self):
        connection = None
        applied_deletions = []
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(self.path), timeout=0.5)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA secure_delete=ON")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, SCHEMA_VERSION):
                    raise ValueError("unsupported checkpoint schema")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    conversation_id TEXT PRIMARY KEY,
                    scope_hash TEXT NOT NULL,
                    history_json TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    last_barrier INTEGER NOT NULL DEFAULT 0,
                    goal_verified INTEGER NOT NULL DEFAULT 0,
                    frontier_json TEXT NOT NULL DEFAULT '{}',
                    goal_history_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )""")
                connection.execute("""CREATE TABLE IF NOT EXISTS agent_task_checkpoints (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES agent_checkpoints(conversation_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    last_barrier INTEGER NOT NULL DEFAULT 0,
                    goal_verified INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                connection.execute("CREATE INDEX IF NOT EXISTS agent_task_by_conversation ON agent_task_checkpoints(conversation_id, updated_at DESC)")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_checkpoints)")}
                if "frontier_json" not in columns and version < 2:
                    connection.execute("ALTER TABLE agent_checkpoints ADD COLUMN frontier_json TEXT NOT NULL DEFAULT '{}'")
                    columns.add("frontier_json")
                if "goal_history_json" not in columns and version < 3:
                    connection.execute("ALTER TABLE agent_checkpoints ADD COLUMN goal_history_json TEXT NOT NULL DEFAULT '[]'")
                    columns.add("goal_history_json")
                if not {"conversation_id", "scope_hash", "history_json", "task_id", "status", "owner",
                        "last_barrier", "goal_verified", "frontier_json", "goal_history_json", "updated_at"}.issubset(columns):
                    raise ValueError("invalid checkpoint schema")
                if version < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                # Apply accepted erase intents before any history can be read
                # or a later task begins. Rollback leaves the marker in place.
                applied_deletions = self._pending_deletions()
                for _, conversation_id in applied_deletions:
                    ObservationStore.clear_conversation(self.context_root, conversation_id)
                    if conversation_id is None:
                        connection.execute("DELETE FROM agent_checkpoints")
                    else:
                        connection.execute("DELETE FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,))
                yield connection
                connection.commit()
                for marker, _ in applied_deletions:
                    try:
                        marker.unlink()
                    except FileNotFoundError:
                        pass
                    # Any other unlink failure is fail-closed. Keeping an erase
                    # marker is safer than later restoring an erased session.
            except GoalHistoryError:
                if connection is not None:
                    connection.rollback()
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError):
                if connection is not None:
                    connection.rollback()
                raise CheckpointError() from None
            finally:
                if connection is not None:
                    connection.close()

    @staticmethod
    def _identity(conversation_id, scope_hash, task_id=None):
        conversation_uuid(conversation_id)
        if not isinstance(scope_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", scope_hash):
            raise ValueError("invalid scope")
        if task_id is not None and (not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id)):
            raise ValueError("invalid task id")

    def load(self, conversation_id, scope_hash, *, secrets=(), legacy_scope_hash=None):
        self._identity(conversation_id, scope_hash)
        with self._connect() as db:
            row = db.execute("SELECT * FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None:
                return None
            if row["scope_hash"] != scope_hash and row["scope_hash"] == legacy_scope_hash:
                # Trusted host computed the exact prior scope using the same
                # credentials/permissions; no model-provided migration token.
                db.execute("UPDATE agent_checkpoints SET scope_hash=? WHERE conversation_id=?", (scope_hash, conversation_id))
                row = db.execute("SELECT * FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row["scope_hash"] != scope_hash:
                return {"history": [], "status": "scope_changed", "scope_matches": False,
                        "goal_history": [],
                        "goal_verified": False, "recovered_running": False}
            try:
                self._identity(conversation_id, row["scope_hash"], row["task_id"])
                if (row["status"] not in STATUSES or not isinstance(row["owner"], str)
                        or not re.fullmatch(r"[a-f0-9]{32}", row["owner"])
                        or type(row["last_barrier"]) is not int or row["last_barrier"] < 0
                        or row["goal_verified"] not in (0, 1)
                        or not isinstance(row["history_json"], str)
                        or len(row["history_json"].encode("utf-8")) > MAX_HISTORY_BYTES + 4096
                        or not isinstance(row["frontier_json"], str)):
                    raise ValueError("invalid checkpoint row")
                raw = json.loads(row["history_json"])
                history = sanitized_history(raw, secrets, store=self.observation_store(conversation_id, scope_hash, secrets=secrets))
                if not isinstance(row["goal_history_json"], str) or len(row["goal_history_json"]) > 700000:
                    raise ValueError("invalid goal history")
                goals = checked_goal_history(json.loads(row["goal_history_json"]))
                if not goals:
                    goals = legacy_goal_history(raw)
                if len(row["frontier_json"].encode("utf-8")) > MAX_FRONTIER_BYTES:
                    raise ValueError("invalid frontier")
                frontier = json.loads(row["frontier_json"])
                if not isinstance(frontier, dict):
                    raise ValueError("invalid frontier")
                pending = frontier.get("pending_calls", [])
                if not isinstance(pending, list) or len(pending) > 32:
                    raise ValueError("invalid frontier")
                if pending:
                    # Revalidate every call against the same native whitelist;
                    # never turn untrusted SQLite fields into tool invocations.
                    pending = _project_records([{"role": "assistant", "content": "", "tool_calls": pending}], secrets)[0]["tool_calls"]
                    frontier["pending_calls"] = pending
            except GoalHistoryError:
                raise
            except (ValueError, TypeError, RecursionError):
                raise CheckpointError() from None
            recovered = row["status"] == "running" and row["owner"] != self.owner
            status = row["status"]
            frontier_note = _frontier_recovery_note(row["task_id"], frontier)
            if frontier_note and not any(record.get("content") == frontier_note for record in history):
                history = bounded_history(history + [{"role": "assistant", "content": frontier_note}])
            if recovered:
                history = bounded_history(history + [{"role": "assistant", "content": _RECOVERY_NOTE}])
                status = "interrupted"
                db.execute("UPDATE agent_checkpoints SET history_json=?,status=?,goal_verified=0,updated_at=? WHERE conversation_id=?",
                           (json.dumps(history, ensure_ascii=False), status, _now(), conversation_id))
                db.execute("UPDATE agent_task_checkpoints SET status='interrupted',goal_verified=0,updated_at=? WHERE task_id=? AND status='running'",
                           (_now(), row["task_id"]))
            return {"history": history, "goal_history": goals, "status": status, "scope_matches": True,
                    "task_id": row["task_id"], "last_barrier": row["last_barrier"],
                    # Legacy "completed" describes control flow alone. Keep
                    # the explicit evidence bit, and never revive it for an
                    # interrupted/cancelled/pending checkpoint.
                    "goal_verified": bool(row["goal_verified"]) and not recovered
                        and status in {"verified", "completed"} and not pending,
                    "updated_at": row["updated_at"], "recovered_running": recovered,
                    "pending_tool_count": len(pending), "history_omitted": bool(frontier.get("history_omitted"))}

    def begin(self, conversation_id, scope_hash, task_id, history, *, secrets=(), goal_history=None):
        self._identity(conversation_id, scope_hash, task_id)
        now = _now()
        with self._connect() as db:
            observations = self.observation_store(conversation_id, scope_hash, secrets=secrets)
            observations.activate()
            safe_history = sanitized_history(history, secrets, store=observations)
            encoded = json.dumps(safe_history, ensure_ascii=False)
            frontier = json.dumps(durable_frontier(history, safe_history, secrets), ensure_ascii=False)
            goals = checked_goal_history(legacy_goal_history(_project_records(history, secrets))
                                         if goal_history is None else goal_history)
            goal_json = json.dumps([_IMAGE.sub("[Image payload not retained.]", redact_text(item, secrets, "[redacted]"))
                                    for item in goals], ensure_ascii=False)
            previous = db.execute("SELECT scope_hash,task_id,status FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,)).fetchone()
            if previous is not None and previous["scope_hash"] != scope_hash:
                db.execute("DELETE FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,))
            elif previous is not None and previous["status"] == "running":
                db.execute("UPDATE agent_task_checkpoints SET status='interrupted',goal_verified=0,updated_at=? WHERE task_id=?",
                           (now, previous["task_id"]))
            db.execute("""INSERT INTO agent_checkpoints(conversation_id,scope_hash,history_json,task_id,status,owner,last_barrier,goal_verified,frontier_json,goal_history_json,updated_at)
                VALUES(?,?,?,?,?,?,0,0,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                scope_hash=excluded.scope_hash,history_json=excluded.history_json,task_id=excluded.task_id,
                status=excluded.status,owner=excluded.owner,last_barrier=0,goal_verified=0,
                frontier_json=excluded.frontier_json,goal_history_json=excluded.goal_history_json,updated_at=excluded.updated_at""",
                       (conversation_id, scope_hash, encoded, task_id, "running", self.owner, frontier, goal_json, now))
            db.execute("INSERT INTO agent_task_checkpoints(task_id,conversation_id,status,started_at,updated_at) VALUES(?,?,?,?,?)",
                       (task_id, conversation_id, "running", now, now))
            # Keep bounded metadata; full messages remain the separate chat DB.
            db.execute("DELETE FROM agent_task_checkpoints WHERE conversation_id=? AND task_id NOT IN (SELECT task_id FROM agent_task_checkpoints WHERE conversation_id=? ORDER BY updated_at DESC LIMIT 32)",
                       (conversation_id, conversation_id))

    def save(self, conversation_id, scope_hash, task_id, history, *, status="running", barrier=None,
             goal_verified=False, secrets=(), goal_history=None):
        self._identity(conversation_id, scope_hash, task_id)
        if status not in STATUSES or type(goal_verified) is not bool:
            raise ValueError("invalid checkpoint status")
        if barrier is not None and (type(barrier) is not int or not 1 <= barrier <= 999_999_999):
            raise ValueError("invalid barrier")
        if goal_verified and status not in {"verified", "completed"}:
            raise ValueError("invalid verification status")
        safe_history = sanitized_history(history, secrets, store=self.observation_store(conversation_id, scope_hash, secrets=secrets))
        encoded = json.dumps(safe_history, ensure_ascii=False)
        frontier_data = durable_frontier(history, safe_history, secrets)
        frontier = json.dumps(frontier_data, ensure_ascii=False)
        if goal_verified and frontier_data["pending_calls"]:
            raise ValueError("pending actions cannot be goal verified")
        if len(encoded.encode("utf-8")) > MAX_HISTORY_BYTES + 4096:
            raise ValueError("history too large")
        now = _now()
        goal_json = None if goal_history is None else json.dumps(
            [_IMAGE.sub("[Image payload not retained.]", redact_text(item, secrets, "[redacted]"))
             for item in checked_goal_history(goal_history)], ensure_ascii=False)
        with self._connect() as db:
            row = db.execute("SELECT task_id,scope_hash,last_barrier,owner FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None or row["task_id"] != task_id or row["scope_hash"] != scope_hash or row["owner"] != self.owner:
                raise CheckpointError()
            if barrier is not None and barrier <= row["last_barrier"]:
                raise ValueError("non-monotonic barrier")
            sequence = row["last_barrier"] if barrier is None else barrier
            db.execute("UPDATE agent_checkpoints SET history_json=?,frontier_json=?,goal_history_json=COALESCE(?,goal_history_json),status=?,last_barrier=?,goal_verified=?,updated_at=? WHERE conversation_id=?",
                       (encoded, frontier, goal_json, status, sequence, int(goal_verified), now, conversation_id))
            db.execute("UPDATE agent_task_checkpoints SET status=?,last_barrier=?,goal_verified=?,updated_at=? WHERE task_id=?",
                       (status, sequence, int(goal_verified), now, task_id))

    def clear(self, conversation_id=None):
        if conversation_id is not None:
            conversation_uuid(conversation_id)
        self._record_deletion(conversation_id)
        with self._connect() as db:
            if conversation_id is None:
                db.execute("DELETE FROM agent_checkpoints")
            else:
                db.execute("DELETE FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,))
