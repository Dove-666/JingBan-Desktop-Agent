"""SQLite conversation archive; synchronous methods belong in Qt workers.

Every operation owns a connection and transaction. No GUI, network request,
screen image, credential, or Mem0 implementation belongs in this module.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4

from .chronology import register_timestamp_order
from ..chat_images import IMAGE_ERRORS
from ..agents.service import _ERROR_MESSAGES as AGENT_ERRORS
from ..execution_trace import normalize_execution, merge_execution, interrupted_execution
from ..agent_presentation import AGENT_ACTIVITY, public_agent_trace_sql


SCHEMA_VERSION = 3
REMOTE_SYNC_SCHEMA_VERSION = 7
REMOTE_SYNC_MAX_CHANGES = 10000
REMOTE_SYNC_MAX_CHANGE_BYTES = 32 * 1024 * 1024
REMOTE_SYNC_PRUNE_BATCH = 512


def maintain_remote_sync(connection: sqlite3.Connection) -> None:
    """Trim only the derived journal, using trigger-maintained O(1) counters.

    Removing a batch makes compaction infrequent under normal chat traffic.
    The high watermark survives an empty journal; floor is the last removed
    sequence and marks exactly which clients must obtain a fresh snapshot.
    All maintenance stays in the caller's write transaction.
    """
    state = connection.execute("SELECT * FROM remote_sync_state WHERE id = 1").fetchone()
    over_count = state["entry_count"] > REMOTE_SYNC_MAX_CHANGES
    over_bytes = state["payload_bytes"] > REMOTE_SYNC_MAX_CHANGE_BYTES
    if not over_count and not over_bytes:
        return
    target_count = max(0, REMOTE_SYNC_MAX_CHANGES - REMOTE_SYNC_PRUNE_BATCH)
    target_bytes = max(0, REMOTE_SYNC_MAX_CHANGE_BYTES - REMOTE_SYNC_MAX_CHANGE_BYTES // 8)
    remove_count = state["entry_count"] - target_count if over_count else 0
    remove_bytes = state["payload_bytes"] - target_bytes if over_bytes else 0
    count, size, cutoff = 0, 0, 0
    for row in connection.execute("""SELECT seq, length(CAST(payload AS BLOB)) AS size
            FROM remote_sync_changes ORDER BY seq"""):
        cutoff = row["seq"]
        count += 1
        size += row["size"]
        if count >= remove_count and size >= remove_bytes:
            break
    if cutoff:
        connection.execute("DELETE FROM remote_sync_changes WHERE seq <= ?", (cutoff,))


def remote_sync_projection(entity: str, prefix: str = "") -> str:
    """SQL JSON projection shared by the change journal and initial snapshot.

    This is an explicit public-field list, never a serialized database row.
    In particular, observation metadata, local paths and private summaries do
    not belong on the remote protocol. ``entity`` and ``prefix`` are internal
    constants, not values accepted from the network.
    """
    column = lambda name: prefix + name
    if entity == "conversation":
        fields = ("id", "title", "created_at", "updated_at", "message_count")
    elif entity == "screen_event":
        fields = ("id", "conversation_id", "observed_at", "source_kind")
    elif entity == "message":
        fields = ("id", "conversation_id", "role", "content", "created_at", "source")
    else:
        raise ValueError("Unknown synchronization entity")
    pairs = [f"'{name}', {column(name)}" for name in fields]
    if entity == "message":
        metadata = column("metadata")
        status = f"json_extract({metadata}, '$.request_status')"
        reply = f"json_extract({metadata}, '$.reply_to')"
        # CASE must guard json_extract itself: older damaged metadata must not
        # make otherwise unrelated local archive writes start failing.
        pairs.append("'request_status', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + status + " IN ('pending','failed','complete','cancelled','interrupted') THEN "
                     + status + " END END")
        pairs.append("'reply_to', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + f"json_type({metadata}, '$.reply_to') = 'text' AND "
                     + f"EXISTS (SELECT 1 FROM messages AS reply_user WHERE reply_user.id = {reply} "
                     + f"AND reply_user.conversation_id = {column('conversation_id')} "
                     + "AND reply_user.role = 'user') THEN " + reply + " END END")
        image = f"json_extract({metadata}, '$.image_id')"
        pairs.append("'image_id', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + f"{column('role')} = 'user' AND json_type({metadata}, '$.image_id') = 'text' "
                     + f"AND length({image}) = 64 AND {image} NOT GLOB '*[^0-9a-f]*' "
                     + "THEN " + image + " END END")
        image_error = f"json_extract({metadata}, '$.image_error_code')"
        codes = ",".join("'" + code + "'" for code in IMAGE_ERRORS)
        pairs.append("'image_error', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + f"{column('role')} = 'user' AND {status} = 'failed' AND {image_error} IN ({codes}) "
                     + "THEN " + image_error + " END END")
        mode = f"json_extract({metadata}, '$.execution_mode')"
        pairs.append("'execution_mode', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + mode + " = 'agent' THEN 'agent' END END")
        agent_error = f"json_extract({metadata}, '$.agent_error_code')"
        error_codes = ",".join("'" + code + "'" for code in AGENT_ERRORS)
        pairs.append("'agent_error_code', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + status + " IN ('failed','cancelled','interrupted') AND " + agent_error
                     + f" IN ({error_codes}) THEN " + agent_error + " END END")
        labels = ",".join("'" + label.replace("'", "''") + "'" for label in sorted(AGENT_ACTIVITY))
        pairs.append("'agent_activity', CASE WHEN json_valid(" + metadata + ") THEN CASE WHEN "
                     + status + " = 'pending' THEN (SELECT json_extract(value, '$.label') FROM json_each("
                     + metadata + ", '$.execution.events') WHERE "
                     + f"CASE WHEN type = 'object' THEN json_extract(value, '$.label') END IN ({labels}) "
                     + "ORDER BY CAST(key AS INTEGER) DESC LIMIT 1) END END")
        pairs.append("'agent_trace', json(" + public_agent_trace_sql(metadata, column("role"), mode) + ")")
    return "json_object(" + ", ".join(pairs) + ")"


class StoreError(RuntimeError):
    """Static errors safe to show without SQLite paths or message contents."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise StoreError("会话或消息标识无效。")
    if any(ord(char) < 32 for char in value):
        raise StoreError("会话或消息标识无效。")
    return value


def _text(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise StoreError("消息或会话文本不能为空。")
    return value


def _metadata(value: dict | None) -> str:
    if value is not None and not isinstance(value, dict):
        raise StoreError("消息附加信息格式无效。")
    try:
        return json.dumps(value or {}, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise StoreError("消息附加信息格式无效。") from None


def _decoded(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    if "metadata" in result:
        try:
            metadata = json.loads(result["metadata"])
        except (TypeError, ValueError):
            raise StoreError("聊天记录附加信息损坏，请保留数据库后重试。") from None
        if not isinstance(metadata, dict):
            raise StoreError("聊天记录附加信息损坏，请保留数据库后重试。")
        result["metadata"] = metadata
    for key in ("title_generated", "title_manual"):
        if key in result:
            result[key] = bool(result[key])
    return result


class ConversationStore:
    """Explicit conversation IDs prevent cross-session writes and lookups.

    Construction does no disk I/O. Call ``initialize`` from a worker before
    use. Initializing is also lazy for callers that only perform one operation.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connection(self, *, initialize: bool = True) -> Iterator[sqlite3.Connection]:
        if initialize and not self._initialized:
            self.initialize()
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=5.0)
            connection.row_factory = sqlite3.Row
            register_timestamp_order(connection)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            with connection:
                yield connection
                if connection.total_changes:
                    maintain_remote_sync(connection)
        except sqlite3.Error:
            raise StoreError("聊天数据库操作失败，请检查本地存储空间和文件权限。") from None
        except OSError:
            raise StoreError("无法访问聊天数据库，请检查本地存储路径和权限。") from None
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise StoreError("无法创建聊天数据库目录，请检查本地存储权限。") from None
            with self._connection(initialize=False) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise StoreError("聊天数据库来自更新版本，请升级程序后再打开。")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                if version < 1:
                    self._migrate_v1(connection)
                    connection.execute("PRAGMA user_version = 1")
                if version < 2:
                    self._migrate_v2(connection)
                    connection.execute("PRAGMA user_version = 2")
                if version < 3:
                    self._migrate_v3(connection)
                    connection.execute("PRAGMA user_version = 3")
                self._migrate_remote_sync(connection)
            # Only advertise initialization after the migration commits.
            self._initialized = True

    @staticmethod
    def _migrate_remote_sync(connection: sqlite3.Connection) -> None:
        """Add a separately versioned, host-owned synchronization journal.

        Keep the archive's public user_version at v3: its existing schema and
        older tooling remain compatible. All remote additions are additive and
        are versioned independently, in the same migration transaction.
        """
        connection.execute("""CREATE TABLE IF NOT EXISTS remote_sync_schema (
            id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL)""")
        row = connection.execute("SELECT version FROM remote_sync_schema WHERE id = 1").fetchone()
        version = row[0] if row is not None else 0
        if version > REMOTE_SYNC_SCHEMA_VERSION:
            raise StoreError("远程同步数据库来自更新版本，请升级程序后再打开。")
        if version == REMOTE_SYNC_SCHEMA_VERSION:
            return
        if version in (1, 2, 3, 4, 5, 6):
            if version == 1:
                ConversationStore._migrate_remote_retention(connection)
            ConversationStore._migrate_remote_image_refs(connection)
            return
        statements = (
            """CREATE TABLE remote_sync_changes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
                payload TEXT NOT NULL)""",
            """CREATE TABLE remote_sync_tombstones (
                entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, PRIMARY KEY(entity, entity_id))""",
            """CREATE TABLE remote_sync_snapshots (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, watermark INTEGER NOT NULL)""",
            """CREATE TABLE remote_sync_snapshot_items (
                snapshot_id TEXT NOT NULL REFERENCES remote_sync_snapshots(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(snapshot_id, ordinal))""",
            """CREATE TABLE remote_sync_operations (
                device_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','complete','expired')),
                result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                expires_at REAL, ephemeral INTEGER NOT NULL CHECK(ephemeral IN (0,1)),
                PRIMARY KEY(device_id, request_id))""",
        )
        for statement in statements:
            connection.execute(statement)
        for entity, table in (("conversation", "conversations"), ("message", "messages"),
                              ("screen_event", "screen_events")):
            cid = "NEW.id" if entity == "conversation" else "NEW.conversation_id"
            old_cid = "OLD.id" if entity == "conversation" else "OLD.conversation_id"
            projection = remote_sync_projection(entity, "NEW.")
            # Tombstones outlive clear/delete. A retried old operation cannot
            # recreate a deleted message or a deleted conversation identifier.
            connection.execute(f"""CREATE TRIGGER remote_sync_{entity}_no_resurrection
                BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM remote_sync_tombstones
                    WHERE entity = '{entity}' AND entity_id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'deleted synchronization identifier'); END""")
            for operation in ("INSERT", "UPDATE"):
                connection.execute(f"""CREATE TRIGGER remote_sync_{entity}_{operation.lower()}
                    AFTER {operation} ON {table}
                    BEGIN INSERT INTO remote_sync_changes
                        (entity, entity_id, conversation_id, deleted, payload)
                        VALUES ('{entity}', NEW.id, {cid}, 0, {projection}); END""")
            connection.execute(f"""CREATE TRIGGER remote_sync_{entity}_delete
                AFTER DELETE ON {table}
                BEGIN
                    INSERT OR IGNORE INTO remote_sync_tombstones(entity, entity_id, conversation_id)
                        VALUES ('{entity}', OLD.id, {old_cid});
                    INSERT INTO remote_sync_changes(entity, entity_id, conversation_id, deleted, payload)
                        VALUES ('{entity}', OLD.id, {old_cid}, 1, '{{}}');
                END""")
        connection.execute("INSERT INTO remote_sync_schema(id, version) VALUES (1, 1)")
        ConversationStore._migrate_remote_retention(connection)
        ConversationStore._migrate_remote_image_refs(connection)

    @staticmethod
    def _migrate_remote_retention(connection: sqlite3.Connection) -> None:
        """v2: bounded journal accounting, without resetting change sequence."""
        # v1 cached up to three larger snapshots. They are derived data: drop
        # them on this upgrade so the tighter bound applies immediately.
        connection.execute("DELETE FROM remote_sync_snapshots")
        connection.execute("""CREATE TABLE remote_sync_state (
            id INTEGER PRIMARY KEY CHECK(id = 1), floor_seq INTEGER NOT NULL,
            high_seq INTEGER NOT NULL, entry_count INTEGER NOT NULL,
            payload_bytes INTEGER NOT NULL)""")
        # This aggregate runs only once during migration, not on every write.
        connection.execute("""INSERT INTO remote_sync_state
            (id, floor_seq, high_seq, entry_count, payload_bytes)
            SELECT 1,
                coalesce(min(seq) - 1, (SELECT coalesce(max(seq), 0) FROM sqlite_sequence
                    WHERE name = 'remote_sync_changes')),
                max(coalesce(max(seq), 0), (SELECT coalesce(max(seq), 0) FROM sqlite_sequence
                    WHERE name = 'remote_sync_changes')),
                count(*), coalesce(sum(length(CAST(payload AS BLOB))), 0)
            FROM remote_sync_changes""")
        connection.execute("""CREATE TRIGGER remote_sync_journal_added
            AFTER INSERT ON remote_sync_changes BEGIN
                UPDATE remote_sync_state SET high_seq = max(high_seq, NEW.seq),
                    entry_count = entry_count + 1,
                    payload_bytes = payload_bytes + length(CAST(NEW.payload AS BLOB)) WHERE id = 1;
            END""")
        connection.execute("""CREATE TRIGGER remote_sync_journal_removed
            AFTER DELETE ON remote_sync_changes BEGIN
                UPDATE remote_sync_state SET floor_seq = max(floor_seq, OLD.seq),
                    entry_count = max(0, entry_count - 1),
                    payload_bytes = max(0, payload_bytes - length(CAST(OLD.payload AS BLOB))) WHERE id = 1;
            END""")
        for entity, table in (("conversation", "conversations"), ("message", "messages"),
                              ("screen_event", "screen_events")):
            cid = "NEW.id" if entity == "conversation" else "NEW.conversation_id"
            before = remote_sync_projection(entity, "OLD.")
            after = remote_sync_projection(entity, "NEW.")
            # A private summary/title-generator flag update does not warrant
            # another public record. Request status/reply changes still do.
            connection.execute(f"DROP TRIGGER remote_sync_{entity}_update")
            connection.execute(f"""CREATE TRIGGER remote_sync_{entity}_update
                AFTER UPDATE ON {table} WHEN {before} IS NOT {after}
                BEGIN INSERT INTO remote_sync_changes
                    (entity, entity_id, conversation_id, deleted, payload)
                    VALUES ('{entity}', NEW.id, {cid}, 0, {after}); END""")
        connection.execute("UPDATE remote_sync_schema SET version = ? WHERE id = 1",
                           (2,))

    @staticmethod
    def _migrate_remote_image_refs(connection: sqlite3.Connection) -> None:
        """Refresh message projection: image refs plus current safe Agent fields.

        v7 refreshes installed SQL triggers for the bounded public execution
        trace as well as current safe error/activity labels for paired peers.
        """
        connection.execute("DELETE FROM remote_sync_snapshots")
        after = remote_sync_projection("message", "NEW.")
        before = remote_sync_projection("message", "OLD.")
        for operation in ("insert", "update"):
            connection.execute(f"DROP TRIGGER remote_sync_message_{operation}")
            condition = f" WHEN {before} IS NOT {after}" if operation == "update" else ""
            connection.execute(f"""CREATE TRIGGER remote_sync_message_{operation}
                AFTER {operation.upper()} ON messages{condition}
                BEGIN INSERT INTO remote_sync_changes
                    (entity, entity_id, conversation_id, deleted, payload)
                    VALUES ('message', NEW.id, NEW.conversation_id, 0, {after}); END""")
        connection.execute("UPDATE remote_sync_schema SET version = ? WHERE id = 1",
                           (REMOTE_SYNC_SCHEMA_VERSION,))

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )""",
            """CREATE TABLE IF NOT EXISTS screen_events (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                observed_at TEXT NOT NULL, app_name TEXT NOT NULL DEFAULT '',
                context_summary TEXT NOT NULL, event_type TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(id, conversation_id)
            )""",
            """CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL CHECK(length(trim(content)) > 0),
                created_at TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('user', 'assistant', 'proactive_screen')),
                screen_event_id TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(screen_event_id, conversation_id)
                    REFERENCES screen_events(id, conversation_id) ON DELETE CASCADE,
                CHECK((role = 'user' AND source = 'user' AND screen_event_id IS NULL)
                    OR (role = 'assistant' AND source = 'assistant' AND screen_event_id IS NULL)
                    OR (role = 'assistant' AND source = 'proactive_screen'
                        AND screen_event_id IS NOT NULL))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_event ON messages(screen_event_id) WHERE screen_event_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_events_conversation ON screen_events(conversation_id, observed_at DESC)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        existing = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
        additions = {
            "title_generated": "INTEGER NOT NULL DEFAULT 0 CHECK(title_generated IN (0,1))",
            "title_manual": "INTEGER NOT NULL DEFAULT 0 CHECK(title_manual IN (0,1))",
            "summary_through_id": "TEXT",
            "message_count": "INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0)",
        }
        for name, declaration in additions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE conversations ADD COLUMN {name} {declaration}")
        connection.execute("""UPDATE conversations SET message_count =
            (SELECT count(*) FROM messages WHERE messages.conversation_id = conversations.id)""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS message_added_count AFTER INSERT ON messages
            BEGIN UPDATE conversations SET message_count = message_count + 1
                WHERE id = NEW.conversation_id; END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS message_removed_count AFTER DELETE ON messages
            BEGIN UPDATE conversations SET message_count = max(0, message_count - 1)
                WHERE id = OLD.conversation_id; END""")

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Keep legacy event IDs while separating camera and screen provenance."""
        connection.execute("""ALTER TABLE screen_events ADD COLUMN source_kind
            TEXT NOT NULL DEFAULT 'screen' CHECK(source_kind IN ('screen', 'camera'))""")
        # SQLite cannot widen a CHECK constraint in place. Copy explicit rowids
        # as well, since equal timestamps use rowid to retain message order.
        connection.execute("""CREATE TABLE messages_v3 (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            created_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('user', 'assistant', 'proactive_screen', 'proactive_camera')),
            screen_event_id TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(screen_event_id, conversation_id)
                REFERENCES screen_events(id, conversation_id) ON DELETE CASCADE,
            CHECK((role = 'user' AND source = 'user' AND screen_event_id IS NULL)
                OR (role = 'assistant' AND source = 'assistant' AND screen_event_id IS NULL)
                OR (role = 'assistant' AND source IN ('proactive_screen', 'proactive_camera')
                    AND screen_event_id IS NOT NULL))
        )""")
        connection.execute("""INSERT INTO messages_v3
            (rowid, id, conversation_id, role, content, created_at, source, screen_event_id, metadata)
            SELECT rowid, id, conversation_id, role, content, created_at, source, screen_event_id, metadata
            FROM messages""")
        connection.execute("DROP TRIGGER IF EXISTS message_added_count")
        connection.execute("DROP TRIGGER IF EXISTS message_removed_count")
        connection.execute("DROP TABLE messages")
        connection.execute("ALTER TABLE messages_v3 RENAME TO messages")
        connection.execute("CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at)")
        connection.execute("""CREATE UNIQUE INDEX idx_messages_event ON messages(screen_event_id)
            WHERE screen_event_id IS NOT NULL""")
        connection.execute("""CREATE TRIGGER message_added_count AFTER INSERT ON messages
            BEGIN UPDATE conversations SET message_count = message_count + 1
                WHERE id = NEW.conversation_id; END""")
        connection.execute("""CREATE TRIGGER message_removed_count AFTER DELETE ON messages
            BEGIN UPDATE conversations SET message_count = max(0, message_count - 1)
                WHERE id = OLD.conversation_id; END""")

    @staticmethod
    def _ensure_conversation(connection: sqlite3.Connection, conversation_id: str,
                             fallback_title: str, timestamp: str) -> None:
        title = " ".join(fallback_title.split())[:32] or "新对话"
        connection.execute("""INSERT OR IGNORE INTO conversations
            (id, title, summary, created_at, updated_at, status)
            VALUES (?, ?, '', ?, ?, 'active')""", (conversation_id, title, timestamp, timestamp))

    @staticmethod
    def _touch(connection: sqlite3.Connection, conversation_id: str, timestamp: str) -> None:
        # A delayed assistant result must not move updated_at backwards.
        connection.execute("""UPDATE conversations SET updated_at = CASE
            WHEN aura_timestamp(updated_at) = '~'
                OR aura_timestamp(?) > aura_timestamp(updated_at) THEN ?
            ELSE updated_at END WHERE id = ?""", (timestamp, timestamp, conversation_id))

    def list_conversations(self, query: str = "") -> list[dict]:
        _text(query, allow_empty=True)
        with self._connection() as connection:
            if query.strip():
                # Escape LIKE metacharacters so the UI performs a literal search.
                pattern = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = connection.execute("""SELECT c.*, (
                        SELECT m.id FROM messages m WHERE m.conversation_id = c.id
                        AND m.content LIKE ? ESCAPE '\\'
                        ORDER BY aura_timestamp(m.created_at) DESC, m.rowid DESC LIMIT 1
                    ) AS matched_message_id FROM conversations c
                    WHERE c.message_count > 0 AND (c.title LIKE ? ESCAPE '\\'
                        OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id
                                   AND m.content LIKE ? ESCAPE '\\'))
                    ORDER BY aura_timestamp(c.updated_at) DESC, c.id DESC""", (pattern, pattern, pattern)).fetchall()
            else:
                rows = connection.execute("""SELECT * FROM conversations WHERE message_count > 0
                    ORDER BY aura_timestamp(updated_at) DESC, id DESC""").fetchall()
            return [_decoded(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict | None:
        _identifier(conversation_id)
        with self._connection() as connection:
            return _decoded(connection.execute("SELECT * FROM conversations WHERE id = ?",
                                               (conversation_id,)).fetchone())

    def load_messages(self, conversation_id: str) -> list[dict]:
        _identifier(conversation_id)
        with self._connection() as connection:
            rows = connection.execute("""SELECT * FROM messages WHERE conversation_id = ?
                ORDER BY aura_timestamp(created_at) ASC, rowid ASC""", (conversation_id,)).fetchall()
            messages = [_decoded(row) for row in rows]
            events = {row["id"]: _decoded(row) for row in connection.execute(
                "SELECT * FROM screen_events WHERE conversation_id = ?", (conversation_id,))}
            for message in messages:
                event_id = message.get("screen_event_id")
                if event_id:
                    event = dict(events[event_id])
                    event["message_id"] = message["id"]
                    event["comment"] = message["content"]
                    message["screen_event"] = event
            return messages

    def append_message(self, conversation_id: str, role: str, content: str, source: str,
                       *, message_id: str | None = None, metadata: dict | None = None,
                       created_at: str | None = None) -> dict:
        _identifier(conversation_id)
        message_id = _identifier(message_id or str(uuid4()))
        _text(content)
        if (role, source) not in {("user", "user"), ("assistant", "assistant")}:
            raise StoreError("消息角色或来源无效，画面评论必须通过事件接口保存。")
        encoded = _metadata(metadata)
        timestamp = _text(created_at) if created_at is not None else _now()
        with self._connection() as connection:
            self._ensure_conversation(connection, conversation_id, content, timestamp)
            connection.execute("""INSERT INTO messages
                (id, conversation_id, role, content, created_at, source, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (message_id, conversation_id, role, content, timestamp, source, encoded))
            self._touch(connection, conversation_id, timestamp)
            return _decoded(connection.execute("SELECT * FROM messages WHERE id = ?",
                                               (message_id,)).fetchone())

    def append_screen_comment(self, conversation_id: str, content: str, *, event_id: str,
                              app_name: str, context_summary: str, event_type: str,
                              observed_at: str | None = None, metadata: dict | None = None,
                              message_id: str | None = None, source_kind: str = "screen",
                              created_at: str | None = None) -> dict:
        """Save a visual event; the legacy name/container also supports cameras."""
        if not isinstance(source_kind, str) or source_kind not in {"screen", "camera"}:
            raise StoreError("画面来源必须为屏幕或摄像头。")
        _identifier(conversation_id)
        _identifier(event_id)
        message_id = _identifier(message_id or str(uuid4()))
        _text(content)
        _text(app_name, allow_empty=True)
        _text(context_summary)
        _text(event_type)
        encoded = _metadata(metadata)
        timestamp = _text(observed_at) if observed_at is not None else _now()
        message_timestamp = _text(created_at) if created_at is not None else timestamp
        with self._connection() as connection:
            self._ensure_conversation(connection, conversation_id, context_summary, message_timestamp)
            connection.execute("""INSERT INTO screen_events
                (id, conversation_id, observed_at, app_name, context_summary, event_type, metadata, source_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (event_id, conversation_id, timestamp,
                app_name, context_summary, event_type, encoded, source_kind))
            connection.execute("""INSERT INTO messages
                (id, conversation_id, role, content, created_at, source, screen_event_id, metadata)
                VALUES (?, ?, 'assistant', ?, ?, ?, ?, '{}')""",
                (message_id, conversation_id, content, message_timestamp, "proactive_" + source_kind, event_id))
            self._touch(connection, conversation_id, message_timestamp)
            result = _decoded(connection.execute("SELECT * FROM messages WHERE id = ?",
                                                (message_id,)).fetchone())
            event = _decoded(connection.execute("SELECT * FROM screen_events WHERE id = ?",
                                               (event_id,)).fetchone())
            event["message_id"] = message_id
            event["comment"] = content
            result["screen_event"] = event
            return result

    def complete_request(self, conversation_id: str, user_message_id: str, content: str,
                         *, emotion: str = "neutral", execution=None) -> dict:
        """Save the assistant turn and complete its user turn in one transaction.

        A disk failure cannot leave a reply with a retryable user turn. Replaying
        a completed request returns its existing reply instead of duplicating it.
        """
        _identifier(conversation_id)
        _identifier(user_message_id)
        _text(content)
        if execution is not None:
            execution = normalize_execution(execution)
        timestamp, message_id = _now(), str(uuid4())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _decoded(connection.execute(
                "SELECT * FROM messages WHERE id = ? AND conversation_id = ? AND role = 'user'",
                (user_message_id, conversation_id)).fetchone())
            if user is None:
                raise StoreError("指定用户消息不存在，无法保存回复。")
            existing = connection.execute("""SELECT * FROM messages
                WHERE conversation_id = ? AND role = 'assistant' AND source = 'assistant'
                AND json_extract(metadata, '$.reply_to') = ? LIMIT 1""",
                (conversation_id, user_message_id)).fetchone()
            if existing is not None:
                user["metadata"].pop("image_error_code", None)
                connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                                   (_metadata(dict(user["metadata"], request_status="complete")),
                                    user_message_id, conversation_id))
                return _decoded(existing)
            metadata = dict(user["metadata"], request_status="complete")
            metadata.pop("image_error_code", None)
            metadata.pop("agent_error_code", None)
            if execution is not None:
                metadata["execution"] = merge_execution(metadata.get("execution"), execution)
            assistant_metadata = {"emotion": emotion, "reply_to": user_message_id}
            if metadata.get("execution_mode") == "agent":
                assistant_metadata["execution_mode"] = "agent"
            if isinstance(metadata.get("execution"), dict):
                assistant_metadata["execution"] = normalize_execution(metadata["execution"])
            connection.execute("""INSERT INTO messages
                (id, conversation_id, role, content, created_at, source, metadata)
                VALUES (?, ?, 'assistant', ?, ?, 'assistant', ?)""",
                (message_id, conversation_id, content, timestamp,
                  _metadata(assistant_metadata)))
            connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                               (_metadata(metadata), user_message_id, conversation_id))
            self._touch(connection, conversation_id, timestamp)
            return _decoded(connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())

    def get_screen_event(self, event_id: str, conversation_id: str) -> dict | None:
        _identifier(event_id)
        _identifier(conversation_id)
        with self._connection() as connection:
            return _decoded(connection.execute("""SELECT e.*, m.id AS message_id, m.content AS comment
                FROM screen_events e LEFT JOIN messages m ON m.screen_event_id = e.id
                    AND m.conversation_id = e.conversation_id
                WHERE e.id = ? AND e.conversation_id = ?""", (event_id, conversation_id)).fetchone())

    def recent_screen_events(self, conversation_id: str, query: str = "", limit: int = 3) -> list[dict]:
        _identifier(conversation_id)
        _text(query, allow_empty=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise StoreError("画面事件查询数量无效。")
        with self._connection() as connection:
            # Pull a small, current-session-only window. References such as “刚才”
            # have no lexical overlap; in that case return the newest events.
            rows = connection.execute("""SELECT e.*, m.id AS message_id, m.content AS comment
                FROM screen_events e JOIN messages m ON m.screen_event_id = e.id
                    AND m.conversation_id = e.conversation_id
                WHERE e.conversation_id = ? ORDER BY aura_timestamp(e.observed_at) DESC, e.rowid DESC LIMIT 30""",
                (conversation_id,)).fetchall()
            events = [_decoded(row) for row in rows]
            words = [word.casefold() for word in query.split() if len(word) >= 2]
            if words:
                scored = [(sum(word in (event["context_summary"] + " " + event["comment"]).casefold()
                               for word in words), index, event) for index, event in enumerate(events)]
                if any(score for score, _, _ in scored):
                    events = [event for _, _, event in sorted(scored, key=lambda item: (-item[0], item[1]))]
            return events[:limit]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        _identifier(conversation_id)
        _text(title)
        clean_title = " ".join(title.split())[:100]
        with self._connection() as connection:
            cursor = connection.execute("""UPDATE conversations SET title = ?, title_manual = 1,
                title_generated = 1 WHERE id = ?""", (clean_title, conversation_id))
            if not cursor.rowcount:
                raise StoreError("指定会话不存在，无法重命名。")

    def delete_conversation(self, conversation_id: str) -> None:
        _identifier(conversation_id)
        with self._connection() as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def clear_messages(self, conversation_id: str) -> None:
        _identifier(conversation_id)
        with self._connection() as connection:
            connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM screen_events WHERE conversation_id = ?", (conversation_id,))
            connection.execute("""UPDATE conversations SET summary = '', summary_through_id = NULL,
                title = CASE WHEN title_manual = 1 THEN title ELSE '新对话' END,
                title_generated = CASE WHEN title_manual = 1 THEN 1 ELSE 0 END,
                updated_at = ? WHERE id = ?""", (_now(), conversation_id))

    def update_metadata(self, conversation_id: str, *, title: str | None = None,
                        summary: str | None = None, summary_through_id: str | None = None,
                        guard_message_id: str | None = None) -> None:
        _identifier(conversation_id)
        if title is not None:
            _text(title)
        if summary is not None:
            _text(summary, allow_empty=True)
        if summary_through_id is not None:
            _identifier(summary_through_id)
        if guard_message_id is not None:
            _identifier(guard_message_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A title/overview job may finish after the user cleared this chat.
            # Only commit while the material it summarized still exists here.
            if guard_message_id is not None and not connection.execute(
                "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?",
                (guard_message_id, conversation_id)).fetchone():
                return
            if summary_through_id is not None and not connection.execute(
                "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?",
                (summary_through_id, conversation_id)).fetchone():
                raise StoreError("会话摘要对应的消息不属于该会话。")
            if title is not None:
                connection.execute("""UPDATE conversations SET title = ?, title_generated = 1
                    WHERE id = ? AND title_manual = 0 AND title_generated = 0""",
                    (" ".join(title.split())[:100], conversation_id))
            if summary is not None:
                connection.execute("""UPDATE conversations SET summary = ?, summary_through_id = ?
                    WHERE id = ?""", (summary, summary_through_id, conversation_id))

    def set_message_status(self, conversation_id: str, message_id: str, status: str, *,
                           image_error_code=None, agent_error_code=None, execution=None) -> None:
        """Keep failed attempts visible while excluding them from later context."""
        _identifier(conversation_id)
        _identifier(message_id)
        if status not in {"pending", "failed", "complete", "cancelled", "interrupted"}:
            raise StoreError("消息请求状态无效。")
        if image_error_code is not None and (status != "failed" or image_error_code not in IMAGE_ERRORS):
            raise StoreError("图片错误状态无效。")
        if agent_error_code is not None and (status not in {"failed", "cancelled", "interrupted"}
                                            or not isinstance(agent_error_code, str)
                                            or agent_error_code not in AGENT_ERRORS):
            raise StoreError("任务错误状态无效。")
        if execution is not None:
            execution = normalize_execution(execution)
        with self._connection() as connection:
            # Stop persistence and the finishing worker can update this row at
            # once. Lock before reading so neither overwrites newer metadata.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT metadata FROM messages WHERE id = ? AND conversation_id = ?",
                                     (message_id, conversation_id)).fetchone()
            if row is None:
                raise StoreError("指定消息不存在，无法更新请求状态。")
            try:
                metadata = json.loads(row["metadata"])
                metadata["request_status"] = status
                if image_error_code is not None:
                    metadata["image_error_code"] = image_error_code
                elif status != "failed":
                    metadata.pop("image_error_code", None)
                if agent_error_code is not None:
                    metadata["agent_error_code"] = agent_error_code
                elif status in {"pending", "complete"}:
                    metadata.pop("agent_error_code", None)
                if execution is not None:
                    metadata["execution"] = merge_execution(metadata.get("execution"), execution)
                encoded = _metadata(metadata)
            except (TypeError, ValueError):
                raise StoreError("消息附加信息损坏，无法更新请求状态。") from None
            connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                               (encoded, message_id, conversation_id))

    def update_execution(self, conversation_id: str, message_id: str, execution) -> None:
        """Persist public progress atomically, preserving a newer terminal state."""
        _identifier(conversation_id)
        _identifier(message_id)
        execution = normalize_execution(execution)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT metadata FROM messages WHERE id = ? AND conversation_id = ?",
                                     (message_id, conversation_id)).fetchone()
            if row is None:
                return  # A user may clear/delete the session while a task ends.
            metadata = json.loads(row["metadata"])
            metadata["execution"] = merge_execution(metadata.get("execution"), execution)
            connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                               (_metadata(metadata), message_id, conversation_id))

    def reserve_agent_execution(self, conversation_id: str, message_id: str) -> None:
        """Reserve side effects once, including after crashes or chat retries.

        The marker follows the original user message and is deleted with it;
        tool failures never cause an implicit replay of already issued actions.
        """
        _identifier(conversation_id)
        _identifier(message_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT metadata FROM messages WHERE id = ? AND conversation_id = ? AND role = 'user'",
                                     (message_id, conversation_id)).fetchone()
            if row is None:
                raise StoreError("任务对应的用户消息不存在，本轮未启动任务助手。")
            metadata = json.loads(row["metadata"])
            if metadata.get("agent_execution_started"):
                raise StoreError("这条消息已经启动过任务助手；为避免重复操作，请先检查结果，再发送新的任务消息。")
            metadata["agent_execution_started"] = True
            connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                               (_metadata(metadata), message_id, conversation_id))

    def recover_pending_message(self, conversation_id: str, message_id: str) -> dict:
        """Recover an orphaned pending turn without overwriting a newer state.

        A history worker may hold a pending snapshot while a request commits.
        Re-read and compare inside the write transaction; return the current
        row so the caller also discards its obsolete pending metadata.
        """
        message, _reply = self.reconcile_request(conversation_id, message_id)
        return message

    def reconcile_request(self, conversation_id: str, message_id: str) -> tuple[dict, dict | None]:
        """Reconcile an old request and its saved reply in the same transaction.

        Pre-atomic versions could commit the assistant reply before completing
        the user turn. An existing ordinary reply is authoritative; recovering
        that turn must not require another completion or replay old speech.
        """
        _identifier(conversation_id)
        _identifier(message_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            message = _decoded(connection.execute("SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
                                                  (message_id, conversation_id)).fetchone())
            if message is None:
                raise StoreError("指定消息不存在，无法恢复请求状态。")
            reply = None
            if message["role"] == "user":
                reply = _decoded(connection.execute("""SELECT * FROM messages
                    WHERE conversation_id = ? AND role = 'assistant' AND source = 'assistant'
                    AND json_extract(metadata, '$.reply_to') = ? LIMIT 1""",
                    (conversation_id, message_id)).fetchone())
            old_status = message["metadata"].get("request_status")
            status = ("complete" if reply is not None else "interrupted"
                      if message["role"] == "user" and old_status == "pending" else old_status)
            execution = message["metadata"].get("execution")
            trace_interrupted = (status == "interrupted" and isinstance(execution, dict)
                                 and execution.get("state") == "running")
            if trace_interrupted:
                message["metadata"]["execution"] = interrupted_execution(execution)
            if status != old_status or trace_interrupted:
                message["metadata"]["request_status"] = status
                connection.execute("UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
                                   (_metadata(message["metadata"]), message_id, conversation_id))
            return message, reply

    def close(self) -> None:
        """No connection is shared or kept open; in-flight workers own theirs."""
