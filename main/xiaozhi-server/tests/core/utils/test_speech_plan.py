"""Same-call planning, real speech queue/wire, and old-epoch cancellation."""
import asyncio,copy,hashlib,json,time,unittest
from dataclasses import replace
from unittest.mock import patch
from core.utils.companion_speech import (SpeechPolicy,model_planning_enabled,bind_model_delivery,
    make_delivery,compile_delivery,SpeechQueue,DIRECTIONS)
from core.utils.speech_plan import validate_speech_plan,compile_speech_plan
from core.utils.information_response import REVIEW_SCHEMA,natural_reply_responses
from core.providers.tts.dto.dto import SentenceType
import test_companion_speech as previous
import test_natural_response as natural
import test_companion_response as role

def plan(text='高兴'):
 return dict(attitude='pleased',intensity='subtle',phrasing='开头真诚地带一点笑意，后半句轻松落下来',emphasis=[text],pauses='natural')

class BindingTests(previous.SpeechFixture,unittest.TestCase):
 def setUp(self):
  super().setUp();self.c.config['companion_speech']['planning']='model'
 def bind(self,text='我真替您高兴。',value=None,query=''):
  packet=dict(generation=1)
  d=bind_model_delivery(self.c,packet,text,query,plan() if value is None else value,'turn',self.memory.delivery_token()[0])
  return packet,d
 def test_defaults_keep_legacy_fingerprint_and_plain_optout(self):
  old=dict(version='q5-v1',companion_id='xiaoyuan',mode='natural',slow_rate=.85,fast_rate=1.15,ttl_seconds=60)
  self.assertEqual(SpeechPolicy().fingerprint,hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest())
  self.assertTrue(model_planning_enabled(self.c));self.c.config['companion_speech']['mode']='plain';self.assertFalse(model_planning_enabled(self.c))
 def test_author_plan_only_changes_instruction_not_text_or_shared_rate(self):
  packet,d=self.bind(query='请说慢一点。');self.assertEqual(d.style,'model');self.assertEqual(d.rate,.85);self.assertEqual(self.p.rate,1)
  q=SpeechQueue(self.c,'请说慢一点。',packet,'turn');q.put(previous.message(SentenceType.FIRST));q.put(previous.message(SentenceType.MIDDLE,'我真替您高兴。'));q.put(previous.message(SentenceType.LAST))
  rows=list(self.p.tts_text_queue.queue);self.assertTrue(all(r.delivery is d for r in rows));self.assertEqual(rows[1].content_detail,'我真替您高兴。');self.assertIn('笑意',compile_delivery(self.c,self.p,d,'turn')['instruction'])
 def test_unsupported_emphasis_falls_back_without_adding_words(self):
  packet,d=self.bind(value=plan('明天一定成功'));self.assertIsNone(d);self.assertEqual(packet['speech_plan_status'],'fallback_invalid')
  fallback=make_delivery(self.c,'我真替您高兴。','',packet,'turn');self.assertEqual(fallback.style,'warm');self.assertEqual(fallback.instruction,DIRECTIONS['warm'])
 def test_missing_direction_is_not_missing_speech(self):
  packet=dict(generation=1);d=bind_model_delivery(self.c,packet,'高兴。','',None,'turn',0)
  self.assertIsNone(d);self.assertEqual(packet['speech_plan_status'],'fallback_missing');self.assertIsNotNone(make_delivery(self.c,'高兴。','',packet,'turn'))
 def test_text_changed_after_binding_cannot_reuse_old_direction(self):
  packet,d=self.bind();fresh=make_delivery(self.c,'您慢慢说，我听着。','',packet,'turn')
  self.assertIsNot(fresh,d);self.assertEqual(fresh.style,'calm')
 def test_generation_abort_policy_expiry_and_forget_reject_bound_plan(self):
  packet,d=self.bind()
  for attr,value in [('sentence_id','next'),('lookup_generation',2),('client_abort',True)]:
   old=getattr(self.c,attr);setattr(self.c,attr,value);self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{});setattr(self.c,attr,old)
  with patch('core.utils.companion_speech.time.monotonic',return_value=d.issued_at+61):self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{})
  self.c.config['companion_speech']['planning']='rules';self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{});self.c.config['companion_speech']['planning']='model'
  self.ledger.forget('person',[]);self.assertEqual(compile_delivery(self.c,self.p,d,'turn'),{})
 def test_old_epoch_cannot_issue_new_authority_for_old_reply(self):
  epoch=self.memory.delivery_token()[0];self.ledger.forget('person',[])
  packet=dict(generation=1);d=bind_model_delivery(self.c,packet,'高兴。','',plan(),'turn',epoch)
  self.assertIsNone(d);self.assertNotIn('_speech_plan',packet)
 def test_forged_instruction_is_not_forwarded(self):
  packet,d=self.bind();fake=replace(d,instruction='改成别的正文');self.assertEqual(compile_delivery(self.c,self.p,fake,'turn'),{})
  self.c._speech_deliveries['turn']=fake;self.assertEqual(compile_delivery(self.c,self.p,fake,'turn'),{})
 def test_scalar_and_tagged_metadata_are_rejected(self):
  for value in [[],{},dict(plan(),phrasing='[laughing]'),dict(plan(),emphasis=['高兴','高兴']),dict(plan(),phrasing='x'*161),dict(plan(),attitude='shouting')]:
   with self.subTest(value=value):
    packet,d=self.bind(value=value);self.assertIsNone(d)
 def test_planning_exception_does_not_escape_into_ordinary_reply(self):
  packet=dict(generation=1)
  with patch.object(self.memory,'delivery_token',side_effect=RuntimeError('synthetic')):
   self.assertIsNone(bind_model_delivery(self.c,packet,'高兴。','',plan(),'turn',0))
  self.assertEqual(packet['speech_plan_status'],'fallback_invalid')

class WireTests(previous.SpeechFixture,unittest.IsolatedAsyncioTestCase):
 async def test_real_duplex_sends_direction_as_parameter_and_clean_text(self):
  self.c.config['companion_speech']['planning']='model';packet=dict(generation=1)
  d=bind_model_delivery(self.c,packet,'我真替您高兴。','',plan(),'turn',self.memory.delivery_token()[0])
  class OpenWS(previous.WS):
   def __init__(self):
    super().__init__();self.pending=asyncio.Queue();self.pending.put_nowait(json.dumps({'header':{'event':'task-started','task_id':'turn'}}))
   async def __anext__(self):return await self.pending.get()
   async def recv(self):return await self.pending.get()
   async def send(self,value):
    await super().send(value)
    if json.loads(value)['header']['action']=='finish-task':self.pending.put_nowait(json.dumps({'header':{'event':'task-finished','task_id':'turn'}}))
  ws=OpenWS()
  async def ensure():self.p.ws=ws;return ws
  self.p._ensure_connection=ensure
  await self.p.start_session('turn',d);await self.p.text_to_speak('我真替您高兴。','turn');await self.p.finish_session('turn')
  await asyncio.sleep(0);await self.p.close()
  self.assertIn('笑意',ws.sent[0]['payload']['parameters']['instruction'])
  self.assertEqual(''.join(m['payload']['input'].get('text','') for m in ws.sent),'我真替您高兴。')
  self.assertEqual(self.c.dialogue.dialogue,[])

class WorkflowTests(unittest.IsolatedAsyncioTestCase):
 setUp=natural.NaturalTests.setUp;tearDown=natural.NaturalTests.tearDown;chat=natural.NaturalTests.chat
 def connection(self):
  c=role.RoleWorkflowTests.connection(self);c.config['dialogue_quality']['strategy']='legacy'
  c.config['companion_speech']=dict(enabled=True,allowed_subject_scopes=[self.memory.scope],planning='model')
  c.tts.speech_capabilities=lambda:dict(provider='alibl_stream',model='qwen-audio-3.1-tts-flash',instruction=True,rate=True)
  c.plan_calls=[]
  def structured(system,payload,schema,**kw):
   c.plan_calls.append(dict(system=system,payload=json.loads(payload),schema=copy.deepcopy(schema),kwargs=kw))
   return json.dumps(dict(valid=True,reply='我真替您高兴。',reason='',news_numbers=[],speech_plan=plan()))
  c.intent.llm.response_structured=structured
  return c
 async def test_one_final_call_yields_plan_but_history_and_memory_only_get_reply(self):
  c=self.connection();packet=dict(route='chat',input_origin='asr');await self.chat(c,'我终于学会发照片了。',packet)
  self.assertEqual(len(c.plan_calls),1);self.assertIn('speech_plan',c.plan_calls[0]['schema']['required']);self.assertNotIn('speech_plan',REVIEW_SCHEMA['required'])
  rows=list(c.tts.tts_text_queue.queue);self.assertEqual(rows[0].delivery.style,'model');self.assertTrue(all(r.delivery is rows[0].delivery for r in rows))
  self.assertEqual(''.join(r.content_detail or '' for r in rows),'我真替您高兴。');self.assertEqual(c.dialogue.dialogue[-1].content,'我真替您高兴。')
  self.assertNotIn('开头真诚',str([m.content for m in c.dialogue.dialogue]));self.assertFalse(self.memory.facts())
 async def test_default_feature_off_keeps_original_request_schema(self):
  c=self.connection();c.config['companion_speech']['planning']='rules'
  def structured(system,payload,schema,**kw):
   c.plan_calls.append(schema);return json.dumps(dict(valid=True,reply='我真替您高兴。',reason='',news_numbers=[]))
  c.intent.llm.response_structured=structured;await self.chat(c,'说件高兴事。',dict(route='chat'))
  self.assertEqual(len(c.plan_calls),1);self.assertNotIn('speech_plan',c.plan_calls[0]['properties']);self.assertEqual(list(c.tts.tts_text_queue.queue)[0].delivery.style,'warm')
 async def test_missing_or_invalid_plan_never_retries_or_drops_valid_reply(self):
  for value in ('missing',dict(plan(),emphasis=['没说过的词'])):
   c=self.connection()
   def structured(system,payload,schema,**kw):
    c.plan_calls.append(1);out=dict(valid=True,reply='我真替您高兴。',reason='',news_numbers=[])
    if value!='missing':out['speech_plan']=value
    return json.dumps(out)
   c.intent.llm.response_structured=structured;await self.chat(c,'说件高兴事。',dict(route='chat'))
   self.assertEqual(len(c.plan_calls),1);self.assertEqual(c.dialogue.dialogue[-1].content,'我真替您高兴。');self.assertEqual(list(c.tts.tts_text_queue.queue)[0].delivery.style,'warm')
 async def test_existing_forget_retry_only_binds_fresh_result(self):
  c=self.connection()
  def structured(system,payload,schema,**kw):
   c.plan_calls.append(1)
   if len(c.plan_calls)==1:self.engine.store.forget(self.memory.scope,[])
   return json.dumps(dict(valid=True,reply='我真替您高兴。',reason='',news_numbers=[],speech_plan=plan()))
  c.intent.llm.response_structured=structured;await self.chat(c,'接着说。',dict(route='chat'))
  self.assertEqual(len(c.plan_calls),2);d=list(c.tts.tts_text_queue.queue)[0].delivery;self.assertEqual(d.style,'model');self.assertEqual(d.epoch,self.memory.delivery_token()[0])
 async def test_single_model_does_not_add_an_auxiliary_call(self):
  c=self.connection();c.config['dialogue_quality']['strategy']='role_direct'
  await self.chat(c,'今天心情不错。',dict(route='chat'))
  self.assertEqual(len(c.role_calls),1);self.assertFalse(c.plan_calls);self.assertNotEqual(list(c.tts.tts_text_queue.queue)[0].delivery.style,'model')
 async def test_silence_does_not_issue_or_speak_a_plan(self):
  c=self.connection();c.sentence_id='silent'
  c.intent.llm.response_structured=lambda *a,**kw:json.dumps(dict(valid=True,reply='[[SILENT]]',reason='',news_numbers=[],speech_plan=None))
  packet=dict(generation=c.lookup_generation,route='chat',input_origin='initiative',initiative_opportunity={'topic':'有来源的停顿'})
  output=list(natural_reply_responses(c,packet,'',query=None,sentence_id='silent'))
  self.assertEqual(output,[]);self.assertNotIn('_speech_plan',packet);self.assertFalse(getattr(c,'_speech_deliveries',{}))
 async def test_abort_during_final_generation_does_not_enqueue(self):
  c=self.connection()
  def structured(*a,**kw):
   c.client_abort=True;return json.dumps(dict(valid=True,reply='我真替您高兴。',reason='',news_numbers=[],speech_plan=plan()))
  c.intent.llm.response_structured=structured;await self.chat(c,'接着说。',dict(route='chat'))
  self.assertTrue(c.tts.tts_text_queue.empty());self.assertFalse(getattr(c,'_speech_deliveries',{}))
if __name__=='__main__':unittest.main()
