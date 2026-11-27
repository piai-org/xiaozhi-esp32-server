"""Per-turn author switching with the real chat/memory delivery contracts."""
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

import test_companion_response as role
import test_natural_response as natural
from core.utils.companion_response import select_reply_policy
from core.providers.llm.openai.openai import LLMProvider
from core.utils.information_router import ReadOnlySession


class HybridPolicyTests(unittest.TestCase):
    def connection(self):
        c = role.fixture()
        c.config['dialogue_quality']['strategy'] = 'hybrid'
        return c

    def test_background_memories_and_fact_statements_keep_role_author(self):
        c = self.connection()
        for packet in ({'route': 'chat'}, {'route': 'chat', 'related_context_needed': True},
                       {'route': 'chat', 'memory_job': 'queued'},
                       {'route': 'chat', 'recent_context_needed': True},
                       {'route': 'grounded', 'conversation_event': {'kind': 'greeting_or_source_inquiry'}},
                       {'route': 'chat', 'initiative_opportunity': {'topic': '散步'}}):
            with self.subTest(packet=packet):
                self.assertEqual(select_reply_policy(c, packet).strategy, 'role_direct')

    def test_reads_evidence_and_control_receipts_use_composer(self):
        c = self.connection()
        for packet in ({'route': 'memory_query'}, {'route': 'memory'}, {'route': 'lookup'},
                       {'route': 'grounded'}, {'route': 'clarify'}, {'route': 'cancel'},
                       {'route': 'unsupported'}, {'route': 'error'},
                       {'route': 'chat', 'memory_query': 'name'},
                       {'route': 'chat', 'assistant_history_needed': True},
                       {'route': 'chat', 'evidence': [{'status': 'unavailable'}]},
                       {'route': 'grounded', 'conversation_event': {'kind': 'greeting'},
                        'operation': {'status': 'unconfirmed'}}):
            with self.subTest(packet=packet):
                self.assertEqual(select_reply_policy(c, packet).strategy, 'legacy')

    def test_decisions_are_immutable_and_do_not_stick_or_mutate_config(self):
        c = self.connection(); before = deepcopy(c.config)
        chat = select_reply_policy(c, {'route': 'chat'})
        read = select_reply_policy(c, {'route': 'lookup'})
        after = select_reply_policy(c, {'route': 'chat'})
        self.assertEqual(chat, after)
        self.assertEqual(read.strategy, 'legacy')
        self.assertEqual((chat.temperature, read.draft_temperature, read.composer_temperature), (.4, .7, .4))
        self.assertEqual(before, c.config)

    def test_scope_validation_and_rollback_preserve_existing_modes(self):
        c = self.connection(); c.device_id = 'outside'
        self.assertIsNone(select_reply_policy(c, {'route': 'chat'}))
        c = self.connection(); c.config['dialogue_quality']['strategy'] = 'legacy'
        p = select_reply_policy(c, {'route': 'chat'})
        self.assertEqual(p.strategy, 'legacy'); self.assertIsNone(p.composer_temperature)
        for value in (True, '0.4', -1, 3, float('nan')):
            c = self.connection(); c.config['dialogue_quality']['temperature'] = value
            self.assertIsNone(select_reply_policy(c), value)

    def test_provider_override_does_not_change_next_structured_call(self):
        p = LLMProvider.__new__(LLMProvider)
        p.model_name = 'synthetic'; p.max_tokens = 512
        p._apply_thinking_disabled = lambda params: None
        p.client = Mock()
        p.client.with_options.return_value = p.client
        p.client.chat.completions.create.return_value = NS(choices=[NS(finish_reason='stop', message=NS(content='{}'))])
        p.response_structured('system', 'user', {}, temperature=.4)
        p.response_structured('router', 'user', {})
        self.assertEqual([call.kwargs['temperature'] for call in p.client.chat.completions.create.call_args_list], [.4, 0])


class HybridWorkflowTests(unittest.IsolatedAsyncioTestCase):
    setUp = natural.NaturalTests.setUp
    tearDown = natural.NaturalTests.tearDown
    add = natural.NaturalTests.add
    chat = natural.NaturalTests.chat
    texts = staticmethod(natural.NaturalTests.texts)

    def connection(self):
        c = natural.NaturalTests.connection(self)
        c.device_id = 'synthetic-device'; c.intent.read_only = True
        c.config['dialogue_quality'] = dict(enabled=True, strategy='hybrid', allowed_device_ids=['synthetic-device'])
        c.author_inputs = []; c.composer_inputs = []
        def role_reply(session, messages, **kwargs):
            c.author_inputs.append((deepcopy(messages), kwargs))
            return iter(['跟您聊这个，我也挺高兴。'])
        def compose(system, payload, schema, **kwargs):
            d = json.loads(payload); c.composer_inputs.append((d, kwargs))
            answer = '您想查哪个城市的天气？' if d['current_turn_data'].get('clarification_slot') == 'location' else '周叔，资料在这儿。'
            if any(b.get('topic') == 'weather' and b.get('status') == 'ok' for b in d['current_turn_data']['tool_results']):
                answer = '杭州预计有小雨。'
            return json.dumps(dict(valid=True, reply=answer, reason='', news_numbers=[]), ensure_ascii=False)
        c.llm.response = role_reply; c.intent.llm.response_structured = compose
        return c

    async def test_mixed_call_keeps_history_and_only_final_answers(self):
        self.add('以后叫我周叔。'); c = self.connection()
        await self.chat(c, '今天晒太阳挺舒服。', {'route': 'chat'})
        await self.chat(c, '你怎么称呼我？', {'route': 'memory_query', 'memory_query': 'preferred_address'})
        await self.chat(c, '咱俩接着聊刚才的太阳。', {'route': 'chat'})
        self.assertEqual(len(c.author_inputs), 2); self.assertEqual(len(c.composer_inputs), 1)
        self.assertIn('周叔', str(c.author_inputs[0][0]))
        self.assertIn('周叔，资料在这儿。', str(c.author_inputs[-1][0]))
        self.assertEqual([m.content for m in c.dialogue.dialogue if m.role == 'assistant'],
                         ['跟您聊这个，我也挺高兴。', '周叔，资料在这儿。', '跟您聊这个，我也挺高兴。'])
        self.assertTrue(all(kw['temperature'] == .4 for _, kw in c.author_inputs))
        self.assertEqual(c.composer_inputs[0][1]['temperature'], .4)
        self.assertEqual(c.composer_inputs[0][0]['current_turn_data']['personal_memory_read']['status'], 'found')

    async def test_weather_failure_uses_evidence_then_returns_to_character(self):
        c = self.connection()
        await self.chat(c, '杭州天气呢？', {'route': 'lookup', 'evidence': [dict(topic='weather', status='unavailable', results=[])]})
        await self.chat(c, '今天就待家里也挺好。', {'route': 'chat'})
        self.assertEqual(len(c.composer_inputs), 1); self.assertEqual(len(c.author_inputs), 1)
        self.assertIn('unavailable', str(c.composer_inputs[0][0]))

    async def test_clarification_and_pending_weather_are_not_sticky(self):
        c = self.connection()
        await self.chat(c, '今天天气怎么样？', {'route': 'clarify', 'clarification_slot': 'location'})
        await self.chat(c, '杭州。', {'route': 'lookup', 'evidence': [dict(topic='weather', status='ok', location='杭州', results=[dict(content='有小雨')])]})
        await self.chat(c, '我还是想和你聊会儿。', {'route': 'chat'})
        self.assertEqual(len(c.composer_inputs), 2); self.assertEqual(len(c.author_inputs), 2)
        self.assertEqual([kw['temperature'] for _, kw in c.author_inputs], [.7, .4])

    async def test_background_interest_retrieval_does_not_route_to_composer(self):
        c = self.connection()
        await self.chat(c, '我还是挺喜欢那种音乐。', {'route': 'chat', 'related_context_needed': True})
        self.assertEqual(len(c.author_inputs), 1); self.assertFalse(c.composer_inputs)

    async def test_proactive_policy_uses_direct_author_and_existing_silence_contract(self):
        c = self.connection()
        p = select_reply_policy(c, {'route': 'chat', 'initiative_opportunity': {'topic': '散步'}})
        self.assertEqual(p.strategy, 'role_direct')
        # Existing Q6 and role tests cover cancellation / [[SILENT]] / delivery.
        self.assertEqual(p.temperature, .4)

    async def test_router_keeps_current_utterance_separate_and_temperature_default(self):
        c = self.connection(); captured = []
        def route(system, payload, schema, **kwargs):
            captured.append((json.loads(payload), kwargs))
            return json.dumps(dict(route='chat', memory_query=None, requests=[], clarification_slot=None,
                                   user_fact_statement=True, related_context_needed=False))
        c.intent.llm.response_structured = route
        plan = await ReadOnlySession(c).plan('我今天去散了会儿步。')
        self.assertEqual(plan['route'], 'chat')
        self.assertEqual(captured[0][0]['current_user_utterance'], '我今天去散了会儿步。')
        self.assertIn('history', captured[0][0]['background'])
        self.assertNotIn('temperature', captured[0][1])
