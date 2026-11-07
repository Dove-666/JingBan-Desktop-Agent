"""Owned actual Store/desktop export read-count contract; no GUI or model."""
import hashlib
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents.artifacts import ArtifactStore, MAX_ARTIFACT_BYTES
from desktop_pet.chat_controller import ChatController
from desktop_pet.agents.service import AgentService


class ArtifactSnapshotReadTests(unittest.TestCase):
    observations = []

    def test_desktop_verified_export_reads_delivery_once(self):
        parent = (Path(__file__).resolve().parents[1] / "agent_workspace").resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="artifact-snapshot-review-", dir=parent) as owned:
            root = Path(owned).resolve(strict=True)
            self.assertEqual(root.parent, parent)
            workspace = root / "workspace"; workspace.mkdir()
            size = MAX_ARTIFACT_BYTES - 1
            raw = (bytes(range(256)) * (size // 256 + 1))[:size]
            source = workspace / "owned.bin"; source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            store = ArtifactStore(root / "delivery"); cid = str(uuid4())
            metadata = store.register(cid, {"path": "owned.bin", "passed": True, "sha256": digest,
                                           "scope": "synthetic_fixture_receipt"}, WorkspaceFiles(workspace))
            copied = store._target(cid, metadata["reference"])
            class Service:
                snapshot_calls = 0
                def artifact_page(self, conversation_id, reference, **kwargs):
                    return store.get(conversation_id, reference, **kwargs)
                def artifact_snapshot(self, conversation_id, reference):
                    self.snapshot_calls += 1
                    return store.verified_snapshot(conversation_id, reference)
            class DesktopHost:
                _current_id = cid
                _closed = False
                _agent_service = Service()
                agent_artifact_page = ChatController.agent_artifact_page
                export_agent_artifact = ChatController.export_agent_artifact
            host = DesktopHost(); target = root / "selected.bin"
            counters = {"read_calls": 0, "source_bytes_read": 0}
            original_open = Path.open
            class Reader:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): self.stream.__enter__(); return self
                def __exit__(self, *args): return self.stream.__exit__(*args)
                def __getattr__(self, name): return getattr(self.stream, name)
                def read(self, *args, **kwargs):
                    data = self.stream.read(*args, **kwargs)
                    counters["read_calls"] += 1; counters["source_bytes_read"] += len(data)
                    return data
            def opened(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                return Reader(stream) if path == copied else stream
            started = time.perf_counter()
            with patch.object(Path, "open", opened):
                saved = host.export_agent_artifact(metadata["reference"], target, conversation_id=cid)
            elapsed = (time.perf_counter() - started) * 1000
            correct = saved and target.read_bytes() == raw
            self.observations.append({"file_bytes": size, **counters, "snapshot_calls": host._agent_service.snapshot_calls,
                "elapsed_ms": elapsed, "saved": saved, "exact_output_bytes": correct,
                "no_export_temporary_left": not list(root.glob(".aura-export-*"))})
            self.assertTrue(correct)
            self.assertEqual(counters["read_calls"], 1, "Desktop export must verify one bounded delivery snapshot, not reread per page")
            self.assertEqual(counters["source_bytes_read"], size)
            self.assertEqual(host._agent_service.snapshot_calls, 1)
            self.assertEqual(root.resolve(strict=True).parent, parent)


class ArtifactSnapshotContracts(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).resolve().parents[1] / "agent_workspace").resolve(strict=True)
        self.owner = tempfile.TemporaryDirectory(prefix="artifact-snapshot-contract-", dir=self.parent)
        self.root = Path(self.owner.name).resolve(strict=True)
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.store = ArtifactStore(self.root / "delivery"); self.cid = str(uuid4())
        self.target = self.root / "selected.bin"; self.target.write_bytes(b"ORIGINAL_OWNED_TARGET")

    def tearDown(self):
        self.assertEqual(self.root.resolve(strict=True).parent, self.parent)
        self.owner.cleanup()

    def seed(self, raw=b"owned"):
        (self.workspace / "owned.bin").write_bytes(raw)
        metadata = self.store.register(self.cid, {"passed": True, "path": "owned.bin",
            "sha256": hashlib.sha256(raw).hexdigest()}, WorkspaceFiles(self.workspace))
        return metadata, self.store._target(self.cid, metadata["reference"])

    def host(self, metadata, raw=b"owned"):
        class Host:
            _current_id = self.cid
            _closed = False
            agent_artifact_page = ChatController.agent_artifact_page
            export_agent_artifact = ChatController.export_agent_artifact
        host = Host()
        host._agent_service = SimpleNamespace(artifact_snapshot=Mock(return_value=(metadata, raw)),
                                             artifact_page=Mock(side_effect=AssertionError("unexpected paged fallback")))
        return host

    def assert_preserved(self):
        self.assertEqual(self.target.read_bytes(), b"ORIGINAL_OWNED_TARGET")
        self.assertEqual(list(self.root.glob(".aura-export-*")), [])

    def test_small_empty_and_exact_limit_use_real_service_snapshot(self):
        for raw in (b"", b"owned", b"X" * MAX_ARTIFACT_BYTES):
            with self.subTest(size=len(raw)):
                metadata, _ = self.seed(raw)
                # This real host method only accesses its owned ArtifactStore;
                # no service constructor, execution runtime or worker is started.
                service = object.__new__(AgentService); service._artifact_store = self.store
                host = self.host(metadata, raw); host._agent_service = service
                self.assertEqual(service.artifact_snapshot(self.cid, metadata["reference"]), (metadata, raw))
                self.assertTrue(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
                self.assertEqual(self.target.read_bytes(), raw)
                self.assertEqual(list(self.root.glob(".aura-export-*")), [])

    def test_wrong_cid_reference_changed_and_oversize_source_reject(self):
        metadata, copied = self.seed()
        for cid, reference in ((str(uuid4()), metadata["reference"]), (self.cid, "artifact_" + "b" * 64)):
            with self.assertRaises(ValueError): self.store.verified_snapshot(cid, reference)
        copied.write_bytes(b"WRONG")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_COPY_CORRUPTED"):
            self.store.verified_snapshot(self.cid, metadata["reference"])
        copied.write_bytes(b"X" * (MAX_ARTIFACT_BYTES + 1))
        with patch.object(Path, "open", side_effect=AssertionError("oversize file read")), \
             self.assertRaisesRegex(ValueError, "ARTIFACT_COPY_CORRUPTED"):
            self.store.verified_snapshot(self.cid, metadata["reference"])
        self.assert_preserved()

    def test_mobile_get_validates_each_page_and_snapshot_bytes_are_not_cached(self):
        metadata, copied = self.seed(b"abcdef")
        first = self.store.get(self.cid, metadata["reference"], offset=0, limit=2)
        self.assertEqual(base64.b64decode(first["data_base64"]), b"ab")
        self.assertEqual(first["next_offset"], 2)
        saved_metadata, snapshot = self.store.verified_snapshot(self.cid, metadata["reference"])
        copied.write_bytes(b"WRONG!")
        self.assertEqual(snapshot, b"abcdef")
        self.assertEqual(saved_metadata, metadata)
        with self.assertRaises(ValueError): self.store.get(self.cid, metadata["reference"], offset=2, limit=2)
        with self.assertRaises(ValueError): self.store.verified_snapshot(self.cid, metadata["reference"])

    def test_store_and_controller_reject_malformed_snapshot_metadata(self):
        metadata, _ = self.seed()
        variants = ({**metadata, "bytes": True}, {**metadata, "bytes": MAX_ARTIFACT_BYTES + 1},
                    {**metadata, "reference": "artifact_" + "b" * 64}, {**metadata, "sha256": "0" * 64},
                    {**metadata, "name": "../private"}, {**metadata, "mime": "invalid"},
                    {**metadata, "verification_scope": "bad\nvalue"}, {"reference": metadata["reference"]})
        for value in variants:
            with self.subTest(value=list(value)):
                with self.store._connect() as connection:
                    connection.execute("UPDATE artifacts SET metadata=? WHERE conversation_id=? AND reference=?",
                        (json.dumps(value), self.cid, metadata["reference"]))
                with self.assertRaises(ValueError): self.store.verified_snapshot(self.cid, metadata["reference"])
                host = self.host(value)
                self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
                host._agent_service.artifact_page.assert_not_called(); self.assert_preserved()

    def test_failed_or_invalid_snapshot_never_retries_via_pages(self):
        metadata, _ = self.seed()
        variants = (None, (), (metadata,), (metadata, bytearray(b"owned")), (metadata, b"WRONG"))
        for value in variants:
            host = self.host(metadata); host._agent_service.artifact_snapshot.return_value = value
            self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
            host._agent_service.artifact_snapshot.assert_called_once(); host._agent_service.artifact_page.assert_not_called()
            self.assert_preserved()
        for error in (OSError("owned IO"), ValueError("owned changed"), RuntimeError("owned unexpected")):
            host = self.host(metadata); host._agent_service.artifact_snapshot.side_effect = error
            if isinstance(error, RuntimeError):
                with self.assertRaises(RuntimeError): host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid)
            else:
                self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
            host._agent_service.artifact_page.assert_not_called(); self.assert_preserved()

    def test_legacy_service_or_missing_service_retains_page_fallback(self):
        metadata, _ = self.seed()
        for missing in (False, True):
            host = self.host(metadata)
            page = Mock(side_effect=self.store.get)
            if missing:
                del host._agent_service
                host.agent_artifact_page = page
            else:
                host._agent_service = SimpleNamespace(artifact_page=page)
            self.assertTrue(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
            self.assertEqual(self.target.read_bytes(), b"owned"); page.assert_called_once()

    def test_changed_cid_or_shutdown_before_replace_preserves_target(self):
        metadata, _ = self.seed()
        host = self.host(metadata)
        def changed(*_): host._current_id = str(uuid4()); return metadata, b"owned"
        host._agent_service.artifact_snapshot.side_effect = changed
        self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid)); self.assert_preserved()
        for closed in (False, True):
            host = self.host(metadata)
            def fsync(_):
                if closed: host._closed = True
                else: host._current_id = str(uuid4())
            with patch("desktop_pet.chat_controller.os.fsync", side_effect=fsync):
                self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
            self.assert_preserved()
        host = self.host(metadata); host._closed = True
        self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
        host._agent_service.artifact_snapshot.assert_not_called()

    def test_fsync_or_replace_failure_cleans_temporary_and_preserves_target(self):
        metadata, _ = self.seed()
        for name in ("fsync", "replace"):
            host = self.host(metadata)
            with patch("desktop_pet.chat_controller.os." + name, side_effect=OSError("owned failure")):
                self.assertFalse(host.export_agent_artifact(metadata["reference"], self.target, conversation_id=self.cid))
            self.assert_preserved()


if __name__ == "__main__": unittest.main()
