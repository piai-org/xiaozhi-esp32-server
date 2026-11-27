"""Q1 role authors use native context and the existing read/delivery contracts."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from core.utils.companion_response import (
    ReplyPolicy, build_reply_messages, finalize_role_reply,
    generate_role_reply, select_reply_policy,
)
from core.utils.dialogue import Dialogue, Message
import test_natural_response as natural


def data(utterance='陪我聊聊。', **kw):
    return dict(current_user_utterance=utterance,
                core_user_memory={'status': 'absent', 'facts': []},
                personal_memory_read=None, related_user_statements=[],
                recent_conversations={'status': 'not_requested', 'turns': []},
                tool_results=[], runtime_capabilities={'input': {'origin': 'asr'}}, **kw)


def fixture():
    conn = NS(config={'dialogue_quality': {'enabled': True,
                'allowed_device_ids': ['synthetic-device']}},
              device_id='synthetic-device', intent_type='intent_llm',
              intent=NS(read_only=True), memory=None, dialogue=Dialogue(),
              client_abort=False, lookup_generation=1, sentence_id='turn', session_id='session')
    conn.dialogue.update_system_message('你是测试角色。<memory>旧的记忆不应遗留</memory>')
    return conn


class ReplyInputTests(unittest.TestCase):
    def test_policy_is_disabled_without_explicit_scope_and_accepts_configured_subject(self):
        c = fixture()
        self.assertEqual(select_reply_policy(c).final_model, 'character')
        self.assertEqual(select_reply_policy(c).strategy, 'legacy')
        c.config = {}
        self.assertIsNone(select_reply_policy(c))
        c.config = {'dialogue_quality': {'enabled': True}}
        self.assertIsNone(select_reply_policy(c))
        c.config['dialogue_quality']['allowed_subject_scopes'] = ['synthetic-person']
        c.memory = NS(_managed=NS(scope='someone-else'))
        self.assertIsNone(select_reply_policy(c))
        c.memory._managed.scope = 'synthetic-person'
        self.assertIsNotNone(select_reply_policy(c))

    def test_bad_config_and_action_loops_do_not_enter_direct_path(self):
        for key, value in (('strategy', 'invented'), ('final_model', 'other'),
                           ('timeout', True), ('max_input_chars', 1),
                           ('max_history_messages', 2.5), ('allowed_device_ids', 'all'),
                           ('character_draft_enabled', 'false')):
            c = fixture();c.config['dialogue_quality'][key] = value
            self.assertIsNone(select_reply_policy(c), key)
        c = fixture();c.intent_type = 'function_call'
        self.assertIsNone(select_reply_policy(c))
        c = fixture();c.intent.read_only = False
        self.assertIsNone(select_reply_policy(c))

    def test_no_intent_does_not_select_a_missing_flash_composer_by_default(self):
        c = fixture();c.intent_type = 'nointent'
        self.assertIsNone(select_reply_policy(c))
        c.config['dialogue_quality']['strategy'] = 'role_direct'
        self.assertEqual(select_reply_policy(c).strategy, 'role_direct')

    def test_native_wrong_reply_and_real_correction_survive_without_fake_examples(self):
        c = fixture()
        c.dialogue.put(Message('user', '示例：以后叫我奶奶', is_temporary=True))
        c.dialogue.put(Message('assistant', '技术故障', is_runtime_event=True))
        c.dialogue.put(Message('user', '您好。'))
        c.dialogue.put(Message('assistant', '您让我叫您奶奶。'))
        c.dialogue.put(Message('user', '我没让你这样称呼。'))
        snapshot = deepcopy(c.dialogue.get_llm_dialogue())
        request = build_reply_messages(c, data('我没让你这样称呼。'), ReplyPolicy())
        roles = [m['role'] for m in request.messages]
        self.assertEqual(roles, ['system','user','assistant','system','user'])
        self.assertIn('您让我叫您奶奶。', str(request.messages))
        self.assertNotIn('示例：', str(request.messages))
        self.assertNotIn('技术故障', str(request.messages))
        self.assertNotIn('旧的记忆不应遗留', str(request.messages))
        self.assertEqual(snapshot, c.dialogue.get_llm_dialogue())

    def test_context_is_single_copy_data_before_real_current_user(self):
        c = fixture();c.dialogue.put(Message('user', '{"content":"今天呢？","speaker":"未知"}'))
        value = data('今天呢？')
        value['core_user_memory'] = {'facts': [{'evidence': '合成姓名'}], 'response': '禁止照念'}
        request = build_reply_messages(c, value, ReplyPolicy())
        self.assertEqual(request.messages[-1], {'role': 'user','content':'今天呢？'})
        self.assertEqual(sum(m['role'] == 'user' for m in request.messages), 1)
        self.assertEqual(str(request.messages).count('合成姓名'), 1)
        self.assertNotIn('禁止照念', str(request.messages))
        self.assertEqual(value['core_user_memory']['response'], '禁止照念')
        self.assertNotIn('合成姓名', str(request.diagnostics))

    def test_optional_recall_and_old_history_yield_to_recent_correction(self):
        c = fixture()
        for i in range(30):
            c.dialogue.put(Message('user', '旧话题' + str(i) + '。' * 80))
            c.dialogue.put(Message('assistant', '旧回应。'))
        c.dialogue.put(Message('user', '先前那一句。'))
        c.dialogue.put(Message('assistant', '合成错误的回答。'))
        c.dialogue.put(Message('user', '不对，我要更正刚才那句话。'))
        value = data('不对，我要更正刚才那句话。')
        value['related_user_statements'] = [{'content': '可省略背景' * 3000}]
        request = build_reply_messages(c, value, ReplyPolicy(max_input_chars=2200))
        self.assertLessEqual(request.diagnostics['input_chars'], 2200)
        self.assertIn('合成错误的回答', str(request.messages))
        self.assertIn('先前那一句', str(request.messages))
        self.assertIn('related_user_statements', request.diagnostics['omitted_blocks'])

    def test_protected_current_user_and_fact_are_not_silently_cut(self):
        c = fixture();c.dialogue.put(Message('user', '不是这个人。'))
        value = data('不是这个人。')
        value['personal_memory_read'] = {'evidence': [{'evidence': '重要事实' * 2000}]}
        with self.assertRaises(ValueError):
            build_reply_messages(c, value, ReplyPolicy(max_input_chars=1800))

    def test_news_selection_uses_existing_user_count_without_rewriting(self):
        c = fixture();c.dialogue.put(Message('user', '只要一条新闻。'))
        value = data('只要一条新闻。')
        value['tool_results'] = [{'topic':'news','status':'ok','results':[
            {'title':'保留的第一条'}, {'title':'不该交付的第二条'}]}]
        request = build_reply_messages(c, value, ReplyPolicy())
        self.assertIn('保留的第一条', str(request.messages))
        self.assertNotIn('不该交付的第二条', str(request.messages))
        self.assertEqual(len(value['tool_results'][0]['results']), 2)

    def test_b_and_c_have_the_same_messages_and_one_plain_author_call(self):
        c = fixture();c.dialogue.put(Message('user', '陪我聊聊。'))
        calls = []
        def generate(*args, **kw):
            calls.append((deepcopy(args[1]), kw));return iter(['自然的一句话。'])
        c.llm = NS(response=generate);c.intent.llm = NS(response=generate)
        for author in ('character', 'intent'):
            p = ReplyPolicy(final_model=author)
            request = build_reply_messages(c, data(), p)
            self.assertEqual(generate_role_reply(c, request, p, {'generation':1}, 'turn'), '自然的一句话。')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_cancel_closes_stream_and_returns_no_partial_reply(self):
        c = fixture();c.dialogue.put(Message('user', '陪我聊聊。'))
        closed = []
        def stream():
            try:
                yield '第一部分'
                c.sentence_id = 'new-turn'
                yield '不应发送'
            finally:
                closed.append(True)
        c.llm = NS(response=lambda *a,**kw: stream())
        request = build_reply_messages(c, data(), ReplyPolicy())
        self.assertIsNone(generate_role_reply(c, request, ReplyPolicy(), {'generation':1}, 'turn'))
        self.assertEqual(closed, [True])

    def test_json_protocol_is_not_treated_as_spoken_role_text(self):
        c = fixture();c.dialogue.put(Message('user', '陪我聊聊。'))
        c.llm = NS(response=lambda *a,**kw: iter(['{"reply":"不要照念对象"}']))
        with self.assertRaises(ValueError):
            generate_role_reply(c, build_reply_messages(c, data(), ReplyPolicy()), ReplyPolicy(), {'generation':1}, 'turn')

    def test_finalize_once_records_generated_not_heard_and_no_fault_event(self):
        c = fixture();c.tts = NS(store_tts_text=lambda *a: None)
        event = finalize_role_reply(c, '我在呢。', 'turn', {'generation':1}, ReplyPolicy())
        self.assertEqual(event['delivery_status'], 'generated')
        self.assertEqual(len(c.companion_reply_events), 1)
        self.assertIsNone(finalize_role_reply(c, '重复', 'turn', {'generation':1}, ReplyPolicy()))
        c.sentence_id = 'fault'
        self.assertIsNone(finalize_role_reply(c, '技术故障', 'fault', {'generation':1,'runtime_event':True}, ReplyPolicy()))
        self.assertEqual(len(c.companion_reply_events), 1)
        self.assertTrue(c.dialogue.dialogue[-1].is_runtime_event)


class RoleWorkflowTests(unittest.IsolatedAsyncioTestCase):
    setUp = natural.NaturalTests.setUp
    tearDown = natural.NaturalTests.tearDown
    add = natural.NaturalTests.add
    chat = natural.NaturalTests.chat
    texts = staticmethod(natural.NaturalTests.texts)

    def connection(self, reply=None, author='character'):
        c = natural.NaturalTests.connection(self)
        c.device_id = 'synthetic-device';c.intent.read_only = True
        c.config['dialogue_quality'] = {'enabled': True, 'strategy':'role_direct',
            'final_model': author, 'allowed_device_ids':['synthetic-device']}
        c.intent_calls = []
        def plain(session, messages, **kw):
            c.intent_calls.append(deepcopy(messages))
            return reply(messages) if reply else iter(['这是Flash自己说的话。'])
        c.intent.llm.response = plain
        if reply:
            def role(session, messages, **kw):
                c.role_calls.append(deepcopy(messages));return reply(messages)
            c.llm.response = role
        return c

    async def test_chat_uses_role_once_and_never_the_organizer(self):
        c = self.connection(lambda m: iter(['听着您这句话，我也想笑呢。']))
        await self.chat(c, '今天跟邻居聊得挺开心。', {'route':'chat'})
        self.assertEqual(len(c.role_calls), 1);self.assertFalse(c.review_calls)
        self.assertIn('也想笑', self.texts(c))
        self.assertEqual(len(c.companion_reply_events), 1)

    async def test_identity_is_read_then_spoken_by_role_without_literal_reply(self):
        self.add('以后叫我周叔。')
        c = self.connection(lambda m: iter(['周叔，我在这儿。']))
        await self.chat(c, '喊我一声。', {'route':'memory_query','memory_query':'preferred_address'})
        self.assertEqual(self.texts(c), '周叔，我在这儿。')
        self.assertIn('周叔', str(c.role_calls[0]))
        self.assertEqual(len(c.role_calls), 1);self.assertFalse(c.review_calls)

    async def test_mixed_tool_and_memory_have_one_role_utterance(self):
        self.add('以后叫我周叔。')
        c = self.connection(lambda m: iter(['周叔，预报杭州可能下雨，咱出门带把伞吧。']))
        await self.chat(c, '喊我一声，杭州天气呢？', {'route':'lookup',
            'memory_query':'preferred_address','evidence':[{'topic':'weather','status':'ok',
                'location':'杭州','results':[{'content':'预计有雨'}]}]})
        self.assertEqual(len(c.role_calls), 1);self.assertFalse(c.review_calls)
        self.assertIn('预计有雨', str(c.role_calls[0]))
        self.assertEqual(sum(m.role == 'assistant' for m in c.dialogue.dialogue), 1)

    async def test_c_uses_intent_plain_generation_without_character_draft_or_review(self):
        c = self.connection(author='intent')
        await self.chat(c, '您好。', {'route':'chat'})
        self.assertFalse(c.role_calls);self.assertFalse(c.review_calls)
        self.assertEqual(len(c.intent_calls), 1)
        self.assertIn('Flash', self.texts(c))

    async def test_a_explicit_legacy_and_out_of_scope_keep_existing_workflow(self):
        for outside in (False, True):
            c = self.connection()
            if outside:
                c.device_id = 'someone-else'
            else:
                c.config['dialogue_quality']['strategy'] = 'legacy'
            await self.chat(c, '您好。', {'route':'chat'})
            self.assertEqual(len(c.review_calls), 1)
            self.assertFalse(hasattr(c, 'companion_reply_events'))

    async def test_direct_path_is_independent_of_memory_natural_switch(self):
        c = self.connection();c.config['Memory']['Natural']['natural_response_enabled'] = False
        await self.chat(c, '陪我聊聊。', {'route':'chat'})
        self.assertEqual(len(c.role_calls), 1);self.assertFalse(c.review_calls)

    async def test_scoped_legacy_draft_switch_changes_only_the_optional_draft(self):
        for enabled in (True, False):
            c = self.connection(lambda messages: iter(['这是合成角色草稿。']))
            c.config['dialogue_quality'].update(strategy='legacy', character_draft_enabled=enabled)
            await self.chat(c, '今天跟邻居聊得挺开心。', {'route':'chat'})
            self.assertEqual(len(c.role_calls), 1 if enabled else 0)
            self.assertEqual(len(c.review_calls), 1)
            self.assertEqual(c.review_calls[0]['draft'], '这是合成角色草稿。' if enabled else '')

    async def test_out_of_scope_draft_disable_keeps_the_original_draft(self):
        c = self.connection();c.device_id = 'outside'
        c.config['dialogue_quality'].update(strategy='legacy', character_draft_enabled=False)
        await self.chat(c, '今天跟邻居聊得挺开心。', {'route':'chat'})
        self.assertEqual(len(c.role_calls), 1)
        self.assertEqual(len(c.review_calls), 1)

    async def test_router_keeps_real_operation_receipt_when_old_memory_switch_is_off(self):
        from core.utils.information_router import ReadOnlySession
        c = self.connection();c.config['Memory']['Natural']['natural_response_enabled'] = False
        c.memory.control = AsyncMock(return_value={'response':'不能照念这句旧文本',
            'operation':{'kind':'forget','status':'completed','scope':'selected'}})
        await ReadOnlySession(c).run('忘掉刚才那条偏好。',1)
        packet = c.calls[-1][-1]
        self.assertEqual(packet['operation']['status'],'completed')
        self.assertNotIn('direct_response',packet)

    async def test_router_retains_semantic_history_fields_with_q1_only(self):
        from core.utils.information_router import ReadOnlySession
        c = self.connection();c.config['Memory']['Natural']['natural_response_enabled'] = False
        def structured(prompt,payload,schema,**kw):
            self.assertIn('user_fact_statement',schema['required'])
            self.assertIn('related_context_needed',schema['required'])
            return json.dumps({'route':'chat','requests':[],'memory_query':None,'clarification_slot':None,
                               'user_fact_statement':False,'related_context_needed':True})
        c.intent.llm.response_structured = structured
        plan = await ReadOnlySession(c).plan('接着上次聊聊。')
        self.assertTrue(plan['_related_context_needed'])

    async def test_changed_core_is_reread_before_output(self):
        self.add('以后叫我周叔。');n = 0
        def generate(messages):
            nonlocal n
            n += 1
            if n == 1:
                self.add('以后改叫我周老师。')
                return iter(['不应播出的旧称呼：周叔。'])
            self.assertIn('周老师', str(messages))
            return iter(['周老师，我改过来啦。'])
        c = self.connection(generate)
        await self.chat(c, '喊我一声。', {'route':'chat'})
        self.assertEqual(n, 2);self.assertNotIn('周叔', self.texts(c))
        self.assertFalse(c.review_calls)

    async def test_cancel_during_author_does_not_persist_or_publish_partial_reply(self):
        c = self.connection()
        def generate(messages):
            c.lookup_generation = 2
            return iter(['不应该播出。'])
        c.llm.response = lambda *a,**kw: generate(a[1])
        await self.chat(c, '陪我聊聊。', {'route':'chat'})
        self.assertEqual(self.texts(c), '')
        self.assertFalse(any(m.role == 'assistant' for m in c.dialogue.dialogue))
        self.assertFalse(hasattr(c, 'companion_reply_events'))

    async def test_failure_does_not_fall_back_to_other_model_or_stored_biography(self):
        self.add('以后叫我周叔。')
        def fail(messages):
            raise TimeoutError('synthetic')
        c = self.connection(fail)
        await self.chat(c, '我叫什么？', {'route':'memory_query','memory_query':'identity'})
        self.assertNotIn('周叔', self.texts(c));self.assertIn('没说顺', self.texts(c))
        self.assertFalse(c.review_calls);self.assertFalse(c.intent_calls)
        self.assertTrue(c.dialogue.dialogue[-1].is_runtime_event)
        self.assertFalse(hasattr(c, 'companion_reply_events'))

    async def test_unavailable_memory_and_tool_are_data_for_role_not_direct_response(self):
        c = self.connection(lambda m: iter(['这次天气没查到，咱先聊您刚才说的。']))
        self.memory.delivery_token = lambda *a: (_ for _ in ()).throw(OSError('synthetic'))
        await self.chat(c, '今天外面天气呢？', {'route':'lookup','direct_response':'固定台词不要读',
            'evidence':[{'topic':'weather','status':'error','error_code':'unavailable','results':[]}]})
        messages = str(c.role_calls[0])
        self.assertIn('unavailable', messages)
        self.assertNotIn('固定台词不要读', messages)
        self.assertEqual(len(c.role_calls), 1)

    async def test_generated_recent_reply_remains_queued_until_playback(self):
        c = self.connection(lambda m: iter(['先说这一句。']))
        saved = []
        self.memory.record_recent_reply = lambda ref,text: saved.append((ref,text)) or True
        reference = {'id':'synthetic-turn','epoch':0}
        await self.chat(c, '陪我聊聊。', {'route':'chat','recent_turn':reference})
        self.assertEqual(saved, [(reference, '先说这一句。')])
        self.assertEqual(c._recent_reply_delivery[1], reference)
        self.assertEqual(c.companion_reply_events[-1]['delivery_status'], 'generated')


class QualityConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_mode_preserves_scoped_local_override_and_explicit_disable(self):
        from config import config_loader
        for block in ({'enabled':True,'allowed_device_ids':['synthetic-device']}, {'enabled':False}):
            remote = {'server': {'auth': {'enabled':True}}, 'dialogue_quality': {'enabled':True}}
            local = {'manager-api': {'url':'https://example.invalid','secret':'SYNTHETIC'},
                     'dialogue_quality':block}
            with patch.object(config_loader,'init_service'), patch.object(
                    config_loader,'get_server_config',new=AsyncMock(return_value=remote)):
                result = await config_loader.get_config_from_api_async(local)
            self.assertEqual(result['dialogue_quality'],block)
            result['dialogue_quality']['changed'] = True
            self.assertNotIn('changed',block)

    async def test_manager_mode_without_local_key_keeps_existing_server_config(self):
        from config import config_loader
        remote = {'server': {'auth': {'enabled':True}}}
        with patch.object(config_loader,'init_service'), patch.object(
                config_loader,'get_server_config',new=AsyncMock(return_value=remote)):
            result = await config_loader.get_config_from_api_async({'manager-api':{'url':'https://example.invalid'}})
        self.assertNotIn('dialogue_quality',result)


if __name__ == '__main__':
    unittest.main()
