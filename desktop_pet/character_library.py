"""A local, transactional library of data-only character card documents.

Entries have independent shelf labels; saving or deleting a library entry does
not apply a character or modify active preferences. Read operations never create
a missing database. Only the selected entry's original document is loaded.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from .character_cards import CharacterCard, CharacterCardError, MAX_CARD_JSON_BYTES


MAX_LIBRARY_ENTRIES = 10_000
MAX_LABEL_CHARACTERS = 128
_APPLICATION_ID = 0x41555243
_VERSION = 1
_SUMMARY_COLUMNS = "id,label,name,digest,created_at,updated_at"
_COLUMNS = frozenset((*_SUMMARY_COLUMNS.split(","), "document"))
_UNAVAILABLE = "角色库暂时无法读取，请检查本地配置目录后重试。"
_WRITE_FAILED = "无法确认角色库操作已保存，请重新打开角色库核对，并检查配置目录权限。"
_DAMAGED = "角色库内容已损坏或版本暂不支持，请保留原文件并检查本地配置。"
_MISSING = "这张角色卡已不在角色库中，请重新选择。"


class CharacterLibraryError(ValueError):
    """Static user-facing errors with no native paths or SQLite contents."""


@dataclass(frozen=True)
class CharacterLibraryEntry:
    id: str
    label: str
    name: str
    digest: str
    created_at: str
    updated_at: str


def _label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_LABEL_CHARACTERS
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CharacterLibraryError("角色库名称不能为空或超过 128 字符，也不能包含换行。")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CharacterLibraryError("角色库名称包含无效字符，请重新填写。") from None
    return value.strip()


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CharacterLibraryError("角色卡标识无效，请重新选择。")
    return value


def _name(value):
    # The source card's name may contain whitespace that a shelf label cannot.
    # Keep the imported identity unchanged; only normalize its default label.
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_LABEL_CHARACTERS or "\0" in value):
        raise CharacterLibraryError(_DAMAGED)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CharacterLibraryError(_DAMAGED) from None
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise CharacterLibraryError(_DAMAGED)
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
    except (ValueError, OverflowError):
        raise CharacterLibraryError(_DAMAGED) from None
    return value


def _entry(row):
    try:
        identity = _identity(row["id"])
        if UUID(identity).version != 4:
            raise CharacterLibraryError(_DAMAGED)
        label, name = _label(row["label"]), _name(row["name"])
        digest = row["digest"]
        if (label != row["label"] or name != row["name"]
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise CharacterLibraryError(_DAMAGED)
        return CharacterLibraryEntry(identity, label, name, digest,
                                     _timestamp(row["created_at"]), _timestamp(row["updated_at"]))
    except (KeyError, IndexError, TypeError, ValueError):
        raise CharacterLibraryError(_DAMAGED) from None


def _document(card):
    if not isinstance(card, CharacterCard):
        raise CharacterLibraryError("角色卡内容无效，请重新导入。")
    try:
        native = card.to_dict()
        checked = CharacterCard.from_dict(native)
        # Preserve original key order for equality with the imported snapshot.
        document = json.dumps(native, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        canonical = json.dumps(native, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(document.encode("utf-8")) > MAX_CARD_JSON_BYTES:
            raise ValueError()
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return checked, document, digest
    except (CharacterCardError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise CharacterLibraryError("角色卡内容无效或过大，请重新导入。") from None


def character_card_digest(card: CharacterCard) -> str:
    """Return the same canonical content identity used by saved library rows."""
    return _document(card)[2]


class CharacterLibrary:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    @staticmethod
    def _check_schema(db):
        if (db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != _VERSION
                or {row[1] for row in db.execute("PRAGMA table_info(character_cards)")} != _COLUMNS):
            raise CharacterLibraryError(_DAMAGED)

    @classmethod
    def _prepare(cls, db):
        application = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        objects = db.execute("SELECT name FROM sqlite_master").fetchall()
        if application == 0 and version == 0 and not objects:
            db.execute("BEGIN IMMEDIATE")
            # Recheck under the write lock if another first saver raced us.
            if not db.execute("SELECT name FROM sqlite_master").fetchall():
                db.execute("""CREATE TABLE character_cards (
                    id TEXT PRIMARY KEY NOT NULL,
                    label TEXT NOT NULL,
                    name TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE,
                    document TEXT NOT NULL CHECK(length(CAST(document AS BLOB)) <= 1048576),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={_VERSION}")
            db.commit()
        cls._check_schema(db)

    @contextmanager
    def _database(self, *, write=False, create=False):
        db = None
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            mode = "rwc" if create else "rw" if write else "ro"
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=1.0)
            db.row_factory = sqlite3.Row
            if create:
                self._prepare(db)
            else:
                self._check_schema(db)
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except CharacterLibraryError:
            if db is not None:
                with suppress(sqlite3.Error): db.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
            if db is not None:
                with suppress(sqlite3.Error): db.rollback()
            raise CharacterLibraryError(_WRITE_FAILED if write else _UNAVAILABLE) from None
        finally:
            if db is not None:
                with suppress(sqlite3.Error): db.close()

    def list(self) -> tuple[CharacterLibraryEntry, ...]:
        if not self.path.exists():
            return ()
        with self._database() as db:
            rows = db.execute("SELECT " + _SUMMARY_COLUMNS + " FROM character_cards "
                              "ORDER BY created_at,id LIMIT ?", (MAX_LIBRARY_ENTRIES + 1,)).fetchall()
            if len(rows) > MAX_LIBRARY_ENTRIES:
                raise CharacterLibraryError("角色库条目过多，请检查本地角色库后重试。")
            return tuple(_entry(row) for row in rows)

    def save(self, card, label=None) -> CharacterLibraryEntry:
        selected, document, digest = _document(card)
        supplied_label = _label(label) if label is not None else None
        with self._database(write=True, create=True) as db:
            row = db.execute("SELECT " + _SUMMARY_COLUMNS + ",document FROM character_cards WHERE digest=?", (digest,)).fetchone()
            if row is not None:
                entry = _entry(row)
                self._load_row(row, entry)
                return entry
            if db.execute("SELECT count(*) FROM character_cards").fetchone()[0] >= MAX_LIBRARY_ENTRIES:
                raise CharacterLibraryError("角色库已达到保存上限，请先删除不需要的角色。")
            labels = {row[0] for row in db.execute("SELECT label FROM character_cards")}
            default_label = " ".join(selected.name.split())
            default_label = "".join(char for char in default_label if ord(char) >= 32 and ord(char) != 127) or "未命名角色"
            shelf = supplied_label or default_label
            if supplied_label is None:
                number = 2
                while shelf in labels:
                    suffix = f"（{number}）"
                    shelf = default_label[:MAX_LABEL_CHARACTERS - len(suffix)] + suffix
                    number += 1
            shelf = _label(shelf)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            identity = uuid4().hex
            db.execute("INSERT INTO character_cards (id,label,name,digest,document,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (identity, shelf, selected.name, digest, document, now, now))
            return CharacterLibraryEntry(identity, shelf, selected.name, digest, now, now)

    def load(self, identity) -> CharacterCard:
        identity = _identity(identity)
        if not self.path.exists():
            raise CharacterLibraryError(_MISSING)
        with self._database() as db:
            row = db.execute("SELECT " + _SUMMARY_COLUMNS + ",document FROM character_cards WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise CharacterLibraryError(_MISSING)
            return self._load_row(row, _entry(row))

    @staticmethod
    def _load_row(row, entry):
        raw = row["document"]
        try:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CARD_JSON_BYTES:
                raise ValueError()
            selected = CharacterCard.from_dict(json.loads(raw))
            _, _, digest = _document(selected)
            if selected.name != entry.name or digest != entry.digest:
                raise ValueError()
            return selected
        except (CharacterCardError, ValueError, TypeError, UnicodeError, RecursionError):
            raise CharacterLibraryError("这张角色卡的数据已损坏，请保留原角色库，删除该库项后重新导入。") from None

    def rename(self, identity, label) -> CharacterLibraryEntry:
        identity, label = _identity(identity), _label(label)
        if not self.path.exists():
            raise CharacterLibraryError(_MISSING)
        with self._database(write=True) as db:
            row = db.execute("SELECT " + _SUMMARY_COLUMNS + " FROM character_cards WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise CharacterLibraryError(_MISSING)
            entry = _entry(row)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            db.execute("UPDATE character_cards SET label=?,updated_at=? WHERE id=?", (label, now, identity))
            return CharacterLibraryEntry(identity, label, entry.name, entry.digest, entry.created_at, now)

    def delete(self, identity) -> bool:
        identity = _identity(identity)
        if not self.path.exists():
            return False
        with self._database(write=True) as db:
            result = db.execute("DELETE FROM character_cards WHERE id=?", (identity,))
            return result.rowcount == 1
