"""Continuous PCM/ASR ordering, quiet idle, opportunity migration and fencing."""
import asyncio,concurrent.futures,json,time,unittest,tempfile,threading
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from core.utils.companion_initiative import (voice_activity,input_finished,current_input_token,listen_started,user_input,reply_submitted,transport_finished)
from core.handle.receiveAudioHandle import handleAudioMessage
from core.utils.companion_store import InitiativeStore,TopicStore
from core.providers.memory.powermem.store import Store
from core.providers.asr.aliyunbl_stream import ASRProvider
import test_companion_initiative as old

class InputTests(unittest.IsolatedAsyncioTestCase):
 setUp=old.RuntimeTests.setUp
 tearDown=old.RuntimeTests.tearDown
 asyncTearDown=old.RuntimeTests.asyncTearDown
 conn=old.RuntimeTests.conn
 runtime=old.RuntimeTests.runtime
 ticket=old.RuntimeTests.ticket
 async def test_final_then_trailing_silent_pcm_and_cleanup_stays_idle(self):
  r=await self.runtime();c=r.conn;c.vad=NS(is_vad=lambda *a:False);c.asr=NS(receive_audio=AsyncMock());c.client_aec=False;c.last_activity_time=0
  await voice_activity(c,True);token=current_input_token(c);await voice_activity(c,False,True)
  await input_finished(c,token);await user_input(c,dict(id='final',origin='asr',input_token=token,text='今天我把阳台的小葱种下去了，头一次种，挺开心。'))
  c.client_voice_stop=True
  for _ in range(12):await handleAudioMessage(c,b'\0'*1920)
  c.client_voice_stop=False;await input_finished(c,token)
  future=concurrent.futures.Future();future.set_result(None);reply_submitted(c,dict(id='final'),dict(route='chat'),future)
  r.generate=AsyncMock();now=time.time()
  with patch('time.time',return_value=now+30):await r.tick();await asyncio.sleep(0);self.assertFalse(r.busy())
  self.assertFalse(r.asr_pending);self.assertEqual(r.generate.await_count,1)
 async def test_old_completion_cannot_release_newer_voice(self):
  r=await self.runtime();c=r.conn;await voice_activity(c,True);oldtoken=current_input_token(c);await voice_activity(c,False,True)
  await voice_activity(c,True);newtoken=current_input_token(c);self.assertNotEqual(oldtoken,newtoken)
  await voice_activity(c,False,True);await input_finished(c,oldtoken)
  self.assertTrue(r.asr_pending);self.assertEqual(current_input_token(c),newtoken)
  await input_finished(c,newtoken);self.assertFalse(r.asr_pending)
 async def test_missing_final_has_bounded_wait_but_genuine_speech_is_not_expired(self):
  r=await self.runtime();c=r.conn;start=time.time()
  with patch('time.time',return_value=start):await voice_activity(c,True);await voice_activity(c,False,True)
  with patch('time.time',return_value=start+10):self.assertTrue(r.busy())
  with patch('time.time',return_value=start+16):self.assertFalse(r.busy());self.assertFalse(r.input_open)
  with patch('time.time',return_value=start+40):await voice_activity(c,True);self.assertTrue(r.busy())
  with patch('time.time',return_value=start+41):await voice_activity(c,True);self.assertTrue(r.busy())
 async def test_microphone_open_and_empty_stop_are_not_recognition(self):
  r=await self.runtime();await listen_started(r.conn)
  for _ in range(5):await voice_activity(r.conn,False,True)
  self.assertFalse(r.input_open);self.assertFalse(r.asr_pending);self.assertIsNone(current_input_token(r.conn))
 async def test_provider_final_cleanup_releases_its_own_input(self):
  r=await self.runtime();c=r.conn;await voice_activity(c,True);await voice_activity(c,False,True)
  provider=ASRProvider({'model':'qwen-audio-3.1-asr-flash-message'},False);provider._initiative_input_token=current_input_token(c)
  messages=asyncio.Queue();messages.put_nowait(json.dumps(dict(header=dict(event='task-finished'))))
  provider.asr_ws=NS(recv=messages.get,send=AsyncMock(),close=AsyncMock());provider.task_id='task';c.asr_audio=[];c.reset_audio_states=lambda:None
  await provider._forward_results(c);self.assertFalse(r.asr_pending);self.assertFalse(r.input_open)
 async def test_provider_late_cleanup_preserves_new_voice_and_buffers(self):
  r=await self.runtime();c=r.conn;await voice_activity(c,True);await voice_activity(c,False,True)
  token=current_input_token(c);await voice_activity(c,True);new=current_input_token(c)
  provider=ASRProvider({'model':'qwen-audio-3.1-asr-flash-message'},False);provider._initiative_input_token=token
  messages=asyncio.Queue();messages.put_nowait(json.dumps(dict(header=dict(event='task-finished'))))
  provider.asr_ws=NS(recv=messages.get,send=AsyncMock(),close=AsyncMock());provider.task_id='old';c.asr_audio=[b'new'];c.reset_audio_states=__import__('unittest.mock').mock.Mock()
  await provider._forward_results(c);self.assertEqual(current_input_token(c),new);c.reset_audio_states.assert_not_called()
 async def test_silent_initiative_restores_calling_playback_owner(self):
  r=await self.runtime(self.conn(reply='[[SILENT]]'));c=r.conn;c.client_is_speaking=True;r.transport_done=c.sentence_id;r.transport_at=time.time()-4
  t=await self.ticket(r);before=c.sentence_id;await r.generate(t)
  self.assertEqual(c.sentence_id,before);self.assertFalse(r.busy());self.assertFalse(r.delivery)
 async def test_two_existing_sourced_topics_can_be_used_without_new_user_input(self):
  r=await self.runtime();c=r.conn;now=time.time();topics=TopicStore(self.engine.store)
  for turn,label in [('earlier','旧收音机'),('human','阳台小葱')]:
   topics.apply_topic(r.scope,'xiaoyuan',r.epoch,c.session_id,turn,dict(topic_action='new',topic_id='',topic_label=label,pending_point='之后接着聊',preferences=[],evidence_quote=label),3600)
  r.user_turn=('human','human');r.generate=AsyncMock()
  await r.tick();await asyncio.sleep(0);first=r.generate.call_args.args[0];r.store.commit(first,'one','第一条补充');r.store.settle(first,'audio_sent')
  seq=r.store.read_subject(r.scope,'xiaoyuan')['user_seq']
  with patch('time.time',return_value=now+190):
   r.idle_since=now;r.generation_task=None;await r.tick();await asyncio.sleep(0)
  self.assertEqual(r.generate.await_count,2);second=r.generate.call_args.args[0]
  self.assertNotEqual(first['opportunity_key'],second['opportunity_key']);self.assertEqual(first['user_seq'],second['user_seq']);self.assertEqual(seq,second['user_seq'])
 async def test_gate_reason_is_queryable_without_user_text(self):
  r=await self.runtime();await voice_activity(r.conn,True);await voice_activity(r.conn,False,True);await r.tick()
  row=r.store.read_subject(r.scope,'xiaoyuan');self.assertEqual(row['gate_reason'],'asr_pending');self.assertEqual(row['gate_session'],r.conn.session_id)

class MigrationTests(unittest.TestCase):
 def test_old_unique_schema_migrates_preserving_rows_and_forget(self):
  with tempfile.TemporaryDirectory() as td:
   ledger=Store(Path(td)/'db.sqlite');store=InitiativeStore(ledger);store.user_input('s','xiaoyuan','one','event','真实来源')
   with store.db() as db:
    db.execute('DROP TRIGGER forget_companion_initiative_candidates');db.execute('DROP TABLE companion_initiative_candidates')
    db.execute("CREATE TABLE companion_initiative_candidates(id TEXT PRIMARY KEY,scope TEXT NOT NULL,companion_id TEXT NOT NULL,epoch INTEGER NOT NULL,user_seq INTEGER NOT NULL,owner TEXT NOT NULL,topic_key TEXT NOT NULL,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,deadline REAL NOT NULL,status TEXT NOT NULL,turn_id TEXT NOT NULL DEFAULT '',reply_text TEXT NOT NULL DEFAULT '',completed_at REAL,UNIQUE(scope,companion_id,epoch,user_seq))")
    db.execute("INSERT INTO companion_initiative_candidates(id,scope,companion_id,epoch,user_seq,owner,topic_key,source_kind,source_id,source_revision,payload,created_at,deadline,status) VALUES('old','s','xiaoyuan',0,1,'one','topic','user','event',1,'{}',100,160,'skipped')")
   store=InitiativeStore(ledger);store=InitiativeStore(ledger)
   with store.db() as db:
    row=dict(db.execute('SELECT * FROM companion_initiative_candidates').fetchone());self.assertEqual(row['id'],'old');self.assertEqual(row['status'],'skipped');self.assertTrue(row['opportunity_key'])
    # Older code can still insert rows without knowing the optional key.
    for i in (2,3):db.execute("INSERT INTO companion_initiative_candidates(id,scope,companion_id,epoch,user_seq,owner,topic_key,source_kind,source_id,source_revision,payload,created_at,deadline,status) VALUES(?, 's','xiaoyuan',0,?,'one','topic','user',?,1,'{}',100,160,'skipped')",('legacy-'+str(i),i,'event-'+str(i)))
   ledger.forget('s',[])
   with store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM companion_initiative_candidates').fetchone()[0],0)

class MoreInputTests(unittest.IsolatedAsyncioTestCase):
 setUp=InputTests.setUp
 tearDown=InputTests.tearDown
 asyncTearDown=InputTests.asyncTearDown
 conn=InputTests.conn
 runtime=InputTests.runtime
 ticket=InputTests.ticket
 async def test_base_empty_recognition_finishes_wait(self):
  r=await self.runtime();c=r.conn;c.voiceprint_provider=None;c.asr_audio=[];c.chat_history_conf=0
  await voice_activity(c,True);await voice_activity(c,False,True)
  provider=ASRProvider({'model':'qwen-audio-3.1-asr-flash-message'},False);provider._initiative_input_token=current_input_token(c)
  provider.speech_to_text_wrapper=AsyncMock(return_value=('',None))
  await provider.handle_voice_stop(c,[b'\0'*1920]);self.assertFalse(r.input_open);self.assertFalse(r.asr_pending)
 async def test_base_failed_recognition_finishes_wait(self):
  r=await self.runtime();c=r.conn;c.voiceprint_provider=None;c.asr_audio=[];c.chat_history_conf=0
  await voice_activity(c,True);await voice_activity(c,False,True)
  provider=ASRProvider({'model':'qwen-audio-3.1-asr-flash-message'},False);provider._initiative_input_token=current_input_token(c)
  provider.speech_to_text_wrapper=AsyncMock(side_effect=TimeoutError('synthetic'))
  await provider.handle_voice_stop(c,[b'\0'*1920]);self.assertFalse(r.input_open);self.assertFalse(r.asr_pending)
 async def test_new_voice_clears_stale_stop_and_abort_does_not_release_it(self):
  r=await self.runtime();c=r.conn;c.client_voice_stop=True
  await voice_activity(c,True);self.assertFalse(c.client_voice_stop)
  token=current_input_token(c);r.cancel('output cancelled',abort=False)
  self.assertEqual(token,current_input_token(c));self.assertTrue(r.busy())
