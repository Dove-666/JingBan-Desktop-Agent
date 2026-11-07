"""Production host/SDK tools/judge with fixed owned network doubles only.

This proves projection, fresh source evidence and completion contracts. It is
not a real-model or browser-rendering success claim. No user window is opened.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/"agent_backend/nanobot"
sys.path.insert(0,str(BACKEND/"tests"))
from test_goal_contract_worker import SeparateGoalModel
from desktop_pet.agents import AgentRequest,AgentService,AgentSettings
from desktop_pet.agents import service as host

SDK=BACKEND/".venv/Scripts/python.exe"
URL="https://docs.python.org/3/tutorial/controlflow.html#default-argument-values"
FACT="Default arguments are evaluated once at function definition; mutable defaults are shared."
SOURCE="Owned public source heading\n"+"x"*17000+"\n"+FACT+"\n"+"y"*5000
ANSWER="The owned source states that default arguments are evaluated once when the function is defined, and mutable defaults are shared."


class SourceModel(SeparateGoalModel):
    def __init__(self,steps,*,browser=False,wrong_shortcut=False):
        self.browser,self.wrong_shortcut=browser,wrong_shortcut
        super().__init__(steps,judge_mode="owned-source")

    def judge(self,snapshot):
        policy=self.judge_calls[-1]["messages"][0]["content"]
        assert "facts.observed.effect_count" in policy and "facts.pending_effects" in policy
        observed=snapshot["facts"]["observed"]
        assert all(row["tool"]!="recall_context" for row in observed["observations"])
        supporting=[]
        for row in observed["observations"]:
            if row.get("truncated"):continue
            observation=row.get("observation",{});result=observation.get("result")
            result=json.loads(result) if isinstance(result,str) else result
            if isinstance(result,dict) and result.get("url")==URL and FACT in result.get("text",""):
                if self.browser and row["tool"]=="browser_navigate":supporting.append(row["id"])
                if not self.browser and row["tool"]=="web_fetch" and observation["arguments"].get("offset")==16000:
                    supporting.append(row["id"])
        assert supporting,"No fresh actual source callback supports the requested fact"
        assert bool(observed["effect_count"])==self.browser
        return json.dumps({"schema":"aura.goal_verdict.v1","goal_id":snapshot["goal_id"],
            "evidence_id":snapshot["evidence_id"],"answer_sha256":snapshot["answer_sha256"],
            "status":"answer_only" if self.wrong_shortcut else "satisfied",
            "evidence_ids":[] if self.wrong_shortcut else supporting})


@unittest.skipUnless(SDK.is_file(),"isolated production SDK environment missing")
class WebPageProjectionWorkerReviewTests(unittest.TestCase):
    def run_owned(self,*,browser=False,wrong_shortcut=False):
        parent=(ROOT/"agent_workspace").resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="web-projection-worker-",dir=parent) as td:
            workspace=Path(td).resolve(strict=True);self.assertEqual(parent,workspace.parent)
            original_source={"url":URL,"text":SOURCE,"links":[],"source_truncated":False,
                "source_sha256":hashlib.sha256(SOURCE.encode()).hexdigest(),"untrusted_external_data":True}
            fixture=workspace/"owned-source.json";fixture.write_text(json.dumps(original_source),encoding="utf-8")
            shim=workspace/"owned-worker.py"
            # Only the network source boundary is replaced. Original worker,
            # registry, ContextGovernor, GoalEvidence and goal guards execute.
            shim.write_text("import asyncio,importlib.util,json,sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0,{str(BACKEND)!r})\nimport public_web,browser_session\n"
                f"source=json.loads(Path({str(fixture)!r}).read_text(encoding='utf-8'))\n"
                "def fetch(url):\n"
                "    if url!=source['url']:raise public_web.PublicWebError('INVALID_URL')\n"
                "    return json.loads(json.dumps(source))\n"
                "public_web._fetch_source=fetch\n"
                "async def navigate(self,url):\n"
                "    if url!=source['url']:raise browser_session.BrowserError('BROWSER_PUBLIC_TARGET_REQUIRED')\n"
                f"    return json.dumps({{'url':{URL!r},'text':{FACT!r},'status':'observed','effect_state':'confirmed','pending_effect':False}})\n"
                "browser_session.BrowserSession.navigate=navigate\n"
                f"spec=importlib.util.spec_from_file_location('owned_production_worker',{str(BACKEND/'worker.py')!r})\n"
                "worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)\nasyncio.run(worker.main())\n",encoding="utf-8")
            seen=[]
            def continuation(body):
                tools=[m for m in body['messages'] if m['role']=='tool']
                projected=json.loads(tools[-1]['content'])
                self.assertEqual(URL,projected['web_page']['url'])
                self.assertEqual(0,projected['web_page']['offset'])
                self.assertEqual(16000,projected['web_page']['next_offset'])
                self.assertNotIn(FACT,projected['excerpt'])
                hint=projected['read_next_page'];seen.append(hint)
                self.assertEqual('web_fetch',hint['tool'])
                self.assertEqual(4000,hint['arguments']['limit'])
                return hint['tool'],hint['arguments']
            steps=[('browser_navigate',{'url':URL}),ANSWER] if browser else [('web_fetch',{'url':URL,'limit':16000}),continuation,ANSWER]
            model=SourceModel(steps,browser=browser,wrong_shortcut=wrong_shortcut)
            service=AgentService(checkpoint_path=workspace.parent/(workspace.name+'.checkpoints.sqlite3'));handles=[]
            try:
                with patch.object(host,'_WORKER_PATH',shim):
                    result=service.execute(AgentSettings(python_executable=str(SDK),workspace=str(workspace),timeout_seconds=25),
                        'FAKE_OWNED_WEB_PROJECTION_KEY',AgentRequest('Read '+URL+' and explain default arguments. Do not save or execute anything.','agent'),
                        {'api':{'base_url':model.base_url,'model':'owned-source-model'},'conversation_id':str(uuid4()),'messages':[]},
                        worker_lifecycle_callback=handles.append)
            finally:
                service.shutdown();model.close()
            self.assertEqual([],model.errors)
            self.assertEqual(len(steps),len(model.planner_calls))
            self.assertTrue(model.judge_calls)
            self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
            self.assertEqual(original_source,json.loads(fixture.read_text(encoding='utf-8')))
            if not browser:self.assertEqual(1,len(seen))
            return result

    def test_archived_web_page_hint_drives_fresh_body_read_and_cited_judge_completion(self):
        result=self.run_owned()
        self.assertEqual('completed',result.status,result.reply)
        self.assertTrue(result.acceptance['passed'])
        self.assertEqual('satisfied',result.acceptance['judge_status'])

    def test_informational_browser_effect_needs_satisfied_with_actual_evidence_ids(self):
        result=self.run_owned(browser=True)
        self.assertEqual('completed',result.status,result.reply)
        self.assertEqual('satisfied',result.acceptance['judge_status'])

    def test_incorrect_answer_only_after_browser_effect_is_still_rejected_by_host(self):
        result=self.run_owned(browser=True,wrong_shortcut=True)
        self.assertEqual('needs_user',result.status)
        self.assertFalse(result.acceptance['passed'])
        self.assertIn('GOAL_ANSWER_ONLY_INVALID',result.acceptance['issue_codes'])


if __name__=='__main__':unittest.main()
