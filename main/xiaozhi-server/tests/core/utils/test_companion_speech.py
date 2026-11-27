"""Per-turn real wire parameters, text invariance, cancellation and PCM tails."""
import asyncio,hashlib,json,queue,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from dataclasses import replace
from unittest.mock import patch,AsyncMock
import numpy as np
from core.providers.memory.powermem.store import Store
from core.providers.memory.powermem.managed import ManagedMemory
from core.providers.tts.alibl_stream import TTSProvider
import core.providers.tts.alibl_stream as alibl
from core.providers.tts.dto.dto import TTSMessageDTO,SentenceType,ContentType
from core.utils.opus_encoder_utils import OpusEncoderUtils
from core.utils.companion_speech import select_speech_policy,make_delivery,compile_delivery,SpeechQueue,DIRECTIONS
from core.utils.dialogue import Dialogue
import test_natural_response as natural
import test_companion_response as q1

class Quiet:
 def bind(self,**kw):return self
 def __getattr__(self,k):return lambda *a:None
class WS:
 def __init__(self,frames=()):self.frames=list(frames);self.sent=[];self.closed=False
 async def send(self,value):self.sent.append(json.loads(value))
 async def recv(self):return self.frames.pop(0)
 async def close(self):self.closed=True
 def __aiter__(self):return self
 async def __anext__(self):
  if not self.frames:raise StopAsyncIteration
  return self.frames.pop(0)

def message(kind,text=None,turn='turn'):
 return TTSMessageDTO(turn,kind,ContentType.TEXT if text else ContentType.ACTION,content_detail=text)

class SpeechFixture:
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.ledger=Store(Path(self.tmp.name)/'ledger.sqlite')
  self.memory=ManagedMemory.__new__(ManagedMemory);self.memory.scope='person';self.memory.config={};self.memory.engine=NS(store=self.ledger)
  cfg=dict(api_key='synthetic-not-real',model='qwen-audio-3.1-tts-flash',voice='longanlingxin_v3.1',format='pcm',output_dir=self.tmp.name)
  self.p=TTSProvider(cfg,False)
  self.c=NS(config=dict(selected_module={'Memory':'Trial'},Memory={'Trial':{'natural_response_enabled':True}},companion_speech=dict(enabled=True,allowed_subject_scopes=['person'])),
   memory=self.memory,tts=self.p,device_id='synthetic',intent_type='intent_llm',intent=NS(read_only=True),dialogue=Dialogue(),
   sentence_id='turn',lookup_generation=1,client_abort=False,session_id='call',sample_rate=24000,stop_event=threading.Event(),logger=Quiet())
  self.p.conn=self.c;self.p.opus_encoder=OpusEncoderUtils(24000,1,60)
 def tearDown(self):self.p.opus_encoder.close();self.tmp.cleanup()
 def delivery(self,text='我在这儿听着。',query='随便聊聊。'):
  return make_delivery(self.c,text,query,dict(generation=1),'turn')

class SpeechTests(SpeechFixture,unittest.TestCase):
 def test_disabled_unknown_provider_or_unbound_scope_preserve_legacy(self):
  self.c.config['companion_speech']['enabled']=False;self.assertIsNone(select_speech_policy(self.c))
  self.c.config['companion_speech']['enabled']=True
  self.p.model='unknown-model';self.assertIsNone(select_speech_policy(self.c));self.p.model='qwen-audio-3.1-tts-flash'
  self.memory.scope=None;self.assertIsNone(select_speech_policy(self.c))

 def test_metadata_and_instruction_never_enter_spoken_text(self):
  q=SpeechQueue(self.c,'说慢一点。',dict(generation=1),'turn')
  q.put(message(SentenceType.FIRST));self.assertTrue(self.p.tts_text_queue.empty())
  text='我在这儿听着，您慢慢说。';q.put(message(SentenceType.MIDDLE,text));q.put(message(SentenceType.LAST))
  rows=list(self.p.tts_text_queue.queue)
  self.assertEqual([r.sentence_type for r in rows],[SentenceType.FIRST,SentenceType.MIDDLE,SentenceType.LAST])
  self.assertEqual(rows[1].content_detail,text);self.assertEqual(rows[0].delivery.style,'calm')
  self.assertEqual(rows[0].delivery.rate,.85);self.assertTrue(all(r.delivery is rows[0].delivery for r in rows))
  self.assertEqual(rows[0].delivery.text_sha256,hashlib.sha256(text.encode()).hexdigest())

 def test_no_plan_no_delay_and_no_shared_rate_mutation(self):
  self.c.config['companion_speech']['enabled']=False;q=SpeechQueue(self.c,'',dict(generation=1),'turn')
  first=message(SentenceType.FIRST);q.put(first);self.assertIs(self.p.tts_text_queue.get_nowait(),first)
  self.c.config['companion_speech']['enabled']=True
  old=self.p.rate;slow=self.delivery(query='你说慢一点。')
  self.assertEqual(compile_delivery(self.c,self.p,slow,'turn')['rate'],.85);self.assertEqual(self.p.rate,old)
  self.c.sentence_id='next';self.c.lookup_generation=2
  d=make_delivery(self.c,'好，接着聊。','继续。',dict(generation=2),'next')
  self.assertNotIn('rate',compile_delivery(self.c,self.p,d,'next'))

 def test_epoch_binding_generation_abort_disabled_and_expiry_reject_old_directions(self):
  d=self.delivery();self.assertIn('instruction',compile_delivery(self.c,self.p,d,'turn'))
  for attr,value in [('sentence_id','new'),('lookup_generation',2),('client_abort',True)]:
   old=getattr(self.c,attr);setattr(self.c,attr,value);self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{});setattr(self.c,attr,old)
  self.memory.scope='other';self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{});self.memory.scope='person'
  with patch('core.utils.companion_speech.time.monotonic',return_value=d.issued_at+1000):self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{})
  self.ledger.forget('person',[]);self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{})

 def test_forged_plan_and_unsupported_format_are_not_forwarded(self):
  d=self.delivery();self.assertEqual(compile_delivery(self.c,self.p,replace(d,instruction='改成别的内容'),'turn'),{})
  self.p.format='mp3';self.assertIsNone(select_speech_policy(self.c))

 def test_current_words_override_old_joy_and_quoted_slow_is_not_a_request(self):
  self.assertEqual(self.delivery('哈哈，太好了！').style,'bright')
  self.assertEqual(self.delivery('我今天高兴不起来。').style,'calm')
  self.assertEqual(self.delivery('我听着，您慢慢说。').style,'calm')
  self.assertEqual(self.delivery('请立即联系现实帮助。').style,'serious')
  self.assertIsNone(self.delivery(query='他让我“说慢一点”，你怎么看？').rate)
  self.assertIsNone(self.delivery(query='我朋友说慢一点比较好。').rate)
  self.assertEqual(self.delivery(query='请说快一点。').rate,1.15)

 def test_q2_concern_background_is_consumed_but_old_joy_does_not_force_brightness(self):
  from core.utils.companion_affect import AffectPolicy
  p=AffectPolicy('xiaoyuan');epoch=self.memory.delivery_token()[0]
  self.c.intent.llm=NS(response_structured=lambda *a,**kw:None)
  self.c.config['companion_affect']=dict(enabled=True,companion_id='xiaoyuan',allowed_subject_scopes=['person'])
  state=dict(emotion='concern',intensity=.8,mood=0,reason='之前的交流',observed_at=time.time(),user_emotion='unknown')
  row=dict(state=state,epoch=epoch,source_turn='old',expires_at=time.time()+3600)
  runtime=NS(scope='person',policy=p,store=NS(ledger=self.ledger),_row=lambda:row)
  self.c._affect_turns={'turn':(runtime,dict(epoch=epoch),{})}
  self.assertEqual(self.delivery('那咱们接着聊。').source,'q2_context')
  self.assertEqual(self.delivery('哈哈，太好了！').style,'bright')
  state['emotion']='joy';self.assertEqual(self.delivery('那咱们接着聊。').style,'warm')

 def test_abort_or_forget_before_first_text_does_not_open_a_task(self):
  q=SpeechQueue(self.c,'继续。',dict(generation=1),'turn');q.put(message(SentenceType.FIRST))
  self.ledger.forget('person',[]);q.put(message(SentenceType.MIDDLE,'旧话'));q.put(message(SentenceType.LAST))
  self.assertTrue(self.p.tts_text_queue.empty())

 def test_empty_reply_does_not_leak_an_unfinished_first(self):
  q=SpeechQueue(self.c,'',dict(generation=1),'turn');q.put(message(SentenceType.FIRST));q.put(message(SentenceType.LAST))
  self.assertTrue(self.p.tts_text_queue.empty())

 def test_real_one_shot_wire_keeps_text_and_flushes_pcm_tail(self):
  pcm=(np.sin(np.arange(1464)*.17)*6000).astype(np.int16).tobytes()
  ws=WS([json.dumps({'header':{'event':'task-started'}}),pcm,json.dumps({'header':{'event':'task-finished'}})])
  async def connect(*a,**kw):return ws
  d=self.delivery('我在这儿听着。','说慢一点。')
  with patch.object(alibl.websockets,'connect',connect):packets=self.p.to_tts('我在这儿听着。',delivery=d)
  self.assertEqual(len(packets),2)
  params=ws.sent[0]['payload']['parameters'];self.assertEqual(params['instruction'],DIRECTIONS['calm']);self.assertEqual(params['rate'],.85)
  submitted=''.join(x['payload']['input'].get('text','') for x in ws.sent)
  self.assertEqual(submitted,'我在这儿听着。');self.assertEqual(self.p.voice,'longanlingxin_v3.1')
  self.assertEqual(len(self.p.opus_encoder.buffer),0)

 def test_plain_voice_synthesis_has_no_instruction_field(self):
  self.c.config['companion_speech']['mode']='plain';d=self.delivery()
  self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{})


class AsyncSpeechTests(SpeechFixture,unittest.IsolatedAsyncioTestCase):
 async def test_duplex_task_end_flushes_real_encoder_before_last_event(self):
  sid='turn';pcm=(np.sin(np.arange(1464)*.17)*5000).astype(np.int16).tobytes()
  ws=WS([json.dumps({'header':{'event':'task-started','task_id':sid}}),pcm,json.dumps({'header':{'event':'task-finished','task_id':sid}})])
  self.p.ws=ws;self.p.current_sentence_id=sid
  await self.p._start_monitor_tts_response()
  rows=list(self.p.tts_audio_queue.queue)
  self.assertEqual(sum(r[0]==SentenceType.MIDDLE for r in rows),2);self.assertEqual(rows[-1][0],SentenceType.LAST)
  self.assertEqual(len(self.p.opus_encoder.buffer),0)

 async def test_correction_word_prefix_finishes_but_interrupted_prefix_is_not_sent(self):
  self.p.correct_words={'北京':'北景'};self.p._words_by_first_char={'北':['北京']}
  ws=WS();self.p.ws=ws
  await self.p.text_to_speak('我想去北','turn');await self.p.finish_session('turn')
  self.assertEqual(''.join(v['payload']['input'].get('text','') for v in ws.sent),'我想去北')
  self.p._pending_prefix='北';self.c.client_abort=True;count=len(ws.sent)
  await self.p.finish_session('turn');self.assertEqual(len(ws.sent),count)

 async def test_start_waits_for_task_started_and_serializes_instruction(self):
  ws=WS([json.dumps({'header':{'event':'task-started','task_id':'turn'}})])
  async def ensure():self.p.ws=ws;return ws
  self.p._ensure_connection=ensure
  await self.p.start_session('turn',self.delivery())
  self.assertTrue(self.p._task_started.is_set());self.assertIn('instruction',ws.sent[0]['payload']['parameters'])

 async def test_cancel_unblocks_start_and_resets_old_owner(self):
  self.p._task_started=asyncio.Event();self.p.ws=WS();self.p.current_sentence_id='turn'
  await self.p.cancel_pending_audio()
  self.assertTrue(self.p._task_started.is_set());self.assertIsNone(self.p.current_sentence_id)

class SpeechWorkflowTests(unittest.IsolatedAsyncioTestCase):
 setUp=natural.NaturalTests.setUp;tearDown=natural.NaturalTests.tearDown
 chat=natural.NaturalTests.chat
 async def test_actual_a_and_b_attach_plan_without_changing_final_text(self):
  for direct in (False,True):
   c=q1.RoleWorkflowTests.connection(self);c.config['dialogue_quality']['strategy']='role_direct' if direct else 'legacy'
   c.tts.speech_capabilities=lambda:dict(provider='alibl_stream',model='qwen-audio-3.1-tts-flash',voice='longanlingxin_v3.1',instruction=True,rate=True)
   c.config['companion_speech']=dict(enabled=True,allowed_subject_scopes=[self.memory.scope])
   await self.chat(c,'你说慢一点。',dict(route='chat',input_origin='asr'))
   rows=list(c.tts.tts_text_queue.queue);self.assertEqual(rows[0].sentence_type,SentenceType.FIRST)
   self.assertTrue(all(r.delivery is rows[0].delivery for r in rows));self.assertEqual(rows[0].delivery.rate,.85)
   self.assertEqual(''.join(r.content_detail or '' for r in rows),c.dialogue.dialogue[-1].content)

 async def test_local_rollout_config_survives_manager_mode(self):
  from config import config_loader
  with patch.object(config_loader,'init_service'),patch.object(config_loader,'get_server_config',new=AsyncMock(return_value={'server':{'auth':{'enabled':True}}})):
   block=dict(enabled=True,allowed_subject_scopes=['synthetic'])
   cfg=await config_loader.get_config_from_api_async({'manager-api':{'url':'https://example.invalid'},'companion_speech':block})
  self.assertEqual(cfg['companion_speech'],block)

 async def test_validated_retry_after_forget_can_deliver_fresh_text(self):
  c=q1.RoleWorkflowTests.connection(self);c.config['dialogue_quality']['strategy']='legacy'
  c.tts.speech_capabilities=lambda:dict(provider='alibl_stream',model='qwen-audio-3.1-tts-flash',instruction=True,rate=True)
  c.config['companion_speech']=dict(enabled=True,allowed_subject_scopes=[self.memory.scope])
  calls=[]
  def compose(system,payload,schema,**kw):
   calls.append(1)
   if len(calls)==1:self.engine.store.forget(self.memory.scope,[])
   return json.dumps(dict(valid=True,reply='按当前这轮的信息聊。',reason='',news_numbers=[]))
  c.intent.llm.response_structured=compose
  await self.chat(c,'接着说。',dict(route='chat',input_origin='asr'))
  rows=list(c.tts.tts_text_queue.queue)
  self.assertEqual(len(calls),2);self.assertTrue(rows)
  self.assertEqual(rows[0].delivery.epoch,self.memory.delivery_token()[0])
  self.assertEqual(''.join(r.content_detail or '' for r in rows),'按当前这轮的信息聊。')
