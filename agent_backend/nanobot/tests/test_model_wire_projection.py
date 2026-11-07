"""Capture actual SDK HTTP JSON; never contact a provider or read credentials.

The worker attaches host-only attachment_id at the message level. SDK 0.4
already projects those message dictionaries through its wire-key allowlist;
these tests lock down that public-provider behavior without duplicating it.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

import httpx
from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_context import ModelManagedProvider
from nanobot.providers.model_api import ModelAPICapabilities


_IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aTfoAAAAASUVORK5CYII='
_PRIOR_ATTACHMENT = 'a' * 64
_CURRENT_ATTACHMENT = 'b' * 64
_HOST_FIELDS = {'attachment_id', '_meta', 'host_private_marker', 'checkpoint_scope'}


def worker_shaped_messages():
    return [
        {'role': 'system', 'content': 'Trusted fixture policy', 'checkpoint_scope': 'HOST-NOT-WIRE'},
        {'role': 'user', 'attachment_id': _PRIOR_ATTACHMENT,
         'content': f'[Authorized prior attachment: {_PRIOR_ATTACHMENT}; inspect on demand, do not attach to a new user message.]',
         'host_private_marker': 'PRIOR-HOST-NOT-WIRE'},
        {'role': 'assistant', 'content': 'Earlier image was inspected.'},
        {'role': 'user', 'attachment_id': _CURRENT_ATTACHMENT,
         '_meta': {'source': 'HOST-NOT-WIRE'}, 'host_private_marker': 'CURRENT-HOST-NOT-WIRE',
         'content': [{'type': 'text', 'text': 'Only this new picture is attached.', '_meta': {'host': True}},
                     {'type': 'image_url', 'image_url': {'url': _IMAGE, 'detail': 'high'}, '_meta': {'source': 'host'}}]},
    ]


class ActualSDKWireProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, api, *, stream=False):
        requests = []
        response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 0, 'status': 'completed',
                    'model': 'fixture-model', 'output': [{'id': 'msg_fixture', 'type': 'message',
                        'status': 'completed', 'role': 'assistant', 'content': [
                            {'type': 'output_text', 'text': 'fixture answer', 'annotations': []}]}],
                    'usage': {'input_tokens': 8, 'output_tokens': 2, 'total_tokens': 10}}

        async def transport(request):
            body = json.loads(request.content)
            requests.append({'path': request.url.path, 'body': body})
            self.assertEqual(request.url.host, 'fixture.invalid')
            if api == 'responses':
                if stream:
                    events = [{'type': 'response.output_text.delta', 'delta': 'fixture answer',
                               'item_id': 'msg_fixture', 'output_index': 0, 'content_index': 0, 'sequence_number': 0},
                              {'type': 'response.completed', 'response': response, 'sequence_number': 1}]
                    payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=payload)
                return httpx.Response(200, json=response)
            if stream:
                chunks = [{'id': 'chat_fixture', 'object': 'chat.completion.chunk', 'created': 0,
                           'model': 'fixture-model', 'choices': [{'index': 0,
                               'delta': {'role': 'assistant', 'content': 'fixture answer'}, 'finish_reason': None}]},
                          {'id': 'chat_fixture', 'object': 'chat.completion.chunk', 'created': 0,
                           'model': 'fixture-model', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
                payload = ''.join(f'data: {json.dumps(chunk)}\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=payload)
            return httpx.Response(200, json={'id': 'chat_fixture', 'object': 'chat.completion', 'created': 0,
                'model': 'fixture-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'fixture answer'}}],
                'usage': {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}})

        provider = ModelManagedProvider(api_key='synthetic-fixture-key', api_base='https://fixture.invalid/v1',
            default_model='fixture-model', model_api=ModelAPICapabilities(
                supported_apis=(api,), preferred_api=api))
        provider._client = AsyncOpenAI(api_key='synthetic-fixture-key', base_url='https://fixture.invalid/v1',
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        messages = worker_shaped_messages()
        before = deepcopy(messages)
        try:
            method = provider.chat_stream if stream else provider.chat
            result = await method(messages=messages, max_tokens=0)
            self.assertEqual(result.finish_reason, 'stop', result.content)
            self.assertEqual(result.content, 'fixture answer')
            self.assertEqual(messages, before, 'Wire projection must not erase host replay metadata in place.')
            self.assertEqual(len(requests), 1, 'No hidden fallback or retry may remove the image.')
            return requests[0]
        finally:
            await provider.aclose()

    def assert_host_metadata_absent(self, value):
        if isinstance(value, dict):
            self.assertFalse(_HOST_FIELDS.intersection(value))
            for item in value.values():
                self.assert_host_metadata_absent(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_host_metadata_absent(item)

    async def test_chat_wire_drops_host_fields_preserves_current_image_and_prior_text_reference(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                captured = await self.request('chat_completions', stream=stream)
                self.assertEqual(captured['path'], '/v1/chat/completions')
                body = captured['body']
                self.assert_host_metadata_absent(body['messages'])
                self.assertIn(_PRIOR_ATTACHMENT, json.dumps(body['messages']))
                images = [block for message in body['messages'] if isinstance(message.get('content'), list)
                          for block in message['content'] if block.get('type') == 'image_url']
                self.assertEqual(images, [{'type': 'image_url', 'image_url': {'url': _IMAGE, 'detail': 'high'}}])
                self.assertEqual(body['messages'][-1]['content'][0]['text'], 'Only this new picture is attached.')
                self.assertNotIn('max_tokens', body)

    async def test_responses_wire_drops_host_fields_preserves_current_image_and_prior_text_reference(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                captured = await self.request('responses', stream=stream)
                self.assertEqual(captured['path'], '/v1/responses')
                body = captured['body']
                self.assert_host_metadata_absent(body['input'])
                self.assertIn(_PRIOR_ATTACHMENT, json.dumps(body['input']))
                images = [block for message in body['input'] if isinstance(message.get('content'), list)
                          for block in message['content'] if block.get('type') == 'input_image']
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0]['image_url'], _IMAGE)
                self.assertIn('Only this new picture is attached.', json.dumps(body['input']))
                self.assertNotIn('max_output_tokens', body)


if __name__ == '__main__':
    unittest.main()
