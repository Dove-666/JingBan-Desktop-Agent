"""Metadata-only archive counts using owned real SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, StoreError


A, B, C = "a" * 64, "b" * 64, "c" * 64
PRIVATE = "OWNED_PRIVATE_BODY_TITLE_SUMMARY_CANARY"


class ImageReferenceSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-image-reference-summary-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite3"
        self.store = ConversationStore(self.path)
        self.store.initialize()
        self.cid = str(uuid4())

    def message(self, metadata=None, *, cid=None, role="user"):
        return self.store.append_message(cid or self.cid, role, PRIVATE, role, metadata=metadata)

    def event(self, metadata=None, *, cid=None):
        return self.store.append_screen_comment(cid or self.cid, PRIVATE, event_id=str(uuid4()),
            app_name=PRIVATE, context_summary=PRIVATE, event_type="owned", metadata=metadata)

    def raw_metadata(self, message_id, raw):
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("UPDATE messages SET metadata=? WHERE id=?", (raw, message_id))

    def test_empty_input_is_no_io_and_invalid_inputs_are_bounded_before_connection(self):
        with patch.object(self.store, "_connection", side_effect=AssertionError("NO_QUERY_EXPECTED")):
            self.assertEqual(self.store.image_reference_summary([]), {})
            self.assertEqual(self.store.image_reference_summary(()), {})
            for invalid in (None, A, {}, iter([A]), [A] * 51, [None], [True], ["A" * 64],
                            ["a" * 63], [A + "\0"], [A, []]):
                with self.subTest(input_type=type(invalid).__name__), self.assertRaises(StoreError):
                    self.store.image_reference_summary(invalid)

    def test_input_dedup_preserves_order_and_zero_counts(self):
        self.message({"image_id": A})
        result = self.store.image_reference_summary([B, A, B, C])
        self.assertEqual(list(result), [B, A, C])
        self.assertEqual(result, {B: {"message_count": 0, "conversation_count": 0},
                                  A: {"message_count": 1, "conversation_count": 1},
                                  C: {"message_count": 0, "conversation_count": 0}})

    def test_all_reference_fields_and_event_duplicates_count_distinct_messages_and_cids(self):
        self.message({"image_id": A, "context_image_id": A, "desktop_image_id": A,
                      "context_image_pair": [{"image_id": A}, {"image_id": B}, {"image_id": A}]})
        self.message({"image_id": A}, role="assistant")
        other = str(uuid4())
        self.message({"image_id": A}, cid=other)
        event = self.event({"image_id": A, "context_image_id": B}, cid=other)
        self.raw_metadata(event["id"], json.dumps({"image_id": A, "context_image_pair": [{"image_id": B}]}))
        self.assertEqual(self.store.image_reference_summary([A, B]), {
            A: {"message_count": 4, "conversation_count": 2},
            B: {"message_count": 2, "conversation_count": 2},
        })

    def test_all_request_states_keep_their_archived_references(self):
        for state in ("pending", "queued", "failed", "complete", "cancelled", "interrupted"):
            self.message({"image_id": A, "request_status": state})
        self.assertEqual(self.store.image_reference_summary([A])[A],
                         {"message_count": 6, "conversation_count": 1})

    def test_references_beyond_500_conversations_and_history_rows_are_counted(self):
        for _ in range(505):
            self.message({}, cid=str(uuid4()))
        late_cid = str(uuid4())
        self.message({"image_id": A}, cid=late_cid)
        self.message({"context_image_pair": [{"image_id": A}]}, cid=late_cid)
        with patch.object(self.store, "list_conversations", side_effect=AssertionError("NO_LIST_PAGINATION")), \
             patch.object(self.store, "load_messages", side_effect=AssertionError("NO_MESSAGE_BODIES")):
            self.assertEqual(self.store.image_reference_summary([A])[A],
                             {"message_count": 2, "conversation_count": 1})

    def test_optional_missing_null_and_empty_pair_are_valid(self):
        self.message({})
        self.message({"image_id": None, "context_image_id": None, "desktop_image_id": None,
                      "context_image_pair": None, "desktop_context": True})
        self.message({"context_image_pair": []})
        self.assertEqual(self.store.image_reference_summary([A])[A],
                         {"message_count": 0, "conversation_count": 0})

    def test_damaged_json_or_nonobject_anywhere_cannot_be_treated_as_no_references(self):
        message = self.message({})
        for raw in ("{", "[]", '"text"', "null", "1", '{"image_id":NaN}'):
            with self.subTest(raw_type=raw[:1]):
                self.raw_metadata(message["id"], raw)
                with self.assertRaises(StoreError) as caught:
                    self.store.image_reference_summary([A])
                self.assertNotIn(PRIVATE, str(caught.exception))
        self.raw_metadata(message["id"], "{}")
        self.assertEqual(self.store.image_reference_summary([A])[A]["message_count"], 0)

    def test_malformed_known_fields_and_pairs_fail_closed(self):
        message = self.message({})
        invalid = [
            {"image_id": 12}, {"context_image_id": []}, {"desktop_image_id": "bad"},
            {"image_id": "A" * 64}, {"image_id": A + "\0"}, {"image_id": "a" * 63 + "\0"},
            {"context_image_pair": {}}, {"context_image_pair": "bad"},
            {"context_image_pair": [None]}, {"context_image_pair": [[]]},
            {"context_image_pair": [{}]}, {"context_image_pair": [{"image_id": None}]},
            {"context_image_pair": [{"image_id": "bad"}]},
        ]
        for metadata in invalid:
            with self.subTest(keys=list(metadata)):
                self.raw_metadata(message["id"], json.dumps(metadata))
                with self.assertRaises(StoreError):
                    self.store.image_reference_summary([B])

    def test_ambiguous_duplicate_reference_keys_fail_closed(self):
        message = self.message({})
        for raw in ('{"image_id":"' + A + '","image_id":"' + B + '"}',
                    '{"context_image_pair":[],"context_image_pair":null}',
                    '{"context_image_pair":[{"image_id":"' + A + '","image_id":"' + B + '"}]}'):
            self.raw_metadata(message["id"], raw)
            with self.assertRaises(StoreError):
                self.store.image_reference_summary([A])

    def test_json_escaped_keys_and_values_are_counted_without_exposing_other_fields(self):
        message = self.message({})
        raw = (r'{"\u0069mage_id":"' + A[:-1] + r'\u0061",'
               r'"\u0063ontext_image_pair":[{"\u0069mage_id":"' + B + r'"}],'
               r'"unrelated_note":"text\u0000is allowed outside references"}')
        self.raw_metadata(message["id"], raw)
        self.assertEqual(self.store.image_reference_summary([A, B]), {
            A: {"message_count": 1, "conversation_count": 1},
            B: {"message_count": 1, "conversation_count": 1}})
        for raw in (r'{"\u0069mage_id":"' + A + r'\u0000suffix"}',
                    r'{"context_image_pair":[{"\u0069mage_id":"' + A + r'\u0000suffix"}]}'):
            self.raw_metadata(message["id"], raw)
            with self.assertRaises(StoreError):
                self.store.image_reference_summary([B])

    def test_candidate_orphan_event_fails_but_unrelated_orphan_does_not_hide_zero(self):
        event = self.event({"desktop_image_id": A})
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("DELETE FROM messages WHERE id=?", (event["id"],))
        with self.assertRaises(StoreError):
            self.store.image_reference_summary([A])
        self.assertEqual(self.store.image_reference_summary([B])[B],
                         {"message_count": 0, "conversation_count": 0})

    def test_event_mapping_requires_both_event_id_and_conversation(self):
        event = self.event({"image_id": A})
        other_cid = str(uuid4())
        self.message({}, cid=other_cid)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("DELETE FROM messages WHERE id=?", (event["id"],))
                connection.execute("""INSERT INTO messages(id,conversation_id,role,content,created_at,source,screen_event_id)
                    VALUES(?,?,'assistant',?,'2026','proactive_screen',?)""",
                    (str(uuid4()), other_cid, PRIVATE, event["screen_event_id"]))
        with self.assertRaises(StoreError):
            self.store.image_reference_summary([A])

    def test_authorizer_denies_all_body_columns_and_query_leaves_archive_unchanged(self):
        self.message({"image_id": A})
        self.event({"context_image_pair": [{"image_id": A}]})
        forbidden = {("messages", "content"), ("screen_events", "context_summary"),
                     ("screen_events", "app_name"), ("conversations", "title"), ("conversations", "summary")}
        reads, denied = set(), []
        original_connect = sqlite3.connect
        def authorizer(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ:
                reads.add((table, column))
                if (table, column) in forbidden:
                    denied.append((table, column))
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_authorizer(authorizer)
            return connection
        with closing(original_connect(self.path)) as connection:
            before = list(connection.iterdump())
        with patch("desktop_pet.conversations.store.sqlite3.connect", connect):
            result = self.store.image_reference_summary([A])
        self.assertEqual(result[A], {"message_count": 2, "conversation_count": 1})
        self.assertEqual(denied, [])
        self.assertFalse(reads & forbidden)
        self.assertIn(("messages", "metadata"), reads)
        self.assertIn(("screen_events", "metadata"), reads)
        self.assertNotIn(PRIVATE, json.dumps(result))
        with closing(original_connect(self.path)) as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_validation_and_counts_share_one_read_snapshot(self):
        self.message({"image_id": A})
        second = self.message({})
        entered, release = threading.Event(), threading.Event()
        original_connect = sqlite3.connect
        class SnapshotConnection(sqlite3.Connection):
            paused = False
            def execute(inner, sql, *args, **kwargs):
                cursor = super().execute(sql, *args, **kwargs)
                if not inner.paused and "json_valid(metadata)" in sql:
                    inner.paused = True
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("OWNED_SNAPSHOT_NOT_RELEASED")
                return cursor
        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=SnapshotConnection)
        with ThreadPoolExecutor(max_workers=1) as worker:
            try:
                with patch("desktop_pet.conversations.store.sqlite3.connect", connect):
                    future = worker.submit(self.store.image_reference_summary, [A])
                    self.assertTrue(entered.wait(2))
                    with closing(original_connect(self.path)) as connection:
                        with connection:
                            connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                               (json.dumps({"image_id": A}), second["id"]))
                    release.set()
                    self.assertEqual(future.result(timeout=4)[A]["message_count"], 1)
            finally:
                release.set()
        self.assertEqual(self.store.image_reference_summary([A])[A]["message_count"], 2)

    def test_sql_permission_and_io_failures_raise_safe_errors_instead_of_zero(self):
        self.message({"image_id": A})
        original_connect = sqlite3.connect
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_authorizer(lambda action, table, column, *_:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == "metadata" else sqlite3.SQLITE_OK)
            return connection
        with patch("desktop_pet.conversations.store.sqlite3.connect", connect):
            with self.assertRaises(StoreError):
                self.store.image_reference_summary([A])
        for error in (sqlite3.OperationalError(PRIVATE), OSError(PRIVATE)):
            with patch("desktop_pet.conversations.store.sqlite3.connect", side_effect=error):
                with self.assertRaises(StoreError) as caught:
                    self.store.image_reference_summary([A])
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_snapshot_has_stable_candidate_bound_hash_and_summary_keeps_two_fields(self):
        with patch.object(self.store, "_connection", side_effect=AssertionError("NO_EMPTY_IO")):
            self.assertEqual(self.store.image_reference_snapshot([]), {})
        first = self.store.image_reference_snapshot([B, A, B])
        self.assertEqual(list(first), [B, A])
        self.assertEqual(first, self.store.image_reference_snapshot([B, A]))
        for value in first.values():
            self.assertEqual(set(value), {"message_count", "conversation_count", "reference_revision"})
            self.assertRegex(value["reference_revision"], r"^[a-f0-9]{64}$")
        self.assertNotEqual(first[A]["reference_revision"], first[B]["reference_revision"])
        self.assertEqual(self.store.image_reference_summary([A])[A],
                         {"message_count": 0, "conversation_count": 0})

    def test_private_reference_identity_swap_changes_revision_without_sync_watermark_change(self):
        first = self.message({"context_image_id": A})
        second = self.message({})
        before = self.store.image_reference_snapshot([A])[A]
        with closing(sqlite3.connect(self.path)) as connection:
            high_before = connection.execute("SELECT high_seq FROM remote_sync_state WHERE id=1").fetchone()[0]
            with connection:
                connection.execute("UPDATE messages SET metadata='{}' WHERE id=?", (first["id"],))
                connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                   (json.dumps({"context_image_id": A}), second["id"]))
            high_after = connection.execute("SELECT high_seq FROM remote_sync_state WHERE id=1").fetchone()[0]
        after = self.store.image_reference_snapshot([A])[A]
        self.assertEqual(high_before, high_after)
        self.assertEqual((before["message_count"], before["conversation_count"]),
                         (after["message_count"], after["conversation_count"]))
        self.assertNotEqual(before["reference_revision"], after["reference_revision"])

    def test_duplicate_paths_and_unrelated_messages_do_not_change_reference_set_revision(self):
        message = self.message({"image_id": A})
        before = self.store.image_reference_snapshot([A])[A]
        self.raw_metadata(message["id"], json.dumps({"image_id": A, "context_image_id": A,
            "context_image_pair": [{"image_id": A}, {"image_id": A}]}))
        self.message({"image_id": B}, cid=str(uuid4()))
        self.assertEqual(self.store.image_reference_snapshot([A])[A], before)

    def test_length_prefixes_distinguish_legacy_identity_concatenations(self):
        first = self.store.append_message("c", "user", PRIVATE, "user", message_id="ab", metadata={"image_id": A})
        second = self.store.append_message("bc", "user", PRIVATE, "user", message_id="a")
        before = self.store.image_reference_snapshot([A])[A]
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("UPDATE messages SET metadata='{}' WHERE id=?", (first["id"],))
                connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                   (json.dumps({"image_id": A}), second["id"]))
        after = self.store.image_reference_snapshot([A])[A]
        self.assertEqual(before["message_count"], after["message_count"])
        self.assertEqual(before["conversation_count"], after["conversation_count"])
        self.assertNotEqual(before["reference_revision"], after["reference_revision"])

    def test_snapshot_revision_streams_identities_under_body_column_denial(self):
        message = self.message({"image_id": A})
        self.event({"context_image_pair": [{"image_id": A}]})
        original_connect = sqlite3.connect
        denied, streamed = [], []
        class StreamingCursor(sqlite3.Cursor):
            def fetchall(inner):
                names = tuple(column[0] for column in inner.description or ())
                if names == ("image_id", "message_id", "conversation_id"):
                    raise AssertionError("NO_MATERIALIZED_IDENTITY_LIST")
                return super().fetchall()
            def __iter__(inner):
                if tuple(column[0] for column in inner.description or ()) == ("image_id", "message_id", "conversation_id"):
                    streamed.append(True)
                return super().__iter__()
        class StreamingConnection(sqlite3.Connection):
            def execute(inner, sql, *args, **kwargs):
                return inner.cursor(factory=StreamingCursor).execute(sql, *args, **kwargs)
        def authorizer(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and column in {"content", "context_summary", "app_name", "title", "summary"}:
                denied.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=StreamingConnection)
            connection.set_authorizer(authorizer)
            return connection
        with patch("desktop_pet.conversations.store.sqlite3.connect", connect):
            result = self.store.image_reference_snapshot([A])
        self.assertEqual(streamed, [True])
        self.assertEqual(denied, [])
        self.assertEqual(result[A]["message_count"], 2)
        serialized = json.dumps(result)
        self.assertNotIn(PRIVATE, serialized)
        self.assertNotIn(self.cid, serialized)
        self.assertNotIn(message["id"], serialized)

    def test_counts_and_revision_share_snapshot_across_concurrent_identity_swap(self):
        first = self.message({"context_image_id": A})
        second = self.message({})
        before = self.store.image_reference_snapshot([A])
        original_connect = sqlite3.connect
        entered, release = threading.Event(), threading.Event()
        class SnapshotConnection(sqlite3.Connection):
            paused = False
            def execute(inner, sql, *args, **kwargs):
                cursor = super().execute(sql, *args, **kwargs)
                if not inner.paused and "count(DISTINCT r.message_id)" in sql:
                    inner.paused = True
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("OWNED_REVISION_BOUNDARY_NOT_RELEASED")
                return cursor
        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=SnapshotConnection)
        with ThreadPoolExecutor(max_workers=1) as worker:
            try:
                with patch("desktop_pet.conversations.store.sqlite3.connect", connect):
                    future = worker.submit(self.store.image_reference_snapshot, [A])
                    self.assertTrue(entered.wait(2))
                    with closing(original_connect(self.path)) as connection:
                        with connection:
                            connection.execute("UPDATE messages SET metadata='{}' WHERE id=?", (first["id"],))
                            connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                               (json.dumps({"context_image_id": A}), second["id"]))
                    release.set()
                    self.assertEqual(future.result(timeout=4), before)
            finally:
                release.set()
        self.assertNotEqual(self.store.image_reference_snapshot([A]), before)

    def test_snapshot_damaged_reference_or_driver_failure_never_returns_partial_revision(self):
        message = self.message({"image_id": A})
        self.raw_metadata(message["id"], '{"image_id":[]}')
        with self.assertRaises(StoreError):
            self.store.image_reference_snapshot([A])
        self.raw_metadata(message["id"], "{}")
        with patch("desktop_pet.conversations.store.sqlite3.connect", side_effect=sqlite3.OperationalError(PRIVATE)):
            with self.assertRaises(StoreError) as caught:
                self.store.image_reference_snapshot([A])
        self.assertNotIn(PRIVATE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
