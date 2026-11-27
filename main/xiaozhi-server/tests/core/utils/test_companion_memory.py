"""Sourced episodes, correction, recall, delivery and actual A/B integration."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch,AsyncMock

from core.providers.memory.powermem.store import Store
from core.utils.companion_store import EpisodeStore
from core.utils.companion_memory import (EpisodePolicy,EpisodeRuntime,validate_episode,read_episode_context,
    select_episodes,episode_context,prepare_episode_turn,mark_episode_delivery,close_episode_turns)
import test_natural_response as natural
import test_companion_response as q1


def result(text='我担心兰花养不好。',**kw):
    value=dict(keep=True,topic='兰花养护',summary='用户担心兰花养不好。',user_quote=text,
               role_reaction='我有些关心这份担忧。',interpretation='可能想一起琢磨养护。',unfinished=True,supersedes=[],resolves=[])
    value.update(kw);return value


class EpisodeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=Store(Path(self.temp.name)/'ledger.sqlite3')
        self.store=EpisodeStore(self.ledger);self.p=EpisodePolicy('xiaoyuan')
    def tearDown(self):self.temp.cleanup()
    def reserve(self,turn='one',text='我担心兰花养不好。',session='call',**kw):
        return self.store.reserve_episode('person','xiaoyuan',self.p.fingerprint,session,turn,text,**kw)
    def save(self,turn='one',text='我担心兰花养不好。',value=None):
        ticket=self.reserve(turn,text);self.store.finish_episode(ticket,'我听着呢。')
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        self.assertTrue(self.store.apply_episode(job,value or result(text),[],90))
        return self.store.episode_records('person','xiaoyuan')[1][0]
    @staticmethod
    def refs(rows):return [dict(id=r['id'],revision=r['revision'],source_revision=r['semantic_revision']) for r in rows]

    def test_q3_independent_of_affect_tables_and_factual_index(self):
        self.save()
        with self.ledger.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='companion_affect'").fetchone())
        self.assertEqual(self.ledger.current('person'),[])

    def test_cross_call_quote_refs_and_shared_person(self):
        row=self.save();context,token=read_episode_context(self.store,'person','xiaoyuan','上次那件事',True,session_id='other-call')
        self.assertEqual(context['records'][0]['user_quote']['text'],'我担心兰花养不好。')
        self.assertTrue(context['records'][0]['user_quote']['source_ref'].endswith(':user'))
        self.assertEqual(context['records'][0]['role_reply']['delivery_status'],'generated')
        self.assertTrue(self.store.episodes_current('person','xiaoyuan',token))
        self.assertFalse(read_episode_context(self.store,'other','xiaoyuan','兰花')[0]['records'])
        self.assertFalse(read_episode_context(self.store,'person','other-role','兰花')[0]['records'])

    def test_related_and_new_topic_and_budget(self):
        self.save()
        self.assertTrue(read_episode_context(self.store,'person','xiaoyuan','兰花')[0]['records'])
        self.assertFalse(read_episode_context(self.store,'person','xiaoyuan','今天想聊水墨画')[0]['records'])
        self.assertFalse(read_episode_context(self.store,'person','xiaoyuan','兰花',budget=200)[0]['records'])

    def test_source_validation_preserves_negation_and_rejects_fabricated_quote(self):
        ticket=self.reserve(text='我只是担心兰花养不好，并不是死了。');source=self.store.episode_source(ticket)
        self.assertEqual(validate_episode(result(source['user_text']),source,[])['user_quote'],source['user_text'])
        with self.assertRaises(ValueError):validate_episode(result('我家兰花死了。'),source,[])
        with self.assertRaises(ValueError):validate_episode(result(source['user_text'],supersedes=['external-id']),source,[])

    def test_explicit_correction_supersedes_only_known_revision(self):
        old=self.save();text='你记错了，我担心的是薄荷，不是兰花。'
        ticket=self.reserve('two',text);self.store.finish_episode(ticket,'是我弄混了。')
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        value=result(text,topic='薄荷养护',summary='用户纠正担心的是薄荷。',supersedes=[old['id']])
        validate_episode(value,self.store.episode_source(job),[old])
        self.assertTrue(self.store.apply_episode(job,value,self.refs([old]),90))
        active=self.store.episode_records('person','xiaoyuan')[1]
        self.assertEqual(len(active),1);self.assertIn('薄荷',active[0]['payload']['summary'])
        all_rows=self.store.episode_records('person','xiaoyuan',include_withdrawn=True)[1]
        self.assertIn('superseded',[r['status'] for r in all_rows])

    def test_resolve_retains_history_but_closes_pending_point(self):
        old=self.save();text='兰花那事已经解决了，现在不用担心。'
        ticket=self.reserve('two',text);self.store.finish_episode(ticket)
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        self.store.apply_episode(job,result(text,unfinished=False,resolves=[old['id']]),self.refs([old]),90)
        records=read_episode_context(self.store,'person','xiaoyuan','兰花')[0]['records']
        self.assertTrue(all(not r['unfinished'] for r in records))

    def test_rephrasing_keeps_new_suggestion_without_rewriting_old_event(self):
        old=self.save();text='那你再帮我换一种更自然的说法。'
        source=dict(user_text=text,delivery_status='generated')
        value=validate_episode(result(text,supersedes=[old['id']],resolves=[old['id']]),source,[old])
        self.assertTrue(value['keep']);self.assertEqual(value['supersedes'],[])
        self.assertEqual(value['resolves'],[]);self.assertIn('rejected_links',value)

    def test_only_explicit_resolution_closes_an_unfinished_event(self):
        old=self.save();text='兰花那事已经解决了，现在不用担心。'
        source=dict(user_text=text,delivery_status='generated')
        self.assertEqual(validate_episode(result(text,resolves=[old['id']]),source,[old])['resolves'],[old['id']])
        old['payload']['unfinished']=False
        self.assertEqual(validate_episode(result(text,resolves=[old['id']]),source,[old])['resolves'],[])

    def test_interruption_preserves_user_but_withdraws_role_statement(self):
        row=self.save();_,token=read_episode_context(self.store,'person','xiaoyuan','兰花')
        ticket=dict(source_id=row['source_id'],id=row['source_id'],epoch=row['epoch'],scope='person')
        self.store.episode_delivery(ticket,'interrupted')
        context,_=read_episode_context(self.store,'person','xiaoyuan','兰花')
        item=context['records'][0]
        self.assertEqual(item['role_reply']['text'],'');self.assertEqual(item['role_reaction'],'')
        self.assertEqual(item['user_quote']['text'],'我担心兰花养不好。')
        self.assertFalse(self.store.episodes_current('person','xiaoyuan',token))

    def test_audio_sent_is_not_a_fact_revision_or_hearing_claim(self):
        row=self.save();_,token=read_episode_context(self.store,'person','xiaoyuan','兰花')
        ticket=dict(source_id=row['source_id'],id=row['source_id'],epoch=row['epoch'],scope='person')
        self.store.episode_delivery(ticket,'audio_sent')
        self.assertTrue(self.store.episodes_current('person','xiaoyuan',token))

    def test_forget_purges_new_tables_and_blocks_late_apply(self):
        self.save();ticket=self.reserve('two');self.store.finish_episode(ticket)
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        self.ledger.forget('person',keys=['name'])
        self.assertFalse(self.store.apply_episode(job,result(),[],90))
        with self.ledger.db() as db:
            for table in ('companion_episodes','companion_episode_sources','companion_episode_jobs','companion_episode_audit'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_maintenance_revision_and_delete_do_not_resurrect_inflight_context(self):
        old=self.save();_,token=read_episode_context(self.store,'person','xiaoyuan','兰花')
        self.assertTrue(self.store.maintain_episode('person','xiaoyuan',old['id'],1,{'interpretation':'用户只想聊聊。'},note='review'))
        self.assertFalse(self.store.episodes_current('person','xiaoyuan',token))
        self.assertFalse(self.store.maintain_episode('person','xiaoyuan',old['id'],1,delete=True))
        ticket=self.reserve('two');self.store.finish_episode(ticket);job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        current=self.store.episode_records('person','xiaoyuan')[1][0]
        self.assertTrue(self.store.maintain_episode('person','xiaoyuan',current['id'],2,delete=True))
        self.assertFalse(self.store.apply_episode(job,result(),self.refs([current]),90))
        self.assertFalse(self.store.episode_records('person','xiaoyuan')[1])

    def test_duplicates_capacity_and_expiry(self):
        ticket=self.reserve(capacity=1);self.assertIsNone(self.reserve('two',capacity=1))
        self.store.finish_episode(ticket);self.assertFalse(self.store.finish_episode(ticket))
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        self.store.apply_episode(job,result(),[],90);self.assertIsNone(self.reserve())
        with self.ledger.db() as db:db.execute('UPDATE companion_episodes SET expires_at=0')
        self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        with self.ledger.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM companion_episode_sources').fetchone()[0],0)

    def test_skipped_turn_has_no_episode_and_clears_raw_source(self):
        ticket=self.reserve(text='今天随便聊聊。');self.store.finish_episode(ticket)
        job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint)
        self.assertTrue(self.store.apply_episode(job,result('',keep=False),[],90))
        self.assertIsNone(self.store.episode_source(ticket))

    def test_retry_is_bounded(self):
        ticket=self.reserve();self.store.finish_episode(ticket)
        for attempt in (1,2):
            job=self.store.claim_episode('person','xiaoyuan',self.p.fingerprint);self.assertEqual(job['attempts'],attempt)
            self.store.fail_episode(job,'TimeoutError')
            with self.ledger.db() as db:db.execute('UPDATE companion_episode_jobs SET due=0')
        self.assertIsNone(self.store.claim_episode('person','xiaoyuan',self.p.fingerprint))

    def test_restart_runtime_processes_durable_pending_source(self):
        ticket=self.reserve();self.store.finish_episode(ticket)
        managed=NS(scope='person',engine=NS(store=self.ledger))
        def extract(system,payload,schema,**kw):return json.dumps(result(json.loads(payload)['source']['user_text']))
        recovered=EpisodeRuntime(managed,self.p,NS(response_structured=extract),start=False)
        self.assertTrue(recovered.step());self.assertEqual(len(recovered.store.episode_records('person','xiaoyuan')[1]),1)
        recovered.close()

    def test_late_first_extraction_does_not_revive_an_already_corrected_source(self):
        old=self.reserve('older');new=self.reserve('newer','你记错了，我担心的是薄荷，不是兰花。')
        self.store.finish_episode(new);self.store.finish_episode(old)
        managed=NS(scope='person',engine=NS(store=self.ledger));calls=[]
        def extract(system,payload,schema,**kw):
            calls.append(payload);return json.dumps(result(json.loads(payload)['source']['user_text'],topic='薄荷'))
        runtime=EpisodeRuntime(managed,self.p,NS(response_structured=extract),start=False)
        self.assertTrue(runtime.step());self.assertTrue(runtime.step());self.assertEqual(len(calls),1)
        self.assertEqual(len(self.store.episode_records('person','xiaoyuan')[1]),1);runtime.close()


class EpisodeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    add=natural.NaturalTests.add;chat=natural.NaturalTests.chat;texts=staticmethod(natural.NaturalTests.texts)
    def setUp(self):
        natural.NaturalTests.setUp(self);original=EpisodeRuntime
        self.patch=patch('core.utils.companion_memory.EpisodeRuntime',side_effect=lambda *a,**kw:original(*a,start=False,**kw));self.patch.start()
    def tearDown(self):
        for runtime in getattr(self.memory.engine,'_companion_episode_runtimes',{}).values():runtime.close()
        self.patch.stop();natural.NaturalTests.tearDown(self)
    def connection(self,direct=False):
        c=q1.RoleWorkflowTests.connection(self,lambda m:iter(['听您说这件事，我有些关心。']))
        c.config['dialogue_quality']['strategy']='role_direct' if direct else 'legacy'
        c.config['companion_memory']=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=[self.memory.scope])
        original=c.intent.llm.response_structured
        def structured(system,payload,schema,**kw):
            if 'keep' in schema.get('properties',{}):
                source=json.loads(payload)['source'];return json.dumps(result(source['user_text']))
            return original(system,payload,schema,**kw)
        c.intent.llm.response_structured=structured
        return c

    async def test_actual_a_final_source_and_b_same_episode_after_new_call(self):
        a=self.connection();await self.chat(a,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()));self.assertTrue(runtime.step())
        b=self.connection(direct=True);b.session_id='new-call';b.device_id='another-terminal'
        b.config['dialogue_quality']['allowed_device_ids'].append(b.device_id)
        await self.chat(b,'上次那事，接着聊。',{'route':'chat','input_origin':'asr','recent_context_needed':True})
        self.assertIn('companion_episodes',str(b.role_calls[-1]));self.assertIn('我担心兰花养不好',str(b.role_calls[-1]))
        self.assertFalse(b.review_calls)

    async def test_actual_a_reads_episodes_for_existing_facts_route_with_continuity(self):
        c=self.connection();await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()));runtime.step()
        c=self.connection();c.session_id='new-call'
        await self.chat(c,'上次那件事是什么？',{'route':'memory_query','memory_query':'facts','input_origin':'asr',
                    'recent_context_needed':True,'memory_selection':[],'memory_read_status':'not_found'})
        self.assertIn('companion_episodes',c.review_calls[-1]['current_turn_data'])

    async def test_assistant_history_flag_alone_recalls_archived_role_reply(self):
        c=self.connection();await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()));runtime.step()
        c=self.connection();c.session_id='new-call';self.memory.config['recent_context_enabled']=False
        await self.chat(c,'上次你说的那句话是什么？',{'route':'chat','input_origin':'asr',
            'recent_context_needed':False,'related_context_needed':False,'assistant_history_needed':True})
        data=c.review_calls[-1]['current_turn_data']
        self.assertTrue(data['companion_episodes']['records'][0]['role_reply']['text'])

    async def test_identity_stays_canonical_and_new_topic_does_not_force_old_event(self):
        self.add('以后叫我周老师。');c=self.connection()
        await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()));runtime.step()
        await self.chat(c,'怎么称呼我？',{'route':'memory_query','memory_query':'preferred_address','input_origin':'asr'})
        self.assertNotIn('companion_episodes',c.review_calls[-1]['current_turn_data'])
        self.assertIn('周老师',str(c.review_calls[-1]['current_turn_data']['personal_memory_read']))
        await self.chat(c,'今天想聊水墨画。',{'route':'chat','input_origin':'asr'})
        self.assertNotIn('companion_episodes',c.review_calls[-1]['current_turn_data'])

    async def test_delete_during_generation_reloads_without_stale_episode(self):
        c=self.connection();await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()));runtime.step()
        old=runtime.store.episode_records(self.memory.scope,'xiaoyuan')[1][0];calls=[]
        def author(messages):
            calls.append(messages)
            if len(calls)==1:
                runtime.store.maintain_episode(self.memory.scope,'xiaoyuan',old['id'],old['revision'],delete=True)
                return iter(['不应交付的旧经历。'])
            self.assertNotIn('companion_episodes',str(messages));return iter(['咱们接着聊。'])
        b=q1.RoleWorkflowTests.connection(self,author);b.config['companion_memory']=c.config['companion_memory']
        await self.chat(b,'上次那件事？',{'route':'chat','input_origin':'asr','recent_context_needed':True})
        self.assertEqual(len(calls),2);self.assertNotIn('不应交付',self.texts(b))

    async def test_abort_preserves_user_source_and_queues_without_role(self):
        c=self.connection();c.sentence_id='turn';c.lookup_generation=1
        bundle=prepare_episode_turn(c,{'input_origin':'asr'},'我担心兰花养不好。','turn')
        mark_episode_delivery(c,'turn','interrupted');runtime,ticket=bundle;runtime.step()
        row=runtime.store.episode_records(self.memory.scope,'xiaoyuan')[1][0]
        self.assertEqual(row['delivery_status'],'interrupted');self.assertEqual(row['payload']['role_reaction'],'')

    async def test_actual_tts_and_abort_hooks_track_delivery_without_losing_user(self):
        from core.handle.sendAudioHandle import send_tts_message
        from core.handle.abortHandle import handleAbortMessage
        c=self.connection();c.websocket=NS(send=AsyncMock());c.clearSpeakStatus=lambda:None;c.clear_queues=lambda:None
        await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime,ticket=c._episode_turns[c.sentence_id]
        with patch('core.handle.sendAudioHandle._wait_for_audio_completion',new=AsyncMock()):
            await send_tts_message(c,'stop')
            self.assertEqual(runtime.store.episode_source(ticket)['delivery_status'],'generated')
            c._recent_audio_sentence=c.sentence_id
            await send_tts_message(c,'stop')
            self.assertEqual(runtime.store.episode_source(ticket)['delivery_status'],'audio_sent')
        await handleAbortMessage(c);runtime.step()
        row=runtime.store.episode_records(self.memory.scope,'xiaoyuan')[1][0]
        self.assertEqual(row['delivery_status'],'interrupted');self.assertEqual(row['payload']['user_quote'],'我担心兰花养不好。')

    def test_outer_provider_denial_overrides_a_stale_inner_scope(self):
        from core.providers.memory.powermem.powermem import MemoryProvider
        from core.utils.companion_affect import select_affect
        c=self.connection();provider=MemoryProvider.__new__(MemoryProvider)
        provider._managed=self.memory;provider.managed=False;provider._memory_scope_denied=True;c.memory=provider
        c.config['companion_affect']=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=[self.memory.scope])
        self.assertIsNone(select_episodes(c));self.assertIsNone(select_affect(c))
        with self.assertRaises(PermissionError):provider.read_companion_episodes('xiaoyuan','上次')
        with self.assertRaises(PermissionError):provider.companion_episode_records('xiaoyuan')

    async def test_disabled_input_equal_and_no_q3_tables(self):
        c=self.connection();c.config['companion_memory']['enabled']=False
        await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        self.assertNotIn('companion_episodes',str(c.review_calls));self.assertFalse(hasattr(self.memory.engine,'_companion_episode_runtimes'))
        with self.memory.engine.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='companion_episodes'").fetchone())

    async def test_disabling_cancels_pending_extraction(self):
        c=self.connection();await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()))
        c.config['companion_memory']['enabled']=False
        await self.chat(c,'继续聊聊。',{'route':'chat','input_origin':'asr'})
        self.assertFalse(runtime.step());self.assertFalse(runtime.store.episode_records(self.memory.scope,'xiaoyuan')[1])

    async def test_slow_episode_extraction_does_not_block_next_reply(self):
        c=self.connection();await self.chat(c,'我担心兰花养不好。',{'route':'chat','input_origin':'asr'})
        runtime=next(iter(self.memory.engine._companion_episode_runtimes.values()))
        started,release=threading.Event(),threading.Event()
        def slow(system,payload,schema,**kw):
            started.set();release.wait(2);return json.dumps(result(json.loads(payload)['source']['user_text']))
        runtime.model=NS(response_structured=slow)
        task=asyncio.create_task(asyncio.to_thread(runtime.step));self.assertTrue(await asyncio.to_thread(started.wait,1))
        try:await asyncio.wait_for(self.chat(c,'我接着说两句。',{'route':'chat','input_origin':'asr'}),.7)
        finally:release.set();await task

    async def test_router_requests_history_metadata_with_archives_even_when_recent_window_off(self):
        from core.utils.information_router import ReadOnlySession
        c=self.connection();self.memory.config['recent_context_enabled']=False
        def route(system,payload,schema,**kw):
            self.assertIn('recent_context_needed',schema['required'])
            return json.dumps(dict(route='chat',requests=[],clarification_slot=None,memory_query=None,user_fact_statement=False,
                related_context_needed=True,recent_context_needed=True,assistant_history_needed=True))
        c.intent.llm.response_structured=route
        plan=await ReadOnlySession(c).plan('接着上次那件事聊。')
        self.assertTrue(plan['_recent_context_needed'])

    def test_scope_and_config_and_secret_source_fences(self):
        c=self.connection();self.assertIsNotNone(select_episodes(c))
        self.assertIsNone(prepare_episode_turn(c,{'input_origin':'asr'},'我的密码是SYNTHETIC','turn'))
        c.config['companion_memory']['allowed_subject_scopes']=[];self.assertIsNone(select_episodes(c))


class EpisodeConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_local_episode_configuration(self):
        from config import config_loader
        with patch.object(config_loader,'init_service'),patch.object(config_loader,'get_server_config',
                new=AsyncMock(return_value={'server':{'auth':{'enabled':True}}})):
            block=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=['synthetic'])
            value=await config_loader.get_config_from_api_async({'manager-api':{'url':'https://example.invalid'},'companion_memory':block})
        self.assertEqual(value['companion_memory'],block)
