"""Actual SDK adapter checks with synthetic responses; no provider networking."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from context_store import ObservationStore, namespace_for
from model_context import ModelManagedContext, ModelManagedProvider, context_capacity_error
from working_context import request_bytes
from nanobot.agent.context_governance import ContextGovernanceConfig
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.llm_usage.types import LLMUsage


class GovernedProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='aura-context-provider-')
        self.store=ObservationStore(Path(self.temporary.name),namespace_for(str(uuid4()),'a'*64),secrets=('synthetic-private-key',))
        self.provider=ModelManagedProvider(api_key='synthetic-private-key',api_base='https://example.invalid/v1',default_model='synthetic-model',context_store=self.store)

    async def asyncTearDown(self):
        await self.provider.aclose()
        self.temporary.cleanup()

    def history(self):
        messages=[{'role':'system','content':'TRUSTED HOST POLICY'}]
        for number in range(12):
            messages += [{'role':'user','content':f'earlier goal {number}'},
                         {'role':'assistant','content':f'confirmed earlier observation {number}: '+'x'*6000}]
        messages += [{'role':'user','content':[{'type':'text','text':'CURRENT TASK'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,CURRENT_PIXELS'}}]}]
        return messages

    async def test_capacity_recovery_only_retries_model_with_smaller_recallable_context(self):
        calls=[]
        async def provider_call(*,messages,**kwargs):
            calls.append(deepcopy(messages))
            if len(calls)==1:
                return LLMResponse(content='context_length_exceeded PRIVATE',finish_reason='error',error_code='context_length_exceeded')
            return LLMResponse(content='next plan',tool_calls=[ToolCallRequest(id='next-only',name='read_file',arguments={'path':'owned.txt'})],usage=LLMUsage.reported(input_tokens=100,output_tokens=10))
        original=self.history();before=deepcopy(original)
        result=await self.provider._governed_call(provider_call,original,{})
        self.assertEqual(len(calls),2)
        self.assertLess(request_bytes(calls[1]),request_bytes(calls[0]))
        self.assertEqual(calls[0][-1],calls[1][-1])
        self.assertEqual(original,before)
        self.assertEqual(result.tool_calls[0].id,'next-only')
        self.assertEqual(len(self.provider.context_events),1)
        self.assertIn('obs_',json.dumps(calls[1]))

    async def test_noncapacity_authentication_or_rate_errors_are_not_misclassified_or_compacted(self):
        calls=[]
        async def provider_call(*,messages,**kwargs):
            calls.append(messages)
            return LLMResponse(content='authentication failed PRIVATE',finish_reason='error',error_code='invalid_api_key')
        result=await self.provider._governed_call(provider_call,self.history(),{})
        self.assertEqual(result.error_code,'invalid_api_key')
        self.assertEqual(len(calls),1)
        self.assertFalse(self.provider.context_events)

    async def test_current_goal_too_large_is_not_silently_truncated_and_retry_is_bounded(self):
        calls=[]
        async def provider_call(*,messages,**kwargs):
            calls.append(messages)
            return LLMResponse(content='context_length_exceeded',finish_reason='error',error_code='context_length_exceeded')
        current=[{'role':'system','content':'policy'},{'role':'user','content':'PROTECTED CURRENT GOAL '+('x'*20000)}]
        result=await self.provider._governed_call(provider_call,current,{})
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],current)
        self.assertEqual(result.finish_reason,'error')

    async def test_capacity_exceptions_recover_without_exposing_error_in_archive(self):
        calls=[]
        async def provider_call(*,messages,**kwargs):
            calls.append(messages)
            if len(calls)==1:
                raise RuntimeError('maximum context length PRIVATE-SERVICE-BODY')
            return LLMResponse(content='recovered')
        result=await self.provider._governed_call(provider_call,self.history(),{})
        self.assertEqual(result.content,'recovered')
        self.assertNotIn(b'PRIVATE-SERVICE-BODY',self.store.path.read_bytes())

    async def test_stream_and_plain_entries_both_use_governed_path(self):
        from unittest.mock import patch
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        calls=[]
        async def fake(instance,messages,**kwargs):
            calls.append(messages)
            return LLMResponse(content='visible')
        with patch.object(OpenAICompatProvider,'chat',fake),patch.object(OpenAICompatProvider,'chat_stream',fake):
            await self.provider.chat([{'role':'user','content':'plain'}])
            await self.provider.chat_stream([{'role':'user','content':'stream'}])
        self.assertEqual(len(calls),2)

    async def test_actual_sdk_retry_layer_never_removes_current_user_image(self):
        from unittest.mock import patch
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        calls=[]
        async def transport(instance,messages,**kwargs):
            calls.append(deepcopy(messages))
            return LLMResponse(content='Vision input unsupported',finish_reason='error',error_status_code=400)
        messages=[{'role':'user','content':[{'type':'text','text':'explicit current picture'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,CURRENT_PIXELS'}}]}]
        with patch.object(OpenAICompatProvider,'chat_stream',transport):
            response=await self.provider.chat_stream_with_retry(messages=messages)
        self.assertEqual(response.finish_reason,'error')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],messages)
        self.assertIn('image_url',json.dumps(messages))

    def test_positive_output_budget_is_respected_and_zero_omitted(self):
        messages=[{'role':'user','content':'synthetic'}]
        positive=self.provider._build_kwargs(messages,None,'synthetic-model',4096,0.1,None,None)
        uncapped=self.provider._build_kwargs(messages,None,'synthetic-model',0,0.1,None,None)
        self.assertEqual(positive.get('max_tokens',positive.get('max_completion_tokens')),4096)
        self.assertNotIn('max_tokens',uncapped)
        self.assertNotIn('max_completion_tokens',uncapped)

    def test_response_api_zero_does_not_become_one_output_token(self):
        messages=[{'role':'user','content':'synthetic'}]
        value=self.provider._build_responses_body(messages,None,'synthetic-model',0,0.1,None,None,None)
        self.assertNotIn('max_output_tokens',value)

    def test_governor_spills_large_result_to_the_same_readable_store(self):
        governor=ModelManagedContext(self.store)
        config=ContextGovernanceConfig(provider=self.provider,model='synthetic-model',tools=ToolRegistry(),
            workspace=None,session_key='fixture',max_tool_result_chars=1000,context_window_tokens=0,max_tokens=0)
        result=governor.normalize_tool_result(config,'fixture-call','web_fetch','begin-'+('x'*20000)+'-tail')
        projected=json.loads(result)
        restored=json.loads(self.store.read(projected['context_observation']['reference'],limit=24000))
        self.assertTrue(restored['text'].endswith('-tail'))
        recalled=governor.normalize_tool_result(config,'recall','recall_context',restored['text'])
        self.assertEqual(recalled,restored['text'])

    def test_governor_keeps_native_tool_pairs_and_reduces_large_working_set(self):
        governor=ModelManagedContext(self.store,working_set_bytes=32000)
        config=ContextGovernanceConfig(provider=self.provider,model='synthetic-model',tools=ToolRegistry(),
            workspace=None,session_key='fixture',max_tool_result_chars=1000,context_window_tokens=0,max_tokens=0)
        messages=self.history()
        messages += [{'role':'assistant','content':'','tool_calls':[{'id':'last','type':'function','function':{'name':'read_file','arguments':'{}'}}]},
                     {'role':'tool','name':'read_file','tool_call_id':'last','content':'confirmed latest observation'}]
        result=governor.prepare_for_model(config,messages)
        self.assertLess(request_bytes(result),request_bytes(messages))
        self.assertEqual(result[-2:],messages[-2:])
        self.assertIn(messages[-3],result)

    async def test_actual_unlimited_sdk_continues_after_capacity_rejection_without_repeating_tool_effects(self):
        """Whole real runner, real registry, synthetic effects and transport."""
        from unittest.mock import patch
        from nanobot.agent.runner import AgentRunSpec
        from nanobot.agent.tools.base import Tool
        from nanobot.providers.generation import GenerationSettings
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        from nanobot.utils.llm_runtime import LLMRuntime
        from sdk_iterations import unlimited_iterations_runner
        effects, requests = [], []
        class CounterTool(Tool):
            @property
            def name(self): return 'fixture_effect'
            @property
            def description(self): return 'Synthetic effect; never repeats a prior index.'
            @property
            def parameters(self): return {'type':'object','properties':{'index':{'type':'integer'}},'required':['index']}
            async def execute(self,index):
                if index in effects:
                    raise AssertionError('Synthetic effect replayed')
                effects.append(index)
                return f'Observed confirmed fixture effect {index}: '+('x'*8000)
        async def transport(instance,messages,**kwargs):
            requests.append(request_bytes(messages))
            if request_bytes(messages)>30000:
                return LLMResponse(content='context_length_exceeded',finish_reason='error',error_code='context_length_exceeded')
            if len(effects)<10:
                index=len(effects)+1
                return LLMResponse(content=None,finish_reason='tool_calls',tool_calls=[
                    ToolCallRequest(id=f'effect-{index}',name='fixture_effect',arguments={'index':index})])
            return LLMResponse(content='All ten distinct fixture effects observed once.')
        async def summarize(*args):
            return 'Owned fixture work; use archived observations.'
        registry=ToolRegistry();registry.register(CounterTool())
        runner=unlimited_iterations_runner();runner.context_governor=ModelManagedContext(self.store)
        spec=AgentRunSpec(initial_messages=[{'role':'system','content':'Use fixture effects in order, stop after ten.'},
            {'role':'user','content':'Execute ten synthetic steps and observe each result.'}],
            tools=registry,runtime=LLMRuntime(self.provider,'synthetic-model',GenerationSettings(max_tokens=0),0),
            max_iterations=0,max_tool_result_chars=16000,consolidate_history=summarize)
        with patch.object(OpenAICompatProvider,'chat_stream',transport):
            result=await runner.run(spec)
        self.assertEqual(effects,list(range(1,11)))
        self.assertIn('All ten distinct',result.final_content)
        self.assertFalse(result.error)
        self.assertTrue(any(size>30000 for size in requests))
        self.assertTrue(self.provider.context_events)
        self.assertGreater(len(requests),11)  # Capacity request retried; effects were not.


if __name__=='__main__':
    unittest.main()
