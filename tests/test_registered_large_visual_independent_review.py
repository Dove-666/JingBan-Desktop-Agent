"""Independent metadata-oracle mutants only; no model, rendering or UI."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.qa_registered_large_document import oracle


class LargeVisualOracleIndependentReviewTests(unittest.TestCase):
    def setUp(self):
        self.owned=tempfile.TemporaryDirectory(prefix="large-visual-oracle-")
        self.root=Path(self.owned.name)
        # These are opaque prevalidated artifact bytes for this pure metadata
        # function. This test makes no Office/renderer/model success claim.
        (self.root/"visual.docx").write_bytes(b"OWNED_PREVALIDATED_SOURCE")
        (self.root/"visual.pdf").write_bytes(b"%PDF-1.7 OWNED_PREVALIDATED_OUTPUT")
        self.expected={"shape":"circle","color":"orange","label":"AURA_12345ABCDE"}
        (self.root/"answer.json").write_text(json.dumps(self.expected),encoding="utf-8")
        self.source_hash=hashlib.sha256((self.root/"visual.docx").read_bytes()).hexdigest()
        pdf=hashlib.sha256((self.root/"visual.pdf").read_bytes()).hexdigest()
        page={"reference":"page_"+"a"*32,"page":1,"pdf_sha256":pdf,"png_sha256":"b"*64,"width":100,"height":80}
        review={"reference":page["reference"],"page":1,"pdf_sha256":pdf,"png_sha256":page["png_sha256"],
            "sent_image_sha256":"c"*64,"sent_content_type":"image/jpeg","sent_width":100,"sent_height":80,
            "derived":True,"scope":"model_visual_observation","reference_only":True,"visual_layout_verified":False,
            "whole_document_verified":False,"observation":"A source-bound reference, not a goal verdict"}
        self.history=[{"role":"tool","name":"render_document","content":json.dumps({"preview_pages":[page]})},
                      {"role":"tool","name":"inspect_document_page","content":json.dumps(review)}]

    def tearDown(self): self.owned.cleanup()

    def check(self, history=None): return oracle(self.root,self.expected,self.source_hash,self.history if history is None else history)[0]

    def test_more_than_one_current_readonly_observation_is_not_a_task_failure(self):
        history=self.history+[deepcopy(self.history[-1])]
        self.assertTrue(self.check(history)["passed"])

    def test_strict_three_field_answer_rejects_duplicate_json_members(self):
        (self.root/"answer.json").write_text('{"shape":"square","shape":"circle","color":"orange","label":"AURA_12345ABCDE"}',encoding="utf-8")
        self.assertFalse(self.check()["passed"],"Last-value-wins JSON parsing hid a fourth answer member")

    def test_nonhex_transport_hash_and_invalid_sent_dimensions_are_not_current_image_evidence(self):
        for mutation in ({"sent_image_sha256":"z"*64},{"sent_width":0},{"sent_height":True}):
            history=deepcopy(self.history); value=json.loads(history[-1]["content"]); value.update(mutation)
            history[-1]["content"]=json.dumps(value)
            with self.subTest(mutation=mutation):
                self.assertFalse(self.check(history)["passed"],"Invalid transport identity was accepted as visual evidence")

    def test_render_witness_must_bind_current_pdf_and_exact_page_number(self):
        for mutation in ({"pdf_sha256":"d"*64},{"page":2}):
            history=deepcopy(self.history); value=json.loads(history[0]["content"]); value["preview_pages"][0].update(mutation)
            history[0]["content"]=json.dumps(value)
            with self.subTest(mutation=mutation):
                self.assertFalse(self.check(history)["passed"],"Reference/png-only match failed to bind PDF/page")


if __name__=="__main__":unittest.main()
