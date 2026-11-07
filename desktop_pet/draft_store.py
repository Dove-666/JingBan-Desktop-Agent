"""Local text drafts, separate from conversation messages and remote sync.

All methods are synchronous and must be called by the draft owner's I/O worker,
never by a keypress handler. Import and construction perform no filesystem I/O.
Epoch/revision CAS prevents an old save/accepted-send from replacing newer text.
Empty records are durable epoch fences, not archived conversations or messages.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

MAX_TEXT_BYTES = 256 * 1024
MAX_TOTAL_TEXT_BYTES = 4 * 1024 * 1024
MAX_LIVE_DRAFTS = 256
MAX_RECORDS = 16384
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_REVISION = 2**63 - 1
_EPOCH = re.compile(r"[a-f0-9]{32}\Z")


class DraftStorageError(ValueError):
    def __init__(self, code="DRAFT_STORAGE_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


def _identifier(value, *, empty=False):
    if empty and value == "": return value
    # Drafts follow the existing archive's exact-string identity rules. They
    # are not queued-agent authority, which separately requires canonical UUIDs.
    if (type(value) is not str or not value.strip() or len(value) > 200
            or any(ord(char) < 32 for char in value)):
        raise DraftStorageError("DRAFT_INVALID_IDENTITY")
    return value


@dataclass(frozen=True)
class DraftSnapshot:
    conversation_id: str
    epoch: str
    revision: int
    generation: str
    text: str
    updated_at: float


def new_draft(conversation_id, text="", *, generation="", epoch=None, revision=1, clock=time.time):
    value = DraftSnapshot(conversation_id, secrets.token_hex(16) if epoch is None else epoch, revision,
                          generation, text, clock())
    _validate(value)
    return value


def _validate(value):
    if type(value) is not DraftSnapshot: raise DraftStorageError("DRAFT_INVALID_IDENTITY")
    _identifier(value.conversation_id); _identifier(value.generation, empty=True)
    if (type(value.epoch) is not str or not _EPOCH.fullmatch(value.epoch)
            or type(value.revision) is not int or not 1 <= value.revision <= MAX_REVISION
            or type(value.text) is not str or type(value.updated_at) not in {int, float}
            or not math.isfinite(value.updated_at) or value.updated_at < 0):
        raise DraftStorageError("DRAFT_INVALID_IDENTITY")
    try: count = len(value.text.encode("utf-8"))
    except UnicodeError: raise DraftStorageError("DRAFT_INPUT_LIMIT") from None
    if count > MAX_TEXT_BYTES: raise DraftStorageError("DRAFT_INPUT_LIMIT")
    return value


def _decoded(row):
    if row is None: return None
    try: return _validate(DraftSnapshot(*row))
    except (ValueError, TypeError, OverflowError): raise DraftStorageError() from None


class LocalDraftStore:
    """One private side database; no prompts, images, settings or remote API.

    A successful CAS means the exact text snapshot was committed, not that a
    message was sent. Callers must recheck main-archive CID/generation/tombstones
    before hydrating drafts; this database never invents archive authority.
    """
    def __init__(self, path):
        self.path = Path(path)

    def _checked_target(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError: return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_DATABASE_BYTES
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise DraftStorageError()

    def _connection(self):
        connection = None
        try:
            self._checked_target()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=.25, isolation_level=None)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {"draft_store_meta", "drafts"}: raise DraftStorageError()
            if not tables:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE draft_store_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
                connection.execute("INSERT INTO draft_store_meta VALUES (1,1)")
                connection.execute("""CREATE TABLE drafts (
                    conversation_id TEXT PRIMARY KEY, epoch TEXT NOT NULL, revision INTEGER NOT NULL,
                    generation TEXT NOT NULL, text TEXT NOT NULL, updated_at REAL NOT NULL)""")
                connection.execute("COMMIT")
            if connection.execute("SELECT version FROM draft_store_meta WHERE singleton=1").fetchone() != (1,):
                raise DraftStorageError()
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete": raise DraftStorageError()
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("PRAGMA cache_size=-512")
            connection.execute("PRAGMA secure_delete=ON")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute("PRAGMA max_page_count="+str(MAX_DATABASE_BYTES//page_size))
            self._checked_target()
            return connection
        except Exception:
            if connection is not None: connection.close()
            raise DraftStorageError() from None

    @staticmethod
    def _find(connection, conversation_id):
        return _decoded(connection.execute("""SELECT conversation_id,epoch,revision,generation,text,updated_at
            FROM drafts WHERE conversation_id=?""", (conversation_id,)).fetchone())

    @staticmethod
    def _budget(connection, value, existing):
        records, live, size = connection.execute("""SELECT count(*),
            COALESCE(sum(CASE WHEN text!='' THEN 1 ELSE 0 END),0),
            COALESCE(sum(length(CAST(text AS BLOB))),0) FROM drafts""").fetchone()
        if (records + (existing is None) > MAX_RECORDS
                or live + bool(value.text) - bool(existing and existing.text) > MAX_LIVE_DRAFTS
                or size + len(value.text.encode("utf-8")) - len(existing.text.encode("utf-8") if existing else b"") > MAX_TOTAL_TEXT_BYTES):
            raise DraftStorageError("DRAFT_STORAGE_LIMIT")

    @staticmethod
    def _put(connection, value):
        connection.execute("""INSERT INTO drafts VALUES (?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET epoch=excluded.epoch,revision=excluded.revision,
            generation=excluded.generation,text=excluded.text,updated_at=excluded.updated_at""",
            (value.conversation_id, value.epoch, value.revision, value.generation, value.text, value.updated_at))

    def lookup(self, conversation_id):
        _identifier(conversation_id)
        if not self.path.exists(): return None
        connection = self._connection()
        try: return self._find(connection, conversation_id)
        except Exception: raise DraftStorageError() from None
        finally: connection.close()

    def load(self):
        if not self.path.exists(): return ()
        connection = self._connection()
        try:
            rows = connection.execute("""SELECT conversation_id,epoch,revision,generation,text,updated_at
                FROM drafts WHERE text!='' ORDER BY updated_at DESC,conversation_id LIMIT ?""", (MAX_LIVE_DRAFTS+1,)).fetchall()
            values = tuple(_decoded(row) for row in rows)
            if len(values) > MAX_LIVE_DRAFTS or sum(len(row.text.encode("utf-8")) for row in values) > MAX_TOTAL_TEXT_BYTES:
                raise DraftStorageError()
            return values
        except Exception: raise DraftStorageError() from None
        finally: connection.close()

    def save(self, value, *, expected_empty_generation=False):
        """Save newer text; an explicit host bind may advance an empty anchor.

        The owner permits this flag only after the first accepted message has
        acquired its archive identity. It never changes an established UUID
        anchor, rotates no editor epoch, and does not authorize sending text.
        """
        _validate(value)
        if type(expected_empty_generation) is not bool:
            raise DraftStorageError("DRAFT_INVALID_IDENTITY")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._find(connection, value.conversation_id)
            if existing is not None:
                if (existing.epoch != value.epoch or existing.generation != value.generation
                        and not (expected_empty_generation and existing.generation == "" and value.generation)):
                    return False
                if value.revision <= existing.revision:
                    return (value.revision == existing.revision and value.text == existing.text
                            and value.generation == existing.generation)
            self._budget(connection, value, existing)
            self._put(connection, value)
            connection.execute("COMMIT")
            return True
        except DraftStorageError: raise
        except Exception: raise DraftStorageError() from None
        finally: connection.close()  # An uncommitted conflict/failure rolls back.

    def rotate(self, value, *, expected_epoch, expected_revision=None):
        """Atomically establish a new epoch, optionally matching exact old text.

        User clear/delete uses an epoch-only match. Accepted-send cleanup uses
        an exact revision match so concurrent newer edits cannot be discarded.
        A generation transition also requires an explicit host epoch rotation.
        """
        _validate(value)
        if (expected_epoch is not None and (type(expected_epoch) is not str or not _EPOCH.fullmatch(expected_epoch))
                or expected_revision is not None and (type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION)
                or expected_epoch == value.epoch):
            raise DraftStorageError("DRAFT_INVALID_IDENTITY")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._find(connection, value.conversation_id)
            if existing is None:
                if expected_epoch is not None or expected_revision is not None: return False
            elif (existing.epoch != expected_epoch or expected_revision is not None and existing.revision != expected_revision):
                return False
            self._budget(connection, value, existing)
            self._put(connection, value)
            connection.execute("COMMIT")
            return True
        except DraftStorageError: raise
        except Exception: raise DraftStorageError() from None
        finally: connection.close()
