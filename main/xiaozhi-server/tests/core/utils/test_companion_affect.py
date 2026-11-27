"""Affect lifecycle, durable ordering, scope, cancellation and actual authors."""
from dataclasses import replace
import asyncio
import json
import tempfile
import time
import threading
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock

from core.providers.memory.powermem.store import Store
from core.utils.companion_store import CompanionStore
from core.utils.companion_affect import (
    AffectPolicy, AffectRuntime, select_affect, decayed, render_context, rule_observation,
    validated, prepare_affect_turn, complete_affect_turn, cancel_affect_turn,
    affect_context,
)
import test_natural_response as natural
import test_companion_response as q1


def observed(emotion='joy', intensity=.6, **kw):
    return dict(changed=True, emotion=emotion, intensity=intensity, mood_delta=0,
                reason='合成交流中的喜悦', user_emotion='unknown', **kw)


class AffectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Store(Path(self.temp.name)/'ledger.sqlite3')
        self.policy = AffectPolicy('xiaoyuan')
        self.runtime = AffectRuntime(self.ledger, 'synthetic-person', self.policy, None, start=False)
        self.store = self.runtime.store

    def tearDown(self):
        self.runtime.close();self.temp.cleanup()

    def reserve(self, turn, **kw):
        return self.store.reserve('synthetic-person', 'xiaoyuan', self.policy.mode, self.policy.fingerprint,
                                  'synthetic-call', turn, kw.get('ttl',30), kw.get('capacity',32))

    def enqueue(self, ticket, text='我也替您高兴。'):
        self.assertTrue(self.store.enqueue(ticket, {'assistant':text,'user':'今天有好消息。','history':[],'delivery_status':'generated'}))

    def test_final_feedback_is_durable_and_recovered_without_new_fact(self):
        self.enqueue(self.reserve('one'));self.assertTrue(self.runtime.step())
        row=self.runtime._row();self.assertEqual(row['state']['emotion'],'joy')
        recovered=AffectRuntime(self.ledger,'synthetic-person',self.policy,None,start=False)
        self.assertEqual(recovered._row()['revision'],1);recovered.close()
        self.assertEqual(self.ledger.current('synthetic-person'),[])
        with self.ledger.db() as db:
            event=dict(db.execute('SELECT * FROM companion_affect_events').fetchone())
        self.assertEqual(event['status'],'applied');self.assertEqual(event['payload'],'{}')

    def test_source_sequence_is_reserved_before_generation_and_late_old_reply_loses(self):
        older=self.reserve('older');newer=self.reserve('newer')
        self.enqueue(newer);self.runtime.step()
        self.enqueue(older, '我有点担心。');self.runtime.step()
        self.assertEqual(self.runtime._row()['last_seq'],newer['seq'])
        self.assertEqual(self.runtime._row()['state']['emotion'],'joy')

    def test_duplicate_completion_and_bounded_queue(self):
        ticket=self.reserve('one',capacity=1);self.assertIsNone(self.reserve('two',capacity=1))
        self.enqueue(ticket);self.assertFalse(self.store.enqueue(ticket,{'assistant':'重复'}))
        self.runtime.step();self.assertFalse(self.runtime.step())

    def test_cancellation_prevents_a_claimed_job_from_applying(self):
        ticket=self.reserve('one');self.enqueue(ticket)
        job=self.store.claim('synthetic-person','xiaoyuan','feedback',self.policy.fingerprint)
        self.store.cancel(ticket['id'])
        self.assertFalse(self.store.apply(job,dict(emotion='joy',intensity=1,mood=0,reason='',observed_at=time.time()),time.time()+100))
        self.assertIsNone(self.runtime._row())

    def test_epoch_delete_clears_state_payload_and_fences_old_job(self):
        self.enqueue(self.reserve('one'));self.runtime.step()
        ticket=self.reserve('two');self.enqueue(ticket)
        job=self.store.claim('synthetic-person','xiaoyuan','feedback',self.policy.fingerprint)
        self.ledger.forget('synthetic-person',keys=['name'])
        self.assertIsNone(self.runtime._row())
        self.assertFalse(self.store.apply(job,dict(observed_at=time.time()),time.time()+100))
        with self.ledger.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM companion_affect_events').fetchone()[0],0)

    def test_revision_fence(self):
        self.enqueue(self.reserve('one'));self.runtime.step()
        ticket=self.reserve('two');self.enqueue(ticket)
        job=self.store.claim('synthetic-person','xiaoyuan','feedback',self.policy.fingerprint)
        with self.ledger.db() as db:db.execute('UPDATE companion_affect SET revision=revision+1')
        self.assertFalse(self.store.apply(job,dict(observed_at=time.time()),time.time()+100))

    def test_expired_queued_work_and_payload_are_withdrawn(self):
        ticket=self.reserve('one');self.enqueue(ticket)
        with self.ledger.db() as db:db.execute('UPDATE companion_affect_events SET deadline=0')
        self.assertFalse(self.runtime.step())
        with self.ledger.db() as db:row=db.execute('SELECT status,payload FROM companion_affect_events').fetchone()
        self.assertEqual(tuple(row),('expired','{}'))

    def test_queued_event_is_resumable_after_runtime_restart(self):
        self.enqueue(self.reserve('one'));self.runtime.close()
        recovered=AffectRuntime(self.ledger,'synthetic-person',self.policy,None,start=False)
        self.assertTrue(recovered.step());self.assertEqual(recovered._row()['state']['emotion'],'joy');recovered.close()

    def test_interaction_separates_user_emotion_slow_mood_and_role(self):
        p=replace(self.policy,mode='interaction')
        answer=observed('concern');answer.update(mood_delta=-.1,user_emotion='sadness')
        model=NS(response_structured=lambda *a,**kw:json.dumps(answer))
        runtime=AffectRuntime(self.ledger,'synthetic-person',p,model,start=False)
        ticket=runtime.store.reserve('synthetic-person','xiaoyuan',p.mode,p.fingerprint,'call','one',30,32)
        runtime.store.enqueue(ticket,{'assistant':'这事我有些担心。','user':'我挺失落。','history':[]})
        runtime.step();state=runtime._row()['state']
        self.assertEqual(state['emotion'],'concern');self.assertEqual(state['user_emotion'],'sadness')
        self.assertEqual(state['mood'],-.1);self.assertIsNone(self.runtime._row());runtime.close()

    def test_observer_failure_does_not_write_state(self):
        def fail(*a,**kw):raise TimeoutError('synthetic')
        self.runtime.model=NS(response_structured=fail)
        self.enqueue(self.reserve('one'),'说不准是什么心情。');self.runtime.step()
        self.assertIsNone(self.runtime._row())
        with self.ledger.db() as db:row=db.execute('SELECT status,error_class FROM companion_affect_events').fetchone()
        self.assertEqual(tuple(row),('failed','TimeoutError'))

    def test_scopes_and_companions_do_not_share_state(self):
        self.enqueue(self.reserve('one'));self.runtime.step()
        self.assertIsNone(self.store.read('other-person','xiaoyuan','feedback',self.policy.fingerprint))
        self.assertIsNone(self.store.read('synthetic-person','other-role','feedback',self.policy.fingerprint))

    def test_expired_state_is_physically_removed(self):
        self.enqueue(self.reserve('one'));self.runtime.step()
        with self.ledger.db() as db:db.execute('UPDATE companion_affect SET expires_at=0')
        self.assertIsNone(self.runtime._row())
        with self.ledger.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM companion_affect').fetchone()[0],0)


class ObservationTests(unittest.TestCase):
    def test_rule_does_not_treat_user_sadness_or_negated_joy_as_role_joy(self):
        for text in ('您别难过。','我高兴不起来。','我不高兴。','他问我高兴吗？'):
            self.assertIsNone(rule_observation(text),text)
        self.assertEqual(rule_observation('真替您高兴。')['emotion'],'joy')
        self.assertEqual(rule_observation('我也挺高兴的。')['emotion'],'joy')
        self.assertEqual(rule_observation('我也特别开心。')['emotion'],'joy')

    def test_aux_schema_rejects_protocol_and_nonfinite_numbers(self):
        for change in ({'intensity':True},{'intensity':float('nan')},{'mood_delta':1},{'emotion':'custom'}):
            value=observed();value.update(change)
            with self.assertRaises(ValueError):validated(value)

    def test_decay_is_idempotent_for_reads_and_recovers_to_neutral(self):
        p=AffectPolicy('xiaoyuan');row=dict(state=dict(emotion='joy',intensity=.8,mood=.4,reason='好消息',observed_at=100),
                                         expires_at=100+86400,source_turn='turn')
        self.assertEqual(decayed(row,p,100+5400)['intensity'],.4)
        self.assertEqual(decayed(row,p,5500),decayed(row,p,5500))
        self.assertIsNone(render_context(row,p,100+23*3600))
        self.assertIsNone(decayed(row,p,100+86400))


class AffectWorkflowTests(unittest.IsolatedAsyncioTestCase):
    add=natural.NaturalTests.add
    chat=natural.NaturalTests.chat
    texts=staticmethod(natural.NaturalTests.texts)

    def setUp(self):
        natural.NaturalTests.setUp(self)
        original=AffectRuntime
        self.patch=patch('core.utils.companion_affect.AffectRuntime',side_effect=lambda *a,**kw:original(*a,start=False,**kw))
        self.patch.start()

    def tearDown(self):
        for runtime in getattr(self.memory.engine,'_companion_affect_runtimes',{}).values():runtime.close()
        self.patch.stop();natural.NaturalTests.tearDown(self)

    def connection(self,direct=False):
        c=q1.RoleWorkflowTests.connection(self,lambda m:iter(['我有点难过。']))
        c.config['dialogue_quality']['strategy']='role_direct' if direct else 'legacy'
        c.config['companion_affect']={'enabled':True,'companion_id':'xiaoyuan','allowed_subject_scopes':[self.memory.scope]}
        return c

    async def test_a_observes_final_flash_not_character_draft_and_injects_next_request(self):
        c=self.connection()
        calls=[]
        def compose(system,payload,schema,**kw):
            value=json.loads(payload);calls.append(value)
            return json.dumps(dict(valid=True,reply='真替您高兴。',reason='',news_numbers=[]))
        c.intent.llm.response_structured=compose
        await self.chat(c,'今天有个好消息。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_affect_runtimes.values()))
        runtime.step();self.assertEqual(runtime._row()['state']['emotion'],'joy')
        await self.chat(c,'我还想再聊两句。',{'route':'chat','input_origin':'asr'})
        self.assertIn('高兴',str(calls[-1]['current_turn_data']['companion_affect']))
        self.assertNotIn('companion_affect',calls[0]['current_turn_data'])

    async def test_b_receives_same_state_without_changing_author(self):
        a=self.connection()
        a.llm.response=lambda *a,**kw:iter(['真替您高兴。'])
        await self.chat(a,'有好消息。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_affect_runtimes.values()));runtime.step()
        b=self.connection(direct=True)
        b.device_id='synthetic-other-terminal'
        b.config['dialogue_quality']['allowed_device_ids'].append(b.device_id)
        await self.chat(b,'那咱接着聊。',{'route':'chat','input_origin':'asr'})
        self.assertIn('companion_affect',str(b.role_calls[-1]))
        self.assertFalse(b.review_calls)

    async def test_disable_stops_observation_and_injection(self):
        c=self.connection();c.config['companion_affect']['enabled']=False
        await self.chat(c,'您好。',{'route':'chat','input_origin':'asr'})
        self.assertFalse(hasattr(self.memory.engine,'_companion_affect_runtimes'))
        self.assertNotIn('companion_affect',str(c.review_calls))

    async def test_disabled_and_absent_configuration_have_equal_final_inputs(self):
        rows=[]
        for absent in (False,True):
            c=self.connection();c.config['companion_affect']['enabled']=False
            if absent:c.config.pop('companion_affect')
            await self.chat(c,'您好。',{'route':'chat','input_origin':'asr'})
            value=c.review_calls[0];value['current_turn_data'].pop('local_time')
            rows.append(value)
        self.assertEqual(rows[0],rows[1])

    async def test_disable_cancels_already_queued_observations(self):
        c=self.connection()
        await self.chat(c,'有个好消息。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_affect_runtimes.values()))
        c.config['companion_affect']['enabled']=False
        await self.chat(c,'接着聊。',{'route':'chat','input_origin':'asr'})
        self.assertFalse(runtime.step());self.assertIsNone(runtime._row())

    async def test_subject_change_does_not_reuse_a_previous_turn_binding(self):
        c=self.connection()
        c.llm.response=lambda *a,**kw:iter(['真替您高兴。'])
        await self.chat(c,'有个好消息。',{'route':'chat','input_origin':'asr'})
        bundle=c._affect_turns[c.sentence_id];runtime=bundle[0];runtime.step()
        self.assertIsNotNone(affect_context(c,bundle))
        before=self.memory.scope
        try:
            self.memory.scope='other-person';c.config['companion_affect']['allowed_subject_scopes'].append('other-person')
            self.assertIsNone(affect_context(c,bundle))
        finally:self.memory.scope=before

    async def test_slow_observer_does_not_hold_the_next_generation(self):
        c=self.connection()
        c.llm.response=lambda *a,**kw:iter(['这话听起来挺有分量的。'])
        await self.chat(c,'随便聊两句。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_affect_runtimes.values()))
        started,release=threading.Event(),threading.Event()
        def slow(*a,**kw):
            started.set();release.wait(2)
            return json.dumps(observed())
        runtime.model=NS(response_structured=slow)
        task=asyncio.create_task(asyncio.to_thread(runtime.step))
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        try:
            await asyncio.wait_for(self.chat(c,'我接着说。',{'route':'chat','input_origin':'asr'}),.7)
        finally:
            release.set();await task

    async def test_actual_abort_cancels_queued_observation(self):
        from core.handle.abortHandle import handleAbortMessage
        c=self.connection();c.websocket=NS(send=AsyncMock());c.clear_queues=lambda:None;c.clearSpeakStatus=lambda:None
        await self.chat(c,'有个好消息。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_affect_runtimes.values()))
        await handleAbortMessage(c)
        self.assertFalse(runtime.step());self.assertIsNone(runtime._row())

    def test_identity_and_explicit_scope_required_and_modes_are_alternatives(self):
        c=self.connection();self.assertEqual(select_affect(c)[2].mode,'feedback')
        c.config['companion_affect']['mode']='interaction';self.assertEqual(select_affect(c)[2].mode,'interaction')
        c.config['companion_affect']['mode']='off';self.assertIsNone(select_affect(c))
        c.config['companion_affect']['mode']='feedback';c.config['companion_affect']['companion_id']=''
        self.assertIsNone(select_affect(c))
        c.config['companion_affect']['companion_id']='xiaoyuan';c.config['companion_affect']['allowed_subject_scopes']=[]
        self.assertIsNone(select_affect(c))


class AffectConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_keeps_explicit_affect_enable_and_disable(self):
        from config import config_loader
        for block in ({'enabled':True,'companion_id':'xiaoyuan','allowed_subject_scopes':['synthetic']},{'enabled':False}):
            with patch.object(config_loader,'init_service'), patch.object(config_loader,'get_server_config',
                    new=AsyncMock(return_value={'server':{'auth':{'enabled':True}}})):
                result=await config_loader.get_config_from_api_async({'manager-api':{'url':'https://example.invalid'},'companion_affect':block})
            self.assertEqual(result['companion_affect'],block)
