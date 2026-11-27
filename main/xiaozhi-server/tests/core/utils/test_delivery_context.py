"""Delivery regressions: audit preservation, interruption, actual final context."""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import test_natural_response as natural
import test_recent_memory as recent
from core.utils.dialogue import Dialogue, Message
from core.utils.information_response import explicit_news_limit, delivery_tool_results, _compose_reply
from core.utils.runtime_capabilities import build_runtime_capabilities
from core.handle.abortHandle import handleAbortMessage


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_withdraws_only_current_assistant_and_preserves_audit(self):
        dialogue = Dialogue()
        dialogue.put(Message('system', '角色'))
        dialogue.put(Message('assistant', '已经说过', turn_id='old', delivery_status='audio_sent'))
        dialogue.put(Message('user', '讲个故事'))
        dialogue.put(Message('assistant', '还没听完的整段故事', turn_id='current'))
        conn = NS(sentence_id='current', dialogue=dialogue, session_id='session',
                  logger=NS(bind=lambda **k:NS(info=lambda *a:None)),
                  intent=NS(read_only=False), websocket=NS(send=AsyncMock()),
                  clear_queues=lambda:None, clearSpeakStatus=lambda:None)
        await handleAbortMessage(conn)
        dialogue.mark_delivery('current', 'audio_sent')
        self.assertEqual(dialogue.dialogue[-1].content, '还没听完的整段故事')
        self.assertEqual(dialogue.dialogue[-1].delivery_status, 'interrupted')
        messages=dialogue.get_llm_dialogue()
        self.assertNotIn('还没听完的整段故事', str(messages))
        self.assertIn('已经说过', str(messages))
        self.assertIn('讲个故事', str(messages))
        stop=json.loads(conn.websocket.send.call_args.args[0])
        self.assertEqual((stop['reason'],stop['turn_id']),('interrupted','current'))

    def test_news_count_does_not_confuse_ordinal_negative_or_quote(self):
        for value, count in [('只说一条新闻',1),('就听两条',2),('只播3条',3),('第二条呢',None),
                             ('不要只说一条',None),('他刚才说“只说一条”',None)]:
            self.assertEqual(explicit_news_limit(value),count,value)
        bundle=dict(topic='news',operation='search',status='ok',results=[{'title':str(i)} for i in range(3)])
        self.assertEqual(len(delivery_tool_results([bundle],'只说一条')[0]['results']),1)
        self.assertEqual(len(bundle['results']),3)
        self.assertEqual(len(delivery_tool_results([dict(bundle,operation='detail')],'只说一条')[0]['results']),3)

    def test_final_composer_obeys_count_and_retains_previous_user_constraints(self):
        captured=[]
        def compose(system,payload,schema,**kwargs):
            captured.append(json.loads(payload))
            return json.dumps(dict(valid=True,reply='第一条，标题甲。',reason='',news_numbers=[1]),ensure_ascii=False)
        conn=NS(intent=NS(llm=NS(response_structured=compose)))
        data={'current_user_utterance':'只说一条新闻','tool_results':[dict(topic='news',status='ok',operation='search',
            results=[dict(title=t,content='') for t in ['标题甲','标题乙','标题丙']])]}
        messages=[dict(role='system',content='角色'),dict(role='user',content='讲慢一点，别讲体育。'),
                  dict(role='user',content='只说一条新闻')]
        answer,_=_compose_reply(conn,messages,data,'')
        self.assertEqual(answer,'第一条，标题甲。')
        self.assertEqual(captured[0]['delivery_requirements']['news_numbers'],[1])
        self.assertEqual(captured[0]['delivery_requirements']['news_titles_in_original_order'],['标题甲'])
        self.assertIn('别讲体育',str(captured[0]['dialogue']))

    def test_capabilities_allowlist_sources_and_no_secret_settings(self):
        for origin in ('asr','user_text','automatic_wakeup','unknown','untrusted'):
            cap=build_runtime_capabilities({'ASR':{'secret':'dont-serialize'}}, input_origin=origin,read_only=True)
            self.assertNotIn('dont-serialize',str(cap))
            self.assertEqual(cap['input']['origin'],origin if origin!='untrusted' else 'unknown')
            self.assertEqual(cap['input']['dialect'],'unknown')
            self.assertEqual(cap['information_lookup']['side_effect_tools'],'unavailable')


class NaturalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = natural.NaturalTests.setUp
    tearDown = natural.NaturalTests.tearDown
    connection = natural.NaturalTests.connection
    chat = natural.NaturalTests.chat
    async def test_asr_false_capability_denial_is_regenerated(self):
        conn = self.connection(review=lambda data: dict(
            valid=True, reason='', reply=('这句话收到了，但我不能确定是哪一种方言。'
                if 'format_correction' in data else '我只能看到文字，听不到声音。')))
        await self.chat(conn, '我说的是什么方言', dict(route='chat', input_origin='asr'))
        self.assertEqual(len(conn.review_calls), 2)
        self.assertIn('本轮确实是语音识别输入', conn.review_calls[-1]['format_correction'])

    async def test_correct_negation_of_text_only_is_allowed(self):
        conn = self.connection(review=lambda data: dict(
            valid=True, reason='', reply='我不是只能看文字，您也可以用语音聊天。'))
        await self.chat(conn, '你只能看文字吗', dict(route='chat', input_origin='asr'))
        self.assertEqual(len(conn.review_calls), 1)

    async def test_final_model_receives_origin_full_capabilities_and_fresh_memory(self):
        conn=self.connection()
        conn.intent.read_only=True
        await self.chat(conn,'我说方言你听得懂吗',dict(route='chat',input_origin='asr'))
        cap=conn.review_calls[0]['current_turn_data']['runtime_capabilities']
        self.assertEqual(cap['input']['origin'],'asr')
        self.assertEqual(cap['information_lookup']['available_topics'],['news','weather','general'])
        self.assertIn('conversation_retention_policy',cap)
        self.assertIn('runtime_capabilities',str(conn.role_calls[0]))


class RecentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = recent.RecentTests.setUp
    tearDown = recent.RecentTests.tearDown
    enable = recent.RecentTests.enable
    checkpoint = recent.RecentTests.checkpoint
    connection = recent.RecentTests.connection

    async def test_abort_after_transport_stop_withdraws_recent_reply(self):
        from core.handle.sendAudioHandle import send_tts_message
        self.enable()
        ref = self.checkpoint('给点建议')
        self.memory.record_recent_reply(ref, '明天去公园。')
        conn = self.connection()
        conn.sentence_id = 's1'
        conn._recent_reply_delivery = ('s1', ref)
        conn._recent_audio_sentence = 's1'
        conn.clear_queues = lambda: None
        conn.clearSpeakStatus = lambda: None
        conn.websocket = NS(send=AsyncMock())
        with patch('core.handle.sendAudioHandle._wait_for_audio_completion', new=AsyncMock()):
            await send_tts_message(conn, 'stop')
        self.assertEqual(self.memory.recent_context('new')['turns'][0]['reply_status'], 'audio_sent')
        await handleAbortMessage(conn)
        turn = self.memory.recent_context('new')['turns'][0]
        self.assertEqual(turn['reply_status'], 'interrupted')
        self.assertEqual(turn['assistant'], '')
        self.assertFalse(self.memory.record_recent_reply(ref, status='audio_sent'))

    async def test_router_does_not_read_interrupted_audit_text(self):
        from core.utils.information_router import ReadOnlySession
        conn = self.connection()
        conn.dialogue.put(Message('assistant', '未播出的约定', turn_id='old', delivery_status='interrupted'))
        captured = []
        def structured(system, payload, schema, **kwargs):
            captured.append(json.loads(payload))
            raise RuntimeError('Stop before generating plan')
        conn.intent.llm.response_structured = structured
        with self.assertRaises(RuntimeError):
            await ReadOnlySession(conn).plan('刚才说了什么')
        self.assertNotIn('未播出的约定', str(captured[0]['history']))

    async def test_stale_transport_callback_cannot_send_new_turn_audio(self):
        from core.handle.sendAudioHandle import _start_background_sender, send_tts_message
        callbacks = []
        conn = self.connection()
        conn.sentence_id = 'new'
        conn.websocket = NS(send=AsyncMock())
        _start_background_sender(conn, NS(start_sending=callbacks.append), {'sentence_id': 'old'})
        with self.assertRaises(asyncio.CancelledError):
            await callbacks[0](b'old-opus')
        await send_tts_message(conn, 'sentence_start', '旧句子', turn_id='old')
        conn.websocket.send.assert_not_called()
