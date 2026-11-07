"""Read-only research authority tests; no models, child processes or registration."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents.research_delegation import ResearchAuthority, ResearchScope, ResearchError


class ResearchScopeTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="research-scope-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.allowed = self.root / "allowed"; self.allowed.mkdir()
        (self.allowed / "a.txt").write_text("Alpha factual source.", encoding="utf-8")
        (self.allowed / "b.txt").write_text("Beta independent source.", encoding="utf-8")
        (self.root / "outside.txt").write_text("Outside permission.", encoding="utf-8")
        self.files = WorkspaceFiles(self.allowed)
        self.active = True
        self.cancel = threading.Event()
        self.authority = self.make()
        self.scope = self.authority.open_scope(["a.txt", "b.txt"])
        self.refs = [row["source_id"] for row in self.authority.manifest(self.scope)]
        self.child = "research_" + uuid4().hex

    def make(self):
        return ResearchAuthority(self.files, parent_id=str(uuid4()), conversation_id=str(uuid4()), revision_id="revision-one",
            authorization_current=lambda: self.active, cancellation_event=self.cancel)

    def answer(self, read, **changes):
        value = {"conclusion": "Model-authored claim remains unverified.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]}
        value.update(changes)
        return json.dumps(value)

    def test_opaque_scope_and_narrowing_cannot_enlarge_existing_file_permissions(self):
        with self.assertRaises(TypeError): ResearchScope()
        with self.assertRaises(TypeError): json.dumps(self.scope)
        with self.assertRaises(ResearchError): self.authority.manifest({"sources": self.refs})
        with self.assertRaises(ResearchError): self.make().manifest(self.scope)
        child_scope = self.authority.derive(self.scope, [self.refs[0]])
        self.assertTrue(self.authority.is_subscope(self.scope, child_scope))
        for invalid in (["src_" + "0" * 32], [self.refs[0], self.refs[0]], [{}]):
            with self.subTest(invalid=invalid), self.assertRaises(ResearchError): self.authority.derive(child_scope, invalid)
        with self.assertRaises(ResearchError): self.authority.read_excerpt(child_scope, self.child, self.refs[1])
        with self.assertRaises(ResearchError): self.authority.open_scope([str(self.root / "outside.txt")])

    def test_issued_read_and_current_source_are_proof_not_conclusion_text(self):
        read = self.authority.read_excerpt(self.scope, self.child, self.refs[0])
        result = self.authority.validate_conclusion(self.scope, self.child, self.answer(read))
        self.assertEqual("reference_ready", result["status"])
        self.assertTrue(result["reference_only"] and result["source_references_verified"])
        self.assertFalse(result["claims_verified"])
        self.assertNotIn("completed", result)
        (self.allowed / "a.txt").write_text("Changed source.", encoding="utf-8")
        with self.assertRaisesRegex(ResearchError, "RESEARCH_SOURCE_CHANGED"):
            self.authority.validate_conclusion(self.scope, self.child, self.answer(read))

    def test_read_ids_cannot_cross_child_scope_parent_cid_or_revision(self):
        narrow = self.authority.derive(self.scope, [self.refs[0]])
        read = self.authority.read_excerpt(narrow, self.child, self.refs[0])
        for scope, child in ((self.scope, self.child), (narrow, "research_" + uuid4().hex)):
            with self.assertRaises(ResearchError): self.authority.validate_conclusion(scope, child, self.answer(read))
        other = self.make(); foreign = other.open_scope(["a.txt"])
        with self.assertRaises(ResearchError): other.validate_conclusion(foreign, self.child, self.answer(read))

    def test_forged_quote_or_schema_cannot_be_promoted_to_a_verified_reference(self):
        read = self.authority.read_excerpt(self.scope, self.child, self.refs[0])
        for answer in (self.answer(read, citations=[{"read_id": "forged", "quote": read["text"]}]),
                       self.answer(read, citations=[{"read_id": read["read_id"], "quote": "Imagined fact"}]),
                       self.answer(read, permission="write"), '{"conclusion":"x","conclusion":"y","citations":[]}',
                       "[" * 2000):
            with self.subTest(prefix=answer[:30]), self.assertRaises(ResearchError):
                self.authority.validate_conclusion(self.scope, self.child, answer)

    def test_full_source_is_redacted_before_pagination_and_same_view_validates_quotes(self):
        secret = "SYNTHETIC_KNOWN_SECRET_92"
        path = self.allowed / (secret + ".txt")
        path.write_text("prefix " + secret + " suffix", encoding="utf-8")
        self.authority.bind_model_secrets([secret])
        scope = self.authority.open_scope([str(path)])
        manifest = self.authority.manifest(scope)
        self.assertNotIn(secret, json.dumps(manifest))
        reference = manifest[0]["source_id"]
        fragments = [self.authority.read_excerpt(scope, self.child, reference, offset, 4)["text"] for offset in range(0, manifest[0]["characters"], 4)]
        self.assertEqual("prefix [redacted] suffix", "".join(fragments))
        read = self.authority.read_excerpt(scope, self.child, reference)
        result = self.authority.validate_conclusion(scope, self.child,
            self.answer(read, conclusion=secret, citations=[{"read_id": read["read_id"], "quote": "prefix " + secret + " suffix"}]))
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn("[redacted]", result["citations"][0]["quote"])
        with self.assertRaises(ResearchError): self.authority.bind_model_secrets(["late"])

    def test_parent_revoke_cancel_and_close_block_next_read_and_merge(self):
        for mode in ("revoke", "cancel", "close"):
            with self.subTest(mode=mode):
                self.active = True; self.cancel.clear()
                authority = self.make(); scope = authority.open_scope(["a.txt"])
                ref = authority.manifest(scope)[0]["source_id"]
                read = authority.read_excerpt(scope, self.child, ref)
                if mode == "revoke": self.active = False
                elif mode == "cancel": self.cancel.set()
                else: authority.close()
                with self.assertRaises(ResearchError): authority.read_excerpt(scope, self.child, ref)
                with self.assertRaises(ResearchError): authority.validate_conclusion(scope, self.child, self.answer(read))

    def test_close_does_not_wait_for_host_read_and_late_excerpt_is_not_exported(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        values, errors = [], []
        original = self.files.read_bytes
        def paused(*args, **kwargs): entered.set(); release.wait(5); return original(*args, **kwargs)
        def read():
            try: values.append(self.authority.read_excerpt(self.scope, self.child, self.refs[0]))
            except ResearchError as exc: errors.append(exc.code)
            finally: finished.set()
        with patch.object(self.files, "read_bytes", paused):
            thread = threading.Thread(target=read); thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.authority.close()
                self.assertFalse(finished.is_set())
                release.set(); thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual([], values)
                self.assertEqual(["RESEARCH_PARENT_INACTIVE"], errors)
            finally: release.set(); thread.join(3)


if __name__ == "__main__": unittest.main()
