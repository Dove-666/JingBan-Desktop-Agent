"""Host-authoritative record synchronization; never copy a live SQLite file.

Snapshots replace a phone cache and then catch up using ``pull(watermark)``.
Finish every snapshot page before applying subsequent changes. Both snapshot
pages and journal pages may be retried; apply them by entity ID and sequence.
This module deliberately offers no remote row-import or automatic AI replay.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from uuid import UUID, uuid4

from ..conversations.store import (
    ConversationStore, StoreError, _identifier, maintain_remote_sync, remote_sync_projection,
)


class SyncError(StoreError):
    """Static synchronization errors, suitable for authenticated clients."""

    code = "sync_error"


class SyncResetRequired(SyncError):
    """The client must discard its cache/partial snapshot and start again."""

    code = "sync_reset_required"

    def __init__(self, message="同步游标已过期，请重新获取完整快照。"):
        super().__init__(message)


STATUSES = frozenset(("pending", "failed", "complete", "cancelled", "interrupted"))


def sanitize_message(message: dict) -> dict:
    """Return only message text and protocol fields, never arbitrary metadata.

    Known request metadata is flattened into public ``request_status`` and
    ``reply_to`` fields. Journal SQL additionally verifies reply_to ownership;
    this standalone helper accepts a UUID only, as used by real chat turns.
    """
    fields = ("id", "conversation_id", "role", "content", "created_at", "source")
    result = {key: message[key] for key in fields if isinstance(message.get(key), str)}
    metadata = message.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    status = message.get("request_status", metadata.get("request_status"))
    result["request_status"] = status if isinstance(status, str) and status in STATUSES else None
    reply = message.get("reply_to", metadata.get("reply_to"))
    try:
        valid_reply = str(UUID(reply)) if isinstance(reply, str) else None
    except (ValueError, TypeError, AttributeError):
        valid_reply = None
    result["reply_to"] = valid_reply
    return result


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise SyncError("同步分页数量必须为 1～100。")
    return value


def _json(value: dict) -> str:
    if not isinstance(value, dict):
        raise SyncError("远程请求数据必须为 JSON 对象。")
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65536:
            raise SyncError("远程请求数据过大。")
        return encoded
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise SyncError("远程请求数据格式无效。") from None


class RemoteSyncStore:
    """A persistent change journal, bounded initial snapshots, and request IDs.

    Operations provide durable *at most once admission*, not a distributed
    transaction around GUI/AI side effects. A pending operation after a crash
    is never admitted for execution again. The caller must reconcile its stable
    message ID against the chat store, then complete the operation. A lost
    completion acknowledgement is safe to retry with the same result.
    """

    SNAPSHOT_TTL_SECONDS = 900
    MAX_SNAPSHOTS = 2
    MAX_SNAPSHOT_ITEMS = 100000
    MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

    def __init__(self, chat_store: ConversationStore):
        self.chat_store = chat_store

    @staticmethod
    def _item(row: sqlite3.Row, *, change: bool = False) -> dict:
        result = {"entity": row["entity"], "id": row["entity_id"],
                  "conversation_id": row["conversation_id"],
                  "deleted": bool(row["deleted"]) if change else False,
                  "data": json.loads(row["payload"])}
        if change:
            result["seq"] = row["seq"]
        return result

    def _new_snapshot(self, connection: sqlite3.Connection, now: float) -> str:
        connection.execute("DELETE FROM remote_sync_snapshots WHERE created_at <= ?",
                           (now - self.SNAPSHOT_TTL_SECONDS,))
        snapshots = connection.execute(
            "SELECT id FROM remote_sync_snapshots ORDER BY created_at DESC, id DESC").fetchall()
        for old in snapshots[max(0, self.MAX_SNAPSHOTS - 1):]:
            connection.execute("DELETE FROM remote_sync_snapshots WHERE id = ?", (old["id"],))
        # The write transaction is already open. This watermark and every
        # materialized row are one atomic database view; later writes become
        # journal entries strictly after it, even between pagination requests.
        watermark = connection.execute("SELECT high_seq FROM remote_sync_state WHERE id = 1").fetchone()[0]
        snapshot_id = str(uuid4())
        connection.execute("INSERT INTO remote_sync_snapshots(id, created_at, watermark) VALUES (?, ?, ?)",
                           (snapshot_id, now, watermark))
        ordinal, size = 0, 0
        for entity, table in (("conversation", "conversations"), ("message", "messages"),
                              ("screen_event", "screen_events")):
            cid = "record.id" if entity == "conversation" else "record.conversation_id"
            projection = remote_sync_projection(entity, "record.")
            rows = connection.execute(f"SELECT record.id, {cid} AS conversation_id, "
                                      f"{projection} AS payload FROM {table} AS record ORDER BY record.id")
            for row in rows:
                size += len(row["payload"].encode("utf-8"))
                ordinal += 1
                if ordinal > self.MAX_SNAPSHOT_ITEMS or size > self.MAX_SNAPSHOT_BYTES:
                    raise SyncError("聊天历史超过同步快照容量（100000 条或 16 MiB）；本机历史未改动。")
                connection.execute("""INSERT INTO remote_sync_snapshot_items
                    (snapshot_id, ordinal, entity, entity_id, conversation_id, payload)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, ordinal, entity, row["id"], row["conversation_id"], row["payload"]))
        return snapshot_id

    def snapshot(self, limit: int = 100, cursor: str = "") -> dict:
        """Read a stable initial snapshot in pages of at most 100 entities.

        Start with an empty cursor. Retain watermark, use next_cursor until it
        is empty, then call pull(since=watermark). An expired cursor requires a
        fresh snapshot and replacement of the partial phone cache. The bounded
        cache holds at most two snapshots, 100000 rows/16 MiB each, for 15 min.
        """
        limit = _limit(limit)
        if not isinstance(cursor, str) or len(cursor) > 100:
            raise SyncError("同步快照游标无效。")
        offset, snapshot_id = 0, ""
        if cursor:
            try:
                snapshot_id, raw_offset = cursor.split(":")
                if str(UUID(snapshot_id)) != snapshot_id or not raw_offset.isascii() or not raw_offset.isdecimal():
                    raise ValueError()
                offset = int(raw_offset)
                if offset < 0 or offset > self.MAX_SNAPSHOT_ITEMS:
                    raise ValueError()
            except (ValueError, TypeError):
                raise SyncError("同步快照游标无效。") from None
        now = time.time()
        with self.chat_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            maintain_remote_sync(connection)
            if not cursor:
                snapshot_id = self._new_snapshot(connection, now)
            snapshot = connection.execute(
                "SELECT * FROM remote_sync_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
            if snapshot is None or snapshot["created_at"] <= now - self.SNAPSHOT_TTL_SECONDS:
                raise SyncResetRequired("同步快照已过期，请重新获取完整快照。")
            floor = connection.execute("SELECT floor_seq FROM remote_sync_state WHERE id = 1").fetchone()[0]
            if snapshot["watermark"] < floor:
                raise SyncResetRequired("同步快照之后的变更已过期，请重新获取完整快照。")
            rows = connection.execute("""SELECT * FROM remote_sync_snapshot_items
                WHERE snapshot_id = ? AND ordinal > ? ORDER BY ordinal LIMIT ?""",
                (snapshot_id, offset, limit + 1)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            last = rows[-1]["ordinal"] if rows else offset
            return {"items": [self._item(row) for row in rows], "watermark": snapshot["watermark"],
                    "next_cursor": f"{snapshot_id}:{last}" if has_more else "", "has_more": has_more}

    def pull(self, since: int = 0, limit: int = 100) -> dict:
        """Read immutable changes/tombstones; advance only to next_since.

        A page need not end at a transaction boundary. Apply strictly in seq
        order and use has_more/next_since rather than a wall-clock timestamp.
        The derived journal retains at most 10000 changes/32 MiB. An older
        cursor raises SyncResetRequired, never silently dropping unseen data.
        """
        limit = _limit(limit)
        if isinstance(since, bool) or not isinstance(since, int) or since < 0 or since > 2**63 - 1:
            raise SyncError("同步增量游标无效。")
        with self.chat_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            maintain_remote_sync(connection)
            connection.execute("DELETE FROM remote_sync_snapshots WHERE created_at <= ?",
                               (time.time() - self.SNAPSHOT_TTL_SECONDS,))
            state = connection.execute("SELECT * FROM remote_sync_state WHERE id = 1").fetchone()
            watermark = state["high_seq"]
            if since < state["floor_seq"]:
                raise SyncResetRequired()
            if since > watermark:
                raise SyncResetRequired("同步增量游标超出当前历史，请重新获取完整快照。")
            rows = connection.execute("SELECT * FROM remote_sync_changes WHERE seq > ? ORDER BY seq LIMIT ?",
                                      (since, limit + 1)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {"changes": [self._item(row, change=True) for row in rows],
                    "next_since": rows[-1]["seq"] if rows else since, "has_more": has_more}

    @staticmethod
    def _operation(row: sqlite3.Row, *, accepted: bool = False) -> dict:
        return {"accepted": accepted, "device_id": row["device_id"], "request_id": row["request_id"],
                "state": row["state"], "result": json.loads(row["result"]) if row["result"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "expires_at": row["expires_at"], "ephemeral": bool(row["ephemeral"])}

    @staticmethod
    def _operation_ids(device_id: str, request_id: str) -> None:
        _identifier(device_id)
        _identifier(request_id)

    def reserve_operation(self, device_id: str, request_id: str, payload: dict, *,
                          expires_at: float | None = None, ephemeral: bool = False) -> dict:
        """Durably admit a new request once; duplicates return accepted=False.

        Request IDs bind to canonical payload, expiry and ephemeral flag. A
        differing reuse is rejected. Expired requests are recorded but never
        admitted. Transient commands should always set ephemeral=True and an
        expiry; no operation in this table is automatically executed/replayed.
        """
        self._operation_ids(device_id, request_id)
        if not isinstance(ephemeral, bool):
            raise SyncError("远程请求过期设置无效。")
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                                       or not math.isfinite(expires_at) or expires_at < 0):
            raise SyncError("远程请求过期设置无效。")
        if ephemeral and expires_at is None:
            raise SyncError("瞬时命令必须指定过期时间。")
        encoded = _json(payload)
        identity = _json({"payload": json.loads(encoded), "expires_at": expires_at, "ephemeral": ephemeral})
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        now = time.time()
        with self.chat_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                     (device_id, request_id)).fetchone()
            if row is not None:
                if row["payload_hash"] != digest:
                    raise SyncError("相同请求编号不能用于不同操作。")
                if row["state"] == "pending" and row["expires_at"] is not None and row["expires_at"] <= now:
                    connection.execute("""UPDATE remote_sync_operations SET state = 'expired', updated_at = ?
                        WHERE device_id = ? AND request_id = ?""", (now, device_id, request_id))
                    row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                             (device_id, request_id)).fetchone()
                return self._operation(row)
            state = "expired" if expires_at is not None and expires_at <= now else "pending"
            connection.execute("""INSERT INTO remote_sync_operations
                (device_id, request_id, payload_hash, state, result, created_at, updated_at, expires_at, ephemeral)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (device_id, request_id, digest, state, now, now, expires_at, int(ephemeral)))
            row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                     (device_id, request_id)).fetchone()
            return self._operation(row, accepted=state == "pending")

    def complete_operation(self, device_id: str, request_id: str, result: dict) -> dict:
        """Complete a reserved request; identical completion retries are safe."""
        self._operation_ids(device_id, request_id)
        encoded = _json(result)
        with self.chat_store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                     (device_id, request_id)).fetchone()
            if row is None:
                raise SyncError("远程请求尚未登记。")
            if row["state"] == "expired":
                raise SyncError("远程请求已过期。")
            if row["state"] == "complete":
                if row["result"] != encoded:
                    raise SyncError("远程请求已经完成，不能覆盖执行结果。")
                return self._operation(row)
            connection.execute("""UPDATE remote_sync_operations SET state = 'complete', result = ?, updated_at = ?
                WHERE device_id = ? AND request_id = ?""", (encoded, time.time(), device_id, request_id))
            row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                     (device_id, request_id)).fetchone()
            return self._operation(row)

    def get_operation(self, device_id: str, request_id: str) -> dict | None:
        """Look up durable state; pending means reconcile, never rerun blindly."""
        self._operation_ids(device_id, request_id)
        with self.chat_store._connection() as connection:
            row = connection.execute("SELECT * FROM remote_sync_operations WHERE device_id = ? AND request_id = ?",
                                     (device_id, request_id)).fetchone()
            return self._operation(row) if row is not None else None
