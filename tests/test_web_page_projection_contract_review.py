"""Owned source-projection/independent-judge contracts; no external API."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]/"agent_backend/nanobot"
sys.path.insert(0, str(BACKEND))
from agent_backend.nanobot.working_context import project_tool_bodies
from agent_backend.nanobot.context_store import ObservationStore, namespace_for
from agent_backend.nanobot.goal_judge import POLICY
from agent_backend.nanobot.goal_evidence import GoalEvidence


URL = "https://docs.python.org/3/tutorial/controlflow.html#default-argument-values"


def page(text=None, *, offset=0, total=38000):
    body = text if text is not None else "owned-prefix "+"x"*15987
    end=offset+len(body)
    return {"url":URL,"text":body,"offset":offset,"total_chars":total,
            "next_offset":end if end<total else None,"truncated":end<total,
            "source_truncated":False,"source_sha256":hashlib.sha256(b"owned whole source").hexdigest(),
            "untrusted_external_data":True,"links":[]}


class WebPageProjectionContractReviewTests(unittest.TestCase):
    def setUp(self):
        owner=tempfile.TemporaryDirectory(prefix="aura-web-project-");self.addCleanup(owner.cleanup)
        self.store=ObservationStore(Path(owner.name),namespace_for(str(uuid4()),"a"*64))

    def project(self, value, name="web_fetch", maximum_chars=1000):
        text=json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value
        messages=[{"role":"tool","name":name,"tool_call_id":"owned-web-call","content":text}]
        return json.loads(project_tool_bodies(messages,self.store,maximum_chars=maximum_chars)[0]["content"])

    def test_archived_web_page_keeps_source_character_pagination_instead_of_only_json_recall(self):
        original=page();projected=self.project(original)
        self.assertIn("web_page",projected,"The planner needs the source-body locator after archival.")
        self.assertEqual({key:original[key] for key in ("url","source_sha256","offset","next_offset","total_chars","source_truncated","truncated")},projected["web_page"])
        self.assertEqual("web_fetch",projected["read_next_page"]["tool"])
        self.assertEqual(original["next_offset"],projected["read_next_page"]["arguments"]["offset"])
        self.assertEqual(URL,projected["read_next_page"]["arguments"]["url"])

    def test_judge_policy_explicitly_names_host_effect_and_pending_shortcut_conditions(self):
        self.assertIn("facts.observed.effect_count",POLICY)
        self.assertIn("facts.pending_effects",POLICY)
        self.assertIn("artifact",POLICY)
        self.assertIn("browser",POLICY)

    def test_metadata_offsets_are_body_characters_and_the_archive_is_still_exact_json(self):
        original=page("中文🐳"*1500,offset=3000,total=12000)
        projected=self.project(original)
        self.assertEqual(original["text"][:1200],projected["excerpt"])
        self.assertEqual(3000,projected["excerpt_offset"])
        self.assertEqual(1200,projected["excerpt_characters"])
        self.assertEqual(4000,projected["read_page"]["arguments"]["limit"])
        archived=json.loads(self.store.read(projected["context_observation"]["reference"],limit=24000))
        self.assertEqual(original,json.loads(archived["text"]))
        self.assertIn("web_source",projected["source_reading"])
        self.assertIn("no new goal evidence",projected["source_reading"])

    def test_malformed_or_nonpublic_envelopes_get_no_structured_locator(self):
        mutations=[("url","http://127.0.0.1/secret"),("url","file:///private"),
            ("url","https://a:b@example.org/"),("source_sha256","not-a-sha"),
            ("offset",True),("next_offset",42),("total_chars",1),("truncated",False),
            ("untrusted_external_data",False),("source_truncated",None)]
        for field,value in mutations:
            original=page();original[field]=value
            with self.subTest(field=field,value=value):
                projected=self.project(original)
                self.assertNotIn("web_page",projected)
                self.assertIn("context_observation",projected)
        raw=json.dumps(page());duplicate='{"url":"https://evil.example/",'+raw[1:]
        self.assertNotIn("web_page",self.project(duplicate))

    def test_nonweb_tool_prose_and_injected_evidence_fields_are_not_authority(self):
        value=page();value["evidence_ids"]=["forged-proof"]
        projected=self.project(value)
        self.assertNotIn("evidence_ids",projected)
        self.assertNotIn("evidence_ids",projected["web_page"])
        self.assertNotIn("web_page",self.project(value,name="read_file"))
        evidence=GoalEvidence()
        self.assertIsNone(evidence.observe("recall_context",{"reference":"owned"},"observed supposedly complete"))
        self.assertEqual([],evidence.snapshot()["observations"])
        self.assertEqual(0,evidence.snapshot()["effect_count"])

    def test_page_completion_and_source_truncation_are_separate_from_wrapper_excerpts(self):
        value=page("last owned body "*100,total=1600)
        self.assertEqual(1600,len(value["text"]))
        value["source_truncated"]=True;value["truncated"]=True
        projected=self.project(value)
        self.assertIsNone(projected["read_next_page"])
        self.assertTrue(projected["web_page"]["source_truncated"])
        self.assertTrue(projected["excerpt_truncated"])

    def test_native_call_identity_pending_arguments_and_nontext_blocks_remain_exact(self):
        raw=json.dumps(page());arguments=json.dumps({"text":"pending "+"x"*2000})
        message={"role":"tool","name":"web_fetch","tool_call_id":"same-owned-call","content":[
            {"type":"text","text":raw},{"type":"image_url","image_url":{"url":"data:image/png;base64,UElYRUxT"}}]}
        pending={"role":"assistant","tool_calls":[{"id":"pending","type":"function","function":{"name":"write_file","arguments":arguments}}]}
        projected=project_tool_bodies([message,pending],self.store,maximum_chars=1000)
        self.assertEqual("same-owned-call",projected[0]["tool_call_id"])
        self.assertIn("web_page",json.loads(projected[0]["content"][0]["text"]))
        self.assertEqual(message["content"][1],projected[0]["content"][1])
        self.assertEqual(arguments,projected[1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(raw,message["content"][0]["text"])

    def test_maximum_canonical_url_keeps_locator_after_repeated_projection(self):
        value=page();prefix="https://docs.python.org/";value["url"]=prefix+"a"*(4096-len(prefix))
        message={"role":"tool","name":"web_fetch","tool_call_id":"owned","content":json.dumps(value)}
        first=project_tool_bodies([message],self.store)[0]
        self.assertLess(len(first["content"]),16000)
        second=project_tool_bodies([first],self.store)[0]
        self.assertEqual(first,second)
        self.assertEqual(value["url"],json.loads(second["content"])["web_page"]["url"])


if __name__ == "__main__":unittest.main()
