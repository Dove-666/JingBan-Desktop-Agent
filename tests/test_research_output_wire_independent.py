"""Independent actual SDK builder/HTTP review, with fixed synthetic data only.

The builder class is captured from research_worker.run itself, not reimplemented.
Real localhost failures exercise the normal isolated child and owned cleanup.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import unittest

from tests import test_research_sdk as fixture


BUILDER_PROBE = r'''
import asyncio,json,sys
from pathlib import Path
repo=Path(sys.argv[1]);sys.path.insert(0,str(repo/'agent_backend/nanobot'))
import research_worker
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.model_api import ModelAPICapabilities
class Captured(Exception):pass
captured=[]
original=OpenAICompatProvider.__init__
def capture(self,*args,**kwargs):
    captured.append(type(self));raise Captured()
async def main():
    OpenAICompatProvider.__init__=capture
    try:
        try:
            await research_worker.run({'child_id':'research_'+'1'*32,'task':'Fixed builder review',
                'sources':[{'source_id':'source_owned'}],
                'config':{'api_key':'SYNTHETIC_REVIEW_KEY','base_url':'http://127.0.0.1:1/v1','model':'owned-model'}})
        except Captured:pass
    finally:OpenAICompatProvider.__init__=original
    assert len(captured)==1
    provider=captured[0](api_key='SYNTHETIC_REVIEW_KEY',api_base='http://127.0.0.1:1/v1',
        default_model='owned-model',model_api=ModelAPICapabilities(),provider_name='custom')
    count=0
    try:
        for method in ('_build_kwargs','_build_responses_body'):
            for model in ('owned-model','gpt-5-owned'):
                for budget in (0,17):
                    for keyword in (False,True):
                        messages=[{'role':'user','content':'Fixed Unicode 中\U0001f30a input'}]
                        params=dict(messages=messages,tools=None,model=model,max_tokens=budget,
                            temperature=.1,reasoning_effort=None,tool_choice=None)
                        fn=getattr(provider,method)
                        result=fn(**params) if keyword else fn(*params.values())
                        keys=set(result)&{'max_tokens','max_completion_tokens','max_output_tokens'}
                        if budget==0:assert not keys,(method,model,keyword)
                        else:
                            expected='max_output_tokens' if method=='_build_responses_body' else (
                                'max_completion_tokens' if 'gpt-5' in model else 'max_tokens')
                            assert keys=={expected} and result[expected]==17,(method,model,keyword)
                        # Output default must not add execution/tool authority.
                        assert not result.get('tools')
                        assert result['model']==model
                        count+=1
    finally:await provider.aclose()
    print(json.dumps({'builder_cases':count,'passed':True}))
asyncio.run(main())
'''


@unittest.skipUnless(fixture.PYTHON.is_file(), "actual isolated SDK required")
class ResearchOutputWireIndependentTests(unittest.TestCase):
    setUp = fixture.ResearchSdkTests.setUp
    tearDown = fixture.ResearchSdkTests.tearDown
    specs = fixture.ResearchSdkTests.specs

    def test_actual_child_class_zero_and_positive_budgets_both_builders(self):
        repo = fixture.Path(__file__).resolve().parents[1]
        result = subprocess.run([str(fixture.PYTHON), "-I", "-u", "-c", BUILDER_PROBE, str(repo)],
            capture_output=True, timeout=20, check=False)
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace")[-1500:])
        self.assertEqual({"builder_cases": 16, "passed": True}, json.loads(result.stdout))

    def test_actual_length_is_not_accepted_or_replayed(self):
        self._failure_case("length")

    def test_actual_authentication_failure_is_not_replayed(self):
        self._failure_case("authentication")

    def test_actual_unavailable_failure_is_not_replayed(self):
        self._failure_case("unavailable")

    def _failure_case(self, mode):
        for mode in (mode,):
            with self.subTest(mode=mode):
                calls = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_): pass
                    def do_POST(self):
                        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        calls.append(body)
                        if mode == "length":
                            # Even syntactically valid structured text is incomplete if the
                            # provider declares truncation; it must not become evidence.
                            message = {"role": "assistant", "content": json.dumps({
                                "conclusion": "SYNTHETIC_REVIEW_KEY partial", "citations": []})}
                            if body.get("stream"):
                                chunks = [{"id": "owned-length", "object": "chat.completion.chunk", "created": 1,
                                    "model": "owned", "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                                    {"id": "owned-length", "object": "chat.completion.chunk", "created": 1,
                                    "model": "owned", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}]
                                raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                                    + "data: [DONE]\n\n").encode()
                                content_type = "text/event-stream"
                            else:
                                raw = json.dumps({"id": "owned-length", "object": "chat.completion", "created": 1,
                                    "model": "owned", "choices": [{"index": 0, "message": message,
                                    "finish_reason": "length"}]}).encode()
                                content_type = "application/json"
                            status = 200
                        else:
                            raw = b'{"error":{"message":"SYNTHETIC_REVIEW_KEY OWNED_PRIVATE_ERROR"}}'
                            content_type = "application/json"
                            status = 401 if mode == "authentication" else 503
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers(); self.wfile.write(raw)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
                thread.start()
                try:
                    group = fixture.ResearchGroup(self.authority, python_executable=str(fixture.PYTHON), config={
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "owned-wire-model",
                        "api_key": "SYNTHETIC_REVIEW_KEY"}, deadline_seconds=10)
                    self.groups.append(group)
                    values = group.run(self.scope, self.specs()[:1])
                    self.assertEqual(1, len(calls), mode)
                    self.assertEqual("unverified", values[0]["status"], values)
                    self.assertEqual("RESEARCH_MODEL_FAILED", values[0]["code"], values)
                    self.assertEqual(1, values[0]["model_requests"])
                    self.assertEqual(0, values[0]["model_responses"])
                    self.assertTrue(values[0]["worker_closed"])
                    self.assertTrue(group.cleanup_confirmed())
                    serialized = json.dumps(values)
                    for private in ("SYNTHETIC_REVIEW_KEY", "OWNED_PRIVATE_ERROR", "partial"):
                        self.assertNotIn(private, serialized)
                    self.assertFalse(any(key in calls[0] for key in (
                        "max_tokens", "max_completion_tokens", "max_output_tokens")))
                finally:
                    server.shutdown(); server.server_close(); thread.join(3)


if __name__ == "__main__": unittest.main()
