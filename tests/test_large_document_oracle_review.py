"""Independent bad-observation controls for the configured visual task oracle."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.qa_registered_large_document import oracle


class LargeDocumentOracleReviewTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(); self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name)
        self.expected = {"shape": "circle", "color": "orange", "label": "AURA_OWNED_MARKER"}
        (self.root/"answer.json").write_text(json.dumps(self.expected), encoding="utf-8")
        (self.root/"visual.docx").write_bytes(b"OWNED_ORACLE_SOURCE")
        (self.root/"visual.pdf").write_bytes(b"OWNED_ORACLE_PDF")
        self.source_hash = hashlib.sha256((self.root/"visual.docx").read_bytes()).hexdigest()
        self.page = {"reference": "preview_owned", "png_sha256": "a"*64,
            "pdf_sha256": hashlib.sha256((self.root/"visual.pdf").read_bytes()).hexdigest(), "page": 1, "width": 100, "height": 80}
        self.review = {**self.page, "pdf_sha256": hashlib.sha256((self.root/"visual.pdf").read_bytes()).hexdigest(),
            "page": 1, "scope": "model_visual_observation", "reference_only": True, "visual_layout_verified": False,
            "whole_document_verified": False, "derived": True, "sent_content_type": "image/jpeg",
            "sent_image_sha256": "b"*64, "sent_width": 100, "sent_height": 80, "observation": "Owned visual reference"}

    def check(self, review=None, extra=()):
        history = [{"role": "tool", "name": "render_document", "content": json.dumps({"preview_pages": [self.page]})},
            {"role": "tool", "name": "inspect_document_page", "content": json.dumps(self.review if review is None else review)}, *extra]
        return oracle(self.root, self.expected, self.source_hash, history)[0]

    def test_valid_reference_and_visual_facts_pass_then_wrong_answer_fails(self):
        self.assertTrue(self.check()["passed"])
        (self.root/"answer.json").write_text(json.dumps({**self.expected, "color": "blue"}), encoding="utf-8")
        self.assertFalse(self.check()["checks"]["EXACT_VISUAL_FACTS"])

    def test_stale_pdf_or_foreign_preview_or_untransformed_image_never_satisfies_derived_observation(self):
        for change in ({"pdf_sha256": "c"*64}, {"reference": "preview_foreign"}, {"derived": False},
                       {"sent_image_sha256": "a"*64}, {"visual_layout_verified": True}):
            with self.subTest(change=change):
                self.assertFalse(self.check({**self.review, **change})["checks"]["CURRENT_PDF_DERIVED_PAGE_OBSERVATION"])

    def test_changed_input_or_unexpected_output_is_rejected(self):
        (self.root/"visual.docx").write_bytes(b"CHANGED")
        self.assertFalse(self.check()["checks"]["SOURCE_DOCX_UNCHANGED"])
        (self.root/"unexpected.png").write_bytes(b"OWNED")
        self.assertFalse(self.check()["checks"]["ONLY_DECLARED_FILES"])

    def test_marker_in_text_read_cannot_be_credited_as_a_pixels_only_task(self):
        read = {"role": "tool", "name": "read_document", "content": json.dumps({
            "path": "visual.pdf", "text": self.expected["label"]})}
        self.assertFalse(self.check(extra=(read,))["checks"]["NO_TEXT_READ_LEAKS_VISUAL_MARKER"])

    def test_parent_image_data_is_rejected(self):
        self.assertFalse(self.check(extra=({"role": "user", "content": "data:image/png;base64,OWNED"},))["passed"])

    def test_two_explicit_current_page_observations_are_allowed_by_the_task(self):
        again = {"role": "tool", "name": "inspect_document_page", "content": json.dumps(self.review)}
        self.assertTrue(self.check(extra=(again,))["passed"])


if __name__ == "__main__": unittest.main()
