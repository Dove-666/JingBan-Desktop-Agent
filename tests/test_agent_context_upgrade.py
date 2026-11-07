"""Recoverable context, scope boundaries and deletion using synthetic records."""
from dataclasses import replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from agent_backend.nanobot.context_store import ContextStoreError, ObservationStore, namespace_for
from agent_backend.nanobot.session_history import bounded_history, text_record, MAX_HISTORY_BYTES
from agent_backend.nanobot.working_context import compact_messages, project_tool_bodies, request_bytes
from desktop_pet.agents import AgentSettings
from desktop_pet.agents.checkpoints import CheckpointStore, scope_fingerprint, legacy_scope_fingerprint


class RecoverableContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-context-test-")
        self.root = Path(self.temporary.name)
        self.cid, self.scope = str(uuid4()), "a"*64
        self.store = ObservationStore(self.root/'private', namespace_for(self.cid,self.scope), secrets=('SYNTHETIC-SECRET',))

    def tearDown(self):
        self.temporary.cleanup()

    def test_constructing_private_archive_has_no_io(self):
        self.assertFalse(self.store.root.exists())

    def test_unicode_pagination_is_complete_and_has_integrity(self):
        text = '开始😀\n' + '可回读数据'*10000 + '\nFINAL-MARKER'
        ref = self.store.put_text(text)
        parts, offset = [], 0
        while True:
            page = json.loads(self.store.read(ref['reference'],offset=offset,limit=413))
            parts.append(page['text'])
            if not page['truncated']:
                break
            self.assertGreater(page['next_offset'],offset)
            offset = page['next_offset']
        self.assertEqual(''.join(parts),text)
        self.assertEqual(ref['sha256'],hashlib.sha256(text.encode()).hexdigest())

    def test_cross_namespace_reference_is_not_readable(self):
        ref = self.store.put_text('private observation')['reference']
        other = ObservationStore(self.store.root,namespace_for(str(uuid4()),self.scope))
        with self.assertRaisesRegex(ContextStoreError,'UNAVAILABLE'):
            other.read(ref)

    def test_namespace_and_read_ranges_do_not_accept_paths_or_negative_offsets(self):
        for namespace in ('../data',str(uuid4())+':bad','00000000-0000-0000-0000-000000000000:'+self.scope):
            with self.assertRaises(ContextStoreError):
                ObservationStore(self.store.root,namespace)
        for reference,offset,limit in (('../private',0,10),('obs_'+'a'*64,-1,10),('obs_'+'a'*64,0,True),('obs_'+'a'*64,0,24001)):
            with self.assertRaises(ContextStoreError):
                self.store.read(reference,offset,limit)

    def test_reference_hash_detects_corruption(self):
        ref = self.store.put_text('before')['reference']
        with closing(sqlite3.connect(self.store.path)) as db:
            with db:
                db.execute('UPDATE observations SET text=? WHERE reference=?',('changed',ref))
        with self.assertRaisesRegex(ContextStoreError,'CORRUPTED'):
            self.store.read(ref)

    def test_archive_strips_images_secrets_and_hidden_reasoning(self):
        ref = self.store.put_records([{'role':'assistant','content':'visible SYNTHETIC-SECRET',
            'reasoning_content':'HIDDEN-REASONING-MUST-NOT-PERSIST','provider_state':{'private':'HIDDEN-STATE'},
            'tool_calls':[{'id':'c','type':'function','function':{'name':'read_file','arguments':'{}','thought':'PRIVATE-THOUGHT'}}]},
            {'role':'user','content':[{'type':'text','text':'user visible'},{'type':'image_url','image_url':{'url':'data:image/png;base64,UElYRUxT'}}]}])
        index=json.loads(json.loads(self.store.read(ref['reference']))['text'])
        archive=json.loads(self.store.read(index['chunks'][0]['reference']))['text']
        self.assertIn('[redacted]',archive)
        for private in ('SYNTHETIC-SECRET','HIDDEN-REASONING','HIDDEN-STATE','PRIVATE-THOUGHT','UElYRUxT'):
            self.assertNotIn(private,archive)
            self.assertNotIn(private.encode(),self.store.path.read_bytes())

    def test_clear_revokes_late_reads_and_writes_but_new_generation_can_start(self):
        ref=self.store.put_text('to erase')['reference']
        ObservationStore.clear_conversation(self.store.root,self.cid)
        with self.assertRaisesRegex(ContextStoreError,'REVOKED'):
            self.store.read(ref)
        with self.assertRaisesRegex(ContextStoreError,'REVOKED'):
            self.store.put_text('late worker write')
        later=ObservationStore(self.store.root,namespace_for(self.cid,'b'*64))
        newref=later.put_text('new user task')['reference']
        self.assertEqual(json.loads(later.read(newref))['text'],'new user task')
        with self.assertRaisesRegex(ContextStoreError,'UNAVAILABLE'):
            later.read(ref)

    def test_old_turns_become_facts_with_exact_recallable_sources(self):
        records=[]
        for number in range(20):
            records += [{'role':'user','content':f'turn-{number}: source path owned-{number}.csv'},
                        {'role':'assistant','content':f'Observed fact FACT-{number}'}]
        hot=bounded_history(records,store=self.store)
        self.assertEqual(sum(r['role']=='user' for r in hot),12)
        self.assertIn('FACT-0',json.dumps(hot))
        digest=next(r['content'] for r in hot if 'Host archived task facts;' in r.get('content',''))
        data=json.loads(digest.split('\n',1)[1])
        index=json.loads(json.loads(self.store.read(data['sources'][0]))['text'])
        exact=''.join(json.loads(self.store.read(chunk['reference']))['text'] for chunk in index['chunks'])
        self.assertIn('FACT-0',exact)
        self.assertIn('owned-0.csv',exact)
        self.assertLessEqual(request_bytes(hot),MAX_HISTORY_BYTES)

    def test_large_current_turn_preserves_pending_call_and_uses_body_references(self):
        records=[{'role':'user','content':'original current goal'}]
        for number in range(30):
            records += [{'role':'assistant','content':'','tool_calls':[{'id':f'c{number}','type':'function','function':{'name':'read_file','arguments':'{}'}}]},
                        {'role':'tool','name':'read_file','tool_call_id':f'c{number}','content':f'BEGIN-{number}'+('x'*24000)+f'END-{number}'}]
        records.append({'role':'assistant','content':'','tool_calls':[{'id':'pending','type':'function','function':{'name':'write_file','arguments':'{"path":"out.txt","content":"expected"}'}}]})
        hot=bounded_history(records,store=self.store)
        self.assertLessEqual(request_bytes(hot),MAX_HISTORY_BYTES)
        self.assertIn('pending',json.dumps(hot))
        self.assertIn('original current goal',json.dumps(hot))
        projected=json.loads(next(r['content'] for r in hot if r.get('role')=='tool'))
        recalled=json.loads(self.store.read(projected['observation']['reference']))
        self.assertTrue(recalled['text'].startswith('BEGIN-0'))

    def test_model_projection_preserves_current_image_and_does_not_mutate_history(self):
        records=[{'role':'system','content':'HOST POLICY'}]
        for number in range(12):
            records += [{'role':'user','content':f'old goal {number}'}, {'role':'assistant','content':'old fact '+('x'*5000)}]
        current={'role':'user','content':[{'type':'text','text':'CURRENT USER GOAL'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,CURRENT-IMAGE'}}]}
        records += [current,{'role':'assistant','content':'','tool_calls':[{'id':'last','type':'function','function':{'name':'read_file','arguments':'{}'}}]},
                    {'role':'tool','name':'read_file','tool_call_id':'last','content':'actual latest observation'}]
        original=json.dumps(records)
        projected=compact_messages(records,self.store,target_bytes=8000)
        self.assertLess(request_bytes(projected),request_bytes(records))
        self.assertEqual(json.dumps(records),original)
        self.assertIn(current,projected)
        self.assertEqual(projected[0],records[0])
        self.assertEqual(projected[-1],records[-1])

    def test_archived_image_blocks_do_not_turn_into_base64_inside_text_digest(self):
        records=[{'role':'user','content':[{'type':'text','text':'old picture'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,OLD_PIXELS_MUST_NOT_REPLAY'}}]},
            {'role':'assistant','content':'old observation '+('x'*15000)},
            {'role':'user','content':'current protected goal'}]
        projected=compact_messages(records,self.store,target_bytes=3000)
        serialized=json.dumps(projected)
        self.assertNotIn('OLD_PIXELS_MUST_NOT_REPLAY',serialized)
        self.assertIn('current protected goal',serialized)

    def test_pending_arguments_never_replaced_but_settled_large_arguments_are_recallable(self):
        args=json.dumps({'content':'LARGE-WRITE-'+'x'*20000})
        call={'role':'assistant','content':'','tool_calls':[{'id':'write','type':'function','function':{'name':'write_file','arguments':args}}]}
        self.assertEqual(project_tool_bodies([call],self.store,maximum_chars=1000)[0]['tool_calls'][0]['function']['arguments'],args)
        settled=project_tool_bodies([call,{'role':'tool','name':'write_file','tool_call_id':'write','content':'ok'}],self.store,maximum_chars=1000)
        ref=json.loads(settled[0]['tool_calls'][0]['function']['arguments'])['_archived_arguments']['reference']
        self.assertEqual(json.loads(self.store.read(ref,limit=24000))['text'],args)

    def test_attachment_reference_survives_projection_without_pixels(self):
        attachment='b'*64
        record=text_record({'role':'user','attachment_id':attachment,'content':[{'type':'text','text':'above picture'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,UElYRUxT'}}]})
        self.assertEqual(record['attachment_id'],attachment)
        self.assertIn(attachment,record['content'])
        self.assertNotIn('UElYRUxT',json.dumps(record))

    def test_model_switch_preserves_facts_while_security_changes_isolate(self):
        settings=AgentSettings()
        api={'base_url':'https://example.invalid/v1','model':'model-a'}
        scope=scope_fingerprint(settings,api,secrets=('same-account',))
        self.assertEqual(scope,scope_fingerprint(replace(settings,gui_model='new-vision',gui_provider='ui-tars-1.0'),{**api,'model':'model-b'},secrets=('same-account',)))
        self.assertNotEqual(scope,scope_fingerprint(settings,api,secrets=('another-account',)))
        self.assertNotEqual(scope,scope_fingerprint(replace(settings,computer_enabled=not settings.computer_enabled),api,secrets=('same-account',)))
        self.assertNotEqual(scope,scope_fingerprint(settings,{**api,'base_url':'https://other.invalid/v1'},secrets=('same-account',)))
        self.assertNotEqual(scope,scope_fingerprint(settings,api,secrets=('same-account',),permissions=[{'path':'new-read-only-root'}]))

    def test_exact_legacy_configuration_migrates_but_wrong_legacy_scope_does_not(self):
        settings=AgentSettings();api={'base_url':'https://example.invalid/v1','model':'model-a'}
        legacy=legacy_scope_fingerprint(settings,api,secrets=('synthetic-account',))
        current=scope_fingerprint(settings,api,secrets=('synthetic-account',))
        checkpoints=CheckpointStore(self.root/'checkpoints.sqlite3')
        checkpoints.begin(self.cid,legacy,uuid4().hex,[{'role':'user','content':'old owned fact'}])
        self.assertFalse(checkpoints.load(self.cid,current,legacy_scope_hash='f'*64)['scope_matches'])
        restored=checkpoints.load(self.cid,current,legacy_scope_hash=legacy)
        self.assertTrue(restored['scope_matches'])
        self.assertIn('old owned fact',json.dumps(restored['history']))

    def test_checkpoint_clear_erases_observation_bodies_and_revokes_known_namespace(self):
        checkpoints=CheckpointStore(self.root/'checkpoints.sqlite3')
        task=uuid4().hex
        checkpoints.begin(self.cid,self.scope,task,[{'role':'user','content':'owned task'}])
        observations=checkpoints.observation_store(self.cid,self.scope)
        ref=observations.put_text('private obsolete observation')['reference']
        checkpoints.clear(self.cid)
        self.assertIsNone(checkpoints.load(self.cid,self.scope))
        with self.assertRaisesRegex(ContextStoreError,'REVOKED'):
            observations.read(ref)


if __name__=='__main__':
    unittest.main()
