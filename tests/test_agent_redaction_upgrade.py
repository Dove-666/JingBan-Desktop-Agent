"""Actual private-store readback with synthetic quote/slash/Unicode secrets."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from uuid import uuid4

from agent_backend.nanobot.redaction import redact_text, redact_value
from agent_backend.nanobot.context_store import ObservationStore, namespace_for
from agent_backend.nanobot.session_history import text_record
from desktop_pet.agents.checkpoints import CheckpointStore
from desktop_pet.agents.interactions import checked_interaction
from desktop_pet.agents.journal import ExecutionJournal
from desktop_pet.agents.service import _redact
from desktop_pet.chat_controller import ChatController


SECRET = 'synthetic-special-\\-"-雪😀-credential'
ASCII_SECRET = 'synthetic-ascii-\\-"-credential'
MARKER = '[redacted]'
HOST_MARKER = '[已隐藏密钥]'


def wrapped(value, depth):
    for index in range(depth):
        value = json.dumps(value, ensure_ascii=index % 2 == 0)
    return value


def unwrapped(value, depth):
    for _ in range(depth):
        value = json.loads(value)
    return value


def record_fixture():
    return [
        {'role': 'user', 'content': wrapped({'safe': 'visible task text', 'private': SECRET}, 5)},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'owned-read', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'owned.txt',
                'private': wrapped(SECRET, 4)}, ensure_ascii=True)}}]},
        {'role': 'tool', 'tool_call_id': 'owned-read', 'name': 'read_file',
         'content': json.dumps({'safe': 'actual observation text', 'private': SECRET}, ensure_ascii=True)},
        {'role': 'assistant', 'content': 'The observation is retained.'},
    ]


class AgentRedactionUpgradeTests(unittest.TestCase):
    def assert_projected_records(self, records):
        user = next(item for item in records if item['role'] == 'user')
        self.assertEqual(unwrapped(user['content'], 5), {'safe': 'visible task text', 'private': MARKER})
        call = next(item for item in records if item.get('tool_calls'))['tool_calls'][0]
        arguments = json.loads(call['function']['arguments'])
        self.assertEqual(arguments['path'], 'owned.txt')
        self.assertEqual(unwrapped(arguments['private'], 4), MARKER)
        observation = json.loads(next(item for item in records if item['role'] == 'tool')['content'])
        self.assertEqual(observation, {'safe': 'actual observation text', 'private': MARKER})

    def test_text_redacts_raw_and_twelve_mixed_json_layers_without_breaking_readback(self):
        for secret in (SECRET, ASCII_SECRET):
            for depth in range(13):
                with self.subTest(depth=depth, unicode=secret is SECRET):
                    result = redact_text(wrapped(secret, depth), (secret,))
                    self.assertEqual(unwrapped(result, depth), MARKER)
        self.assertEqual(redact_text('keep all punctuation: \\ " 😀', (SECRET,)), 'keep all punctuation: \\ " 😀')

    def test_value_redaction_preserves_original_containers_types_and_argument_json(self):
        value = {'safe': 'visible', 'count': 2, 'flag': True, 'none': None,
                 'items': [SECRET, wrapped({'key': SECRET, 'safe': 'quoted " value'}, 5)], 'tuple': (SECRET,)}
        original = deepcopy(value)
        result = redact_value(value, (SECRET,), HOST_MARKER)
        self.assertEqual(value, original)
        self.assertEqual(result['items'][0], HOST_MARKER)
        self.assertEqual(unwrapped(result['items'][1], 5), {'key': HOST_MARKER, 'safe': 'quoted " value'})
        self.assertEqual(result['tuple'], (HOST_MARKER,))
        self.assertIs(result['flag'], True)
        self.assertIsNone(result['none'])

    def test_journal_roundtrip_redacts_values_before_serialization_and_nested_arguments(self):
        with tempfile.TemporaryDirectory(prefix='agent-redaction-journal-') as owned:
            journal = ExecutionJournal(Path(owned) / 'events.sqlite3')
            data = {'content': SECRET, 'arguments': wrapped({'secret': SECRET, 'safe': 'readable'}, 6)}
            journal.append('owned-cid', 'owned-task', 'message', data, secrets=(SECRET,))
            result = journal.read('owned-cid')[0]['data']
            self.assertEqual(result['content'], HOST_MARKER)
            self.assertEqual(unwrapped(result['arguments'], 6), {'secret': HOST_MARKER, 'safe': 'readable'})
            self.assertEqual(data['content'], SECRET)

    def test_session_projection_retains_tool_pairs_and_json_structure(self):
        records = record_fixture()
        original = deepcopy(records)
        safe = [text_record(item, (SECRET,)) for item in records]
        self.assert_projected_records(safe)
        self.assertEqual(records, original)

    def test_observation_store_body_metadata_and_record_archive_are_readable_and_redacted(self):
        with tempfile.TemporaryDirectory(prefix='agent-redaction-context-') as owned:
            store = ObservationStore(owned, namespace_for(str(uuid4()), 'a' * 64), secrets=(SECRET,))
            reference = store.put_text(wrapped({'secret': SECRET, 'safe': 'body'}, 6),
                metadata={'private': wrapped(SECRET, 4), 'safe': 'source'})
            page = json.loads(store.read(reference['reference']))
            self.assertEqual(unwrapped(page['text'], 6), {'secret': MARKER, 'safe': 'body'})
            self.assertEqual(unwrapped(page['metadata']['source']['private'], 4), MARKER)
            self.assertEqual(page['metadata']['source']['safe'], 'source')
            index = json.loads(json.loads(store.read(store.put_records(record_fixture())['reference']))['text'])
            restored = []
            for chunk in index['chunks']:
                text = json.loads(store.read(chunk['reference']))['text']
                restored.extend(json.loads(line) for line in text.splitlines())
            self.assert_projected_records(restored)
            image = store.put_text('body data:image/png;base64,aGVsbG8= preserved')
            self.assertNotIn('base64', json.loads(store.read(image['reference']))['text'])

    def test_checkpoint_begin_save_load_do_not_restore_any_original_secret(self):
        with tempfile.TemporaryDirectory(prefix='agent-redaction-checkpoint-') as owned:
            store = CheckpointStore(Path(owned) / 'checkpoints.sqlite3')
            cid, scope, task = str(uuid4()), 'a' * 64, uuid4().hex
            records = record_fixture()
            store.begin(cid, scope, task, records, secrets=(SECRET,))
            store.save(cid, scope, task, records, status='completed', secrets=(SECRET,))
            self.assert_projected_records(store.load(cid, scope, secrets=(SECRET,))['history'])

    def test_service_result_and_public_question_options_redact_encoded_credentials(self):
        document = wrapped({'secret': SECRET, 'safe': 'visible result'}, 6)
        self.assertEqual(unwrapped(_redact(document, (SECRET,)), 6), {'secret': HOST_MARKER, 'safe': 'visible result'})
        question = checked_interaction({'interaction_id': 'interaction-' + 'a' * 32, 'kind': 'question',
            'question': wrapped(SECRET, 2), 'options': [wrapped(SECRET, 1)]}, secrets=(SECRET,))
        self.assertEqual(unwrapped(question['question'], 2), HOST_MARKER)
        self.assertEqual(unwrapped(question['options'][0], 1), HOST_MARKER)

    def test_chat_controller_known_key_projection_preserves_image_copy_boundary(self):
        holder = SimpleNamespace(_settings_manager=SimpleNamespace(get_known_api_keys=lambda: [SECRET]))
        encoded = wrapped({'secret': SECRET, 'safe': 'chat text'}, 6)
        result = ChatController._redact_known_secrets(holder, encoded)
        self.assertEqual(unwrapped(result, 6), {'secret': HOST_MARKER, 'safe': 'chat text'})
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}
        projected = ChatController._redact_reference(holder, image)
        self.assertEqual(projected, image)
        self.assertIsNot(projected, image)


if __name__ == '__main__':
    unittest.main()
