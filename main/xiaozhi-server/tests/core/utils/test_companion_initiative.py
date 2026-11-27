"""Sourced initiative, three-terminal coordination and cancellable delivery."""
import asyncio,json,tempfile,time,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,AsyncMock
from core.providers.memory.powermem.store import Store
from core.utils.companion_store import InitiativeStore,TopicStore,EpisodeStore
from core.utils.companion_initiative import (InitiativeRuntime,select_initiative,quiet_command,user_input,
 voice_activity,reply_submitted,output_allowed,transport_finished,SILENT,AUTHOR_RULES)
from core.utils.dialogue import Message
import test_natural_response as natural

class LeaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.ledger=Store(Path(self.tmp.name)/'ledger.sqlite');self.s=InitiativeStore(self.ledger)
  self.event('e1');self.presence()
 def tearDown(self):self.tmp.cleanup()
 def event(self,event,session='one',stamp=100,quiet=None):
  self.s.user_input('person','xiaoyuan',session,event,'今天我把阳台的小葱种下去了，挺开心的。',quiet,3600,stamp)
  self.s.route_result('person','xiaoyuan',event,'chat')
 def presence(self,session='one',busy=False,voice=0,stamp=125):self.s.presence('person','xiaoyuan',session,session,busy,voice,60,stamp)
 def material(self):
  row=self.s.read_subject('person','xiaoyuan')
  return dict(topic_key='topic:test',source_kind='user',source_id=row['last_event'],source_revision=row['user_seq'],user_quote=row['last_text'])
 def claim(self,session='one',stamp=125,**kw):
  opts=dict(silence=20,cooldown=30,ttl=60,source_age=600,max_per_session=2,topic_cooldown=60,stamp=stamp);opts.update(kw)
  return self.s.claim('person','xiaoyuan',session,self.material(),**opts)
 def test_three_connections_only_latest_human_terminal_can_claim(self):
  self.presence('two');self.presence('three');self.assertIsNone(self.claim('two'));self.assertIsNone(self.claim('three'));self.assertIsNotNone(self.claim())
 def test_concurrent_same_opportunity_only_one_winner(self):
  with ThreadPoolExecutor(3) as pool:rows=list(pool.map(lambda _:self.claim(),range(3)))
  self.assertEqual(sum(bool(r) for r in rows),1)
 def test_shared_quiet_and_explicit_resume(self):
  self.event('quiet','two',101,True);self.presence('two');self.assertIsNone(self.claim('two',stamp=126))
  self.event('resume','two',102,False);self.assertIsNotNone(self.claim('two',stamp=127))
 def test_peer_voice_or_work_blocks_claim(self):
  self.presence('two',busy=True);self.assertIsNone(self.claim())
  self.presence('two',voice=140);self.assertIsNone(self.claim())
  self.presence('two');self.assertIsNotNone(self.claim())
 def test_dead_peer_lease_expires_and_owner_must_be_present(self):
  self.presence('two',busy=True,stamp=30);self.assertIsNotNone(self.claim())
  self.s.disconnect('person','xiaoyuan','one');self.assertIsNone(self.claim())
 def test_silent_or_failed_not_retried_on_same_user_event(self):
  ticket=self.claim();self.s.settle(ticket,'skipped',126)
  self.assertIsNone(self.claim(stamp=170));self.event('e2',stamp=150);self.presence(stamp=175)
  self.assertIsNotNone(self.claim(stamp=175))
 def test_cooldown_is_subject_wide(self):
  t=self.claim();self.s.settle(t,'skipped',126);self.event('e2',session='two',stamp=105);self.presence('two')
  self.assertIsNone(self.claim('two',stamp=130));self.presence('two',stamp=160);self.assertIsNotNone(self.claim('two',stamp=160))
 def test_claim_expiry_cannot_commit_or_replay(self):
  t=self.claim();self.assertFalse(self.s.current(t,stamp=186));self.assertFalse(self.s.commit(t,'out','话',186));self.presence(stamp=200)
  self.assertIsNone(self.claim(stamp=200))
 def test_user_input_invalidates_captured_generation(self):
  t=self.claim();self.event('e2',stamp=126);self.assertFalse(self.s.current(t,stamp=127));self.assertFalse(self.s.commit(t,'out','旧话',127))
 def test_commit_consumes_before_enqueue_and_crash_is_not_replayed(self):
  t=self.claim();self.assertTrue(self.s.commit(t,'out','这就有个盼头啦。',126));self.s.disconnect('person','xiaoyuan','one')
  self.presence(stamp=170);self.assertIsNone(self.claim(stamp=170))
 def test_delivery_rechecks_other_device_and_never_revives_interrupted(self):
  t=self.claim();self.assertTrue(self.s.commit(t,'out','话',126));self.presence('two',busy=True)
  self.assertFalse(self.s.current(t,delivery=True,stamp=127));self.s.settle(t,'interrupted',128);self.s.settle(t,'audio_sent',129)
  with self.s.db() as db:self.assertEqual(db.execute('SELECT status FROM companion_initiative_candidates').fetchone()[0],'interrupted')
 def test_partial_forget_erases_all_initiative_material_and_fences_old_ticket(self):
  t=self.claim();self.ledger.forget('person',[]);self.assertFalse(self.s.current(t,stamp=127))
  with self.s.db() as db:
   for table in ('subjects','presence','candidates'):
    rows=db.execute('SELECT * FROM companion_initiative_'+table).fetchall()
    # current() may create a fresh empty subject, with no source text.
    self.assertTrue(not rows or (table=='subjects' and not rows[0]['last_text']))
 def test_stale_route_completion_cannot_change_new_event(self):
  self.event('e2');self.s.route_result('person','xiaoyuan','e1','lookup')
  self.assertEqual(self.s.read_subject('person','xiaoyuan')['last_route'],'chat')
 def test_epoch_advanced_by_older_binary_cannot_revive_optional_sources(self):
  t=self.claim()
  with self.ledger.db() as db:db.execute('UPDATE scopes SET epoch=epoch+1 WHERE scope=?',('person',))
  with self.ledger.db() as db:
   self.assertEqual(db.execute('SELECT count(*) FROM companion_initiative_candidates').fetchone()[0],0)
  self.assertFalse(self.s.current(t,stamp=126));fresh=self.s.read_subject('person','xiaoyuan')
  self.assertEqual(fresh['last_text'],'');self.assertEqual(fresh['user_seq'],0)
 def test_tool_route_is_not_an_initiative_opportunity(self):
  self.s.route_result('person','xiaoyuan','e1','lookup');self.assertIsNone(self.claim())
 def test_session_cap_and_topic_cooldown(self):
  t=self.claim();self.s.commit(t,'out','话',126);self.s.settle(t,'audio_sent',127)
  self.event('e2',stamp=135);self.presence(stamp=160);self.assertIsNone(self.claim(stamp=160))
  self.presence(stamp=200);self.assertIsNone(self.claim(stamp=200,max_per_session=1))
 def test_source_topic_revision_change_withdraws_candidate(self):
  topics=TopicStore(self.ledger);epoch=self.s.read_subject('person','xiaoyuan')['epoch']
  update=dict(topic_action='new',topic_id=None,topic_label='种小葱',pending_point='慢慢等长出来',preferences=[],evidence_quote='种下去了')
  topics.apply_topic('person','xiaoyuan',epoch,'one','e1',update,3600)
  _,rows=topics.catalog('person','xiaoyuan');r=rows[0]
  material=dict(topic_key='topic:'+r['id'],source_kind='topic',source_id=r['id'],source_revision=r['revision'])
  t=self.s.claim('person','xiaoyuan','one',material,silence=20,cooldown=30,ttl=60,source_age=600,max_per_session=2,topic_cooldown=60,stamp=125)
  self.assertIsNotNone(t)
  with topics.db() as db:db.execute('UPDATE companion_topics SET revision=revision+1')
  self.assertFalse(self.s.current(t,stamp=126))

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  natural.NaturalTests.setUp(self);self.runtimes=[]
 tearDown=natural.NaturalTests.tearDown
 async def asyncTearDown(self):
  for r in self.runtimes:await r.close()
 def conn(self,reply='刚种下去那会儿最有盼头了，我也挺喜欢看新芽冒出来。',review=None):
  c=natural.NaturalTests.connection(self,lambda m:iter([reply]),review)
  c.config['companion_initiative']=dict(enabled=True,allowed_subject_scopes=['synthetic'])
  c.websocket=NS(closed=False,send=AsyncMock());c.stop_event=threading.Event();c.calling=True;c.close_after_chat=False
  c.client_is_speaking=False;c.loop=asyncio.get_running_loop();c.sentence_id='old';c.intent_type='intent_llm'
  c.intent.read_only=True;c.device_id='one';c.session_id='call-one'
  c.dialogue.put(Message('user','今天我把阳台的小葱种下去了，挺开心的。'))
  c.dialogue.put(Message('assistant','种上就能慢慢等它长大啦。'))
  return c
 async def runtime(self,c=None):
  c=c or self.conn();r=InitiativeRuntime(c,select_initiative(c),start=False);c._initiative_runtime=r
  self.runtimes.append(r);r.armed=True;r.idle_since=time.time()-25
  r.store.user_input(r.scope,'xiaoyuan',c.session_id,'human','今天我把阳台的小葱种下去了，挺开心的。',stamp=time.time()-25)
  r.store.route_result(r.scope,'xiaoyuan','human','chat');await r.pulse();return r
 async def ticket(self,r):
  s=r.store.read_subject(r.scope,'xiaoyuan');m=r.material(s);p=r.policy
  return r.store.claim(r.scope,'xiaoyuan',r.conn.session_id,m,silence=p.min_silence_seconds,cooldown=p.cooldown_seconds,ttl=p.candidate_ttl_seconds,source_age=p.max_source_age_seconds,max_per_session=p.max_per_session,topic_cooldown=p.topic_cooldown_seconds)
 async def test_disabled_or_scope_denied_creates_no_runtime_or_tables(self):
  c=self.conn();c.config['companion_initiative']['enabled']=False;await user_input(c,{'origin':'user_text','id':'u','text':'你好'})
  self.assertFalse(hasattr(c,'_initiative_runtime'))
  with self.engine.store.db() as db:self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'companion_initiative_%'").fetchone())
  c.config['companion_initiative']['enabled']=True;c.config['companion_initiative']['allowed_subject_scopes']=[];self.assertIsNone(select_initiative(c))
 async def test_device_allowlist_keeps_other_terminals_as_observers(self):
  c=self.conn();c.config['companion_initiative']['allowed_device_ids']=['another'];selected=select_initiative(c)
  self.assertIsNotNone(selected);self.assertFalse(selected[2])
 async def test_configuration_rejects_nan_and_unsafe_lease_spacing(self):
  c=self.conn();c.config['companion_initiative']['cooldown_seconds']=float('nan');self.assertIsNone(select_initiative(c))
  c.config['companion_initiative'].pop('cooldown_seconds');c.config['companion_initiative']['presence_ttl_seconds']=3;self.assertIsNone(select_initiative(c))
 async def test_native_assistant_event_no_fabricated_user_and_followup_sees_it(self):
  r=await self.runtime();c=r.conn;t=await self.ticket(r);self.assertIsNotNone(t)
  users=sum(m.role=='user' for m in c.dialogue.dialogue);await r.generate(t)
  self.assertTrue(r.delivery);self.assertEqual(sum(m.role=='user' for m in c.dialogue.dialogue),users)
  final=c.dialogue.dialogue[-1];self.assertEqual(final.role,'assistant');self.assertEqual(final.origin,'initiative')
  self.assertTrue(c.role_calls);self.assertEqual(c.role_calls[0][-1]['role'],'system');self.assertIn(AUTHOR_RULES,c.role_calls[0][-1]['content'])
  self.assertEqual(len(c.review_calls),1);self.assertIsNone(c.review_calls[0]['current_turn_data']['current_user_utterance'])
  self.assertEqual(c.companion_reply_events[-1]['input_origin'],'initiative')
  c.dialogue.put(Message('user','还没呢。'));native=c.dialogue.get_llm_dialogue()
  self.assertTrue(any(m.get('content')==final.content for m in native));self.assertEqual(native[-1]['content'],'还没呢。')
  self.assertTrue(c.tts.tts_text_queue.qsize()>=3)
  with self.engine.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],0)
 async def test_silent_control_never_enters_history_or_audio(self):
  r=await self.runtime(self.conn(reply=SILENT));t=await self.ticket(r);await r.generate(t)
  self.assertFalse(r.delivery);self.assertTrue(r.conn.tts.tts_text_queue.empty())
  self.assertEqual(len(r.conn.dialogue.dialogue),3)
  self.assertIsNone(await self.ticket(r))
 async def test_generation_cancelled_by_peer_voice_produces_no_audio(self):
  r=await self.runtime();t=await self.ticket(r)
  def response(messages):r.cancel('测试语音',abort=False);return iter(['这句话不能送出。'])
  r.conn.llm.response=lambda sid,messages,**kw:response(messages)
  await r.generate(t);self.assertTrue(r.conn.tts.tts_text_queue.empty());self.assertFalse(r.delivery)
 async def test_quiet_and_new_input_block_already_committed_audio(self):
  r=await self.runtime();t=await self.ticket(r);await r.generate(t);turn=r.conn.sentence_id
  r.store.user_input(r.scope,'xiaoyuan','another','quiet','我想安静一会',True)
  with patch.object(r,'cancel') as cancel:self.assertFalse(await output_allowed(r.conn,turn));cancel.assert_called_once()
 async def test_transport_done_allows_calling_mode_idle_but_not_new_voice(self):
  r=await self.runtime();c=r.conn;c.client_is_speaking=True;self.assertTrue(r.busy())
  await transport_finished(c,c.sentence_id);r.transport_at=time.time()-3
  self.assertFalse(r.busy());r.voice_until=time.time()+2;self.assertTrue(r.busy())
 async def test_asr_pending_blocks_until_actual_transcript(self):
  r=await self.runtime();await voice_activity(r.conn,True);await voice_activity(r.conn,False,True);self.assertTrue(r.busy())
  await user_input(r.conn,dict(id='real',origin='asr',text='今天的太阳照着这盆小葱真暖和。'));self.assertFalse(r.asr_pending);self.assertTrue(r.normal_busy)
 async def test_automatic_wakeup_does_not_arm_or_advance_user_sequence(self):
  r=await self.runtime();r.armed=False;before=r.store.read_subject(r.scope,'xiaoyuan')['user_seq']
  await user_input(r.conn,dict(id='auto',origin='automatic_wakeup',text='嘿，你好呀'))
  self.assertFalse(r.armed);self.assertEqual(r.store.read_subject(r.scope,'xiaoyuan')['user_seq'],before)
 async def test_next_human_terminal_cancels_slow_generation_locally(self):
  a=await self.runtime();c=self.conn();c.session_id='call-two';b=await self.runtime(c)
  a.packet={};await user_input(c,dict(id='two',origin='user_text',text='今天我把阳台的小葱种下去了。'))
  self.assertTrue(a.packet['initiative_cancelled']);self.assertEqual(b.store.read_subject(b.scope,'xiaoyuan')['last_session'],'call-two')
 async def test_no_source_and_listening_only_topic_do_not_call_model(self):
  r=await self.runtime();s=r.store.read_subject(r.scope,'xiaoyuan');s['last_text']='你好';self.assertIsNone(r.material(s))
  topics=TopicStore(self.engine.store);topics.apply_topic(r.scope,'xiaoyuan',r.epoch,r.conn.session_id,'quiettopic',
   dict(topic_action='new',topic_id=None,topic_label='种小葱',pending_point='',preferences=[dict(key='listening_only',value=True,quote='听我说')],evidence_quote='听我说'),3600)
  self.assertIsNone(r.material(r.store.read_subject(r.scope,'xiaoyuan')))
 async def test_close_and_forget_fence_runtime_and_leases(self):
  r=await self.runtime();t=await self.ticket(r);self.engine.store.forget(r.scope,[]);self.assertFalse(r.binding_current());await r.close()
  self.assertFalse(r.store.current(t));self.assertTrue(r.closed)
 async def test_tick_returns_while_slow_author_runs_and_cancels_on_disconnect(self):
  r=await self.runtime();started=asyncio.Event();blocked=asyncio.Event()
  async def generate(ticket):started.set();await blocked.wait()
  r.generate=generate;await r.tick();await started.wait();self.assertFalse(r.generation_task.done())
  await r.close();await asyncio.sleep(0);self.assertTrue(r.generation_task.cancelled())
 async def test_character_direct_uses_same_initiative_system_event(self):
  c=self.conn();c.config['dialogue_quality']=dict(enabled=True,allowed_subject_scopes=['synthetic'],strategy='role_direct',final_model='character')
  r=await self.runtime(c);t=await self.ticket(r);await r.generate(t)
  self.assertTrue(r.delivery);self.assertEqual(len(c.review_calls),0);self.assertEqual(c.role_calls[0][-1]['role'],'system')
 async def test_quiet_command_uses_only_explicit_current_request(self):
  for text in ('我想安静一会。','先别说话了。','你别总问了。','我先休息一会。'):self.assertIs(quiet_command(text),True,text)
  for text in ('你继续说吧。','陪我聊聊天。'):self.assertIs(quiet_command(text),False,text)
  for text in ('他让我“别说话”，我很难过。','如果我说别说话你会怎样？','这事先别聊了。'):self.assertIsNone(quiet_command(text),text)
 async def test_explicit_closure_yields_no_candidate_or_model_call(self):
  r=await self.runtime();s=r.store.read_subject(r.scope,'xiaoyuan');s['last_text']='今天的事说完啦，没别的事了。'
  self.assertIsNone(r.material(s));self.assertFalse(r.conn.role_calls)
 async def test_q2_uses_final_initiative_but_q3_does_not_extract_a_user_source(self):
  from core.utils.companion_affect import AffectPolicy,AffectRuntime
  c=self.conn(reply='太好了，我也高兴。');c.config['companion_affect']=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=['synthetic'],auxiliary_enabled=False)
  c.config['companion_memory']=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=['synthetic'])
  p=AffectPolicy('xiaoyuan',auxiliary_enabled=False);observer=AffectRuntime(self.engine.store,'synthetic',p,None,start=False)
  self.engine._companion_affect_runtimes={('synthetic','xiaoyuan',p.fingerprint):observer}
  r=await self.runtime(c);t=await self.ticket(r)
  try:
   await r.generate(t);self.assertTrue(r.delivery);self.assertTrue(observer.step());self.assertEqual(observer._row()['state']['emotion'],'joy')
   bundle=c._affect_turns[c.sentence_id];self.assertEqual(bundle[2]['user'],'');self.assertEqual(bundle[2]['input_origin'],'initiative')
   self.assertFalse(getattr(c,'_episode_turns',{}))
  finally:observer.close()
 async def test_real_send_guard_drops_queue_after_remote_quiet(self):
  from core.handle.sendAudioHandle import _do_send_audio
  r=await self.runtime();t=await self.ticket(r);await r.generate(t);c=r.conn
  r.store.user_input(r.scope,'xiaoyuan','other','new','我先休息',True)
  with patch.object(r,'cancel'):
   with self.assertRaises(asyncio.CancelledError):await _do_send_audio(c,b'old-opus',dict(sentence_id=c.sentence_id))
  self.assertFalse(any(isinstance(a.args[0],bytes) for a in c.websocket.send.call_args_list))
 async def test_model_failure_stays_silent_instead_of_canned_apology(self):
  c=self.conn();c.llm.response=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError())
  c.intent.llm.response_structured=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError())
  r=await self.runtime(c);t=await self.ticket(r);await r.generate(t)
  self.assertTrue(c.tts.tts_text_queue.empty());self.assertFalse(r.delivery)
 async def test_packet_still_cancelled_when_empty_and_abort_does_not_recurse(self):
  r=await self.runtime();r.packet={};r.delivery=({},'current');r.conn.sentence_id='current';r.conn.client_is_speaking=True
  with patch.object(r.store,'settle'),patch('core.handle.abortHandle.handleAbortMessage',new_callable=AsyncMock) as abort:
   r.cancel('external abort',abort=False);await asyncio.sleep(0);abort.assert_not_called();self.assertTrue(r.packet['initiative_cancelled'])
 async def test_rebinding_runtime_does_not_unprotect_old_queued_audio(self):
  r=await self.runtime();t=await self.ticket(r);await r.generate(t);c=r.conn;oldturn=c.sentence_id
  await r.close();c._initiative_runtime=None
  with patch.object(r,'cancel') as cancel:self.assertFalse(await output_allowed(c,oldturn));cancel.assert_called_once()
 async def test_optional_ledger_failure_does_not_break_human_input_handler(self):
  r=await self.runtime()
  with patch.object(r.store,'user_input',side_effect=RuntimeError('synthetic unavailable')):
   await user_input(r.conn,dict(id='new',origin='asr',text='今天的天气真好，阳台上很暖和。'))
  self.assertTrue(r.closed)
 async def test_proactive_turn_rechecks_owner_at_actual_audio_send(self):
  r=await self.runtime();t=await self.ticket(r);await r.generate(t);old=r.conn.sentence_id;r.conn.sentence_id='new-human-turn'
  with patch.object(r,'cancel'):self.assertFalse(await output_allowed(r.conn,old))
