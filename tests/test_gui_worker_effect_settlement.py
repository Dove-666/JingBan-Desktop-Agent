"""Actual SDK/worker/GuiBridge settlement with synthetic JSONL child I/O.

No native GUI child or model is started. The planner/judge use owned localhost
HTTP, and both host and worker enforce literal loopback networking.
"""
import json
import unittest
from unittest.mock import patch

from tests import test_gui_no_effect_recovery_offline_review as offline


@unittest.skipUnless(offline.before.fixture.PYTHON.is_file(), "isolated SDK environment unavailable")
class GuiWorkerEffectSettlementTests(unittest.TestCase):
    setUp = offline.before.fixture.HarnessWorkerTests.setUp
    tearDown = offline.before.fixture.HarnessWorkerTests.tearDown
    start = offline.before.fixture.HarnessWorkerTests.start
    observations = []

    def wrapper(self):
        cache = offline.seed_owned_tokenizer_cache(self.workspace)
        wrapper, output = offline.before.GuiNoEffectRecoveryBeforeTests.wrapper(self, "actual_bridge")
        code = offline.install_owned_cache(wrapper.read_text(encoding="utf-8"), cache)
        head, rest = code.split("class OwnedGui(worker.GuiBridge):", 1)
        _, tail = rest.split("worker.GuiBridge=OwnedGui\n", 1)
        transport = r'''
counts.update(spawn_calls=0,effectful_runs=0,verify_only_runs=0,abort_frames=0,process_waits=0)
class OwnedInput:
    def __init__(self,process):self.process=process
    def write(self,raw):
        frame=json.loads(raw)
        if frame['type']=='abort':counts['abort_frames']+=1;return
        if frame['type']!='run':raise AssertionError('UNEXPECTED_FAKE_TRANSPORT_FRAME')
        identity=frame['id']
        if frame['options']['verifyOnly']:
            counts['verify_only_runs']+=1;counts['verification_calls']+=1
            final={'type':'final','id':identity,'status':'needs_user','summary':'Owned observation did not verify goal.',
                'steps':0,'actions_dispatched':0,'effect_state':'not_dispatched',
                'verification':report('failed',reason='GOAL_STATE_NOT_MET')}
            self.process.feed(final)
        else:
            counts['effectful_runs']+=1;counts['gui_calls']+=1
            if counts['effectful_runs']!=1:raise AssertionError('EFFECTFUL_RUN_REPLAYED')
            counts['input_calls']+=1
            self.process.feed({'type':'progress','id':identity,'stage':'acting','status':'running','action':'click','step':1})
            self.process.feed({'type':'final','id':identity,'status':'needs_user','summary':'owned malformed report',
                'steps':1,'verification':{'status':'verified'}})
    async def drain(self):pass
    def close(self):self.process.stdout.feed_eof()
class OwnedProcess:
    def __init__(self):
        self.stdin=OwnedInput(self);self.stdout=asyncio.StreamReader();self.returncode=None;self.pid=99999999
        self.feed({'type':'ready'})
    def feed(self,frame):self.stdout.feed_data((json.dumps(frame)+'\n').encode())
    async def wait(self):counts['process_waits']+=1;self.returncode=0;return 0
async def owned_spawn(*argv,**kwargs):
    if len(argv)!=2 or not str(argv[-1]).endswith('worker.mjs'):raise AssertionError('UNEXPECTED_REAL_CHILD_REQUEST')
    counts['spawn_calls']+=1
    return OwnedProcess()
asyncio.create_subprocess_exec=owned_spawn
'''
        code = head + transport + tail
        old = "raise worker.DesktopControlError('DESKTOP_UNAVAILABLE')"
        self.assertEqual(code.count(old), 1)
        code = code.replace(old, "return json.dumps({'status':'observed','snapshot_id':'a'*16,'window':{'title':'Owned fixture','process_id':1},'controls':[],'truncated':False})")
        code += "\nvalue=json.loads(OUTPUT.read_text(encoding='utf-8'))\nvalue['post_cleanup_transport_counts']=dict(counts)\nOUTPUT.write_text(json.dumps(value),encoding='utf-8')\n"
        wrapper.write_text(code, encoding="utf-8")
        return wrapper, output

    def test_malformed_final_keeps_ledger_uncertain_blocks_new_input_and_allows_independent_reads(self):
        fixture = offline.before.fixture
        wrapper, output = self.wrapper()
        checked = {}

        def after_second_input(body):
            observations = [m for m in body["messages"] if m.get("role") == "tool" and m.get("name") == "computer_use"]
            self.assertEqual(len(observations), 2)
            self.assertIn("GUI_PROTOCOL", observations[0]["content"])
            self.assertIn("ACTION_OUTCOME_UNCERTAIN", observations[1]["content"])
            checked["second_input_blocked_by_actual_ledger"] = True
            return fixture.tool("desktop_observe")

        def after_file_read(body):
            self.assertEqual(fixture.last_observation(body, "read_file")["text"], "DONE")
            self.assertEqual(fixture.last_observation(body, "desktop_observe")["status"], "observed")
            checked.update(real_file_read_passed=True, independent_readonly_observation_passed=True)
            return fixture.tool("verify_artifact", path="report.txt", kind="text")

        steps = [fixture.tool("computer_use", task="Inspect owned fixture context"),
                 fixture.tool("computer_use", task="A distinct next input intent"), after_second_input,
                 fixture.tool("write_file", path="report.txt", content="DONE"),
                 fixture.tool("read_file", path="report.txt"), after_file_read,
                 fixture.tool("finish_task", status="completed", summary="Owned file was read back; desktop effect remains unknown."),
                 "The owned file is verified; the desktop action remains unverified."]
        import fixture_goal_judge
        local_http = {"judge": 0}
        send_judge = fixture_goal_judge.send_judge

        def counted_judge(*args, **kwargs):
            local_http["judge"] += 1
            return send_judge(*args, **kwargs)

        with patch.object(fixture_goal_judge, "send_judge", counted_judge):
            client, model = self.start(steps, computer=True, worker_path=wrapper,
                task='Create report.txt containing exactly "DONE". Inspect only this owned fixture if needed.')
            terminal = client.terminal()
        self.assertEqual(terminal["type"], "final")
        self.assertEqual(client.process.wait(timeout=10), 0)
        value = json.loads(output.read_text(encoding="utf-8"))
        value.update(checked)
        value["terminal_status"] = terminal["status"]
        value["local_http_requests"] = {"planner": len(model.calls), "judge": local_http["judge"],
                                        "total": len(model.calls) + local_http["judge"]}
        self.observations.append(value)
        self.assertEqual((self.workspace / "report.txt").read_bytes(), b"DONE")
        self.assertEqual(value["terminal_status"], "needs_user")
        self.assertEqual(value["synthetic_counts"]["effectful_runs"], 1)
        self.assertEqual(value["synthetic_counts"]["input_calls"], 1)
        self.assertGreaterEqual(value["synthetic_counts"]["verify_only_runs"], 1)
        self.assertTrue(value["bridge_pending_effect"])
        self.assertEqual(value["bridge_effect_state"], "uncertain")
        self.assertIn("desktop", value["pending_effects"])
        self.assertEqual(value["synthetic_counts"]["blocked_network"], [])
        self.assertTrue(value["synthetic_counts"]["offline_tokenizer_ready"])
        self.assertEqual(value["live_input_effects"], 0)
        cleaned = value["post_cleanup_transport_counts"]
        self.assertEqual(cleaned["spawn_calls"], cleaned["process_waits"])
        self.assertEqual(cleaned["spawn_calls"], cleaned["abort_frames"])


if __name__ == "__main__":
    unittest.main()
