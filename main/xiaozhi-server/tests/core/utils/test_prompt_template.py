"""Template/role composition contract. Run with unittest without loading pytest conftest.

PROMPT_TEMPLATE_UNDER_TEST lets an isolated process check a candidate without deployment.
"""
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.utils.prompt_manager import PromptManager
from core.utils.dialogue import Dialogue, Message


class QuietLogger:
    def bind(self, **kwargs): return self
    def __getattr__(self, name): return lambda *args, **kwargs: None


class PromptTemplateContractTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Offline contract test'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.template = str(Path(os.environ.get('PROMPT_TEMPLATE_UNDER_TEST', ROOT / 'agent-base-prompt.txt')).resolve())
        self.config = {'prompt_template': self.template, 'selected_module': {'TTS': 'test_tts'},
                       'TTS': {'test_tts': {'language': '普通话'}}, 'context_providers': []}
        self.pm = PromptManager(self.config, logger=QuietLogger())
        self.assertIsNotNone(self.pm.base_prompt_template)

    def render(self, role='你叫木木，表达沉静克制。', **kwargs):
        return self.pm.build_enhanced_prompt(role, 'synthetic-contract-device', **kwargs)

    def test_roles_are_inserted_once_without_rewriting(self):
        for role in ('你叫木木，表达沉静克制。', '你叫阿洛，是热情直爽的科幻讲解员。',
                     'You are Rowan, a reserved astronomy guide.'):
            with self.subTest(role=role):
                text = self.render(role)
                self.assertEqual(text.count(role), 1)
                self.assertEqual(re.search(r'<identity>(.*?)</identity>', text, re.S)[1].strip(), role)

    def test_empty_optional_context_is_not_presented_as_observed_data(self):
        context = re.search(r'<context>(.*?)</context>', self.render(), re.S)[1]
        self.assertNotIn('位置资料：', context)
        self.assertNotIn('天气资料：', context)
        self.assertNotIn('{}', context)
        self.assertNotIn('None', context)

    def test_location_weather_and_dynamic_context_are_preserved_when_supplied(self):
        address = '合成测试城市'
        weather = '合成测试预报：2030-01-02 晴；仅用于离线组装'
        self.pm.cache_manager.set(self.pm.CacheType.LOCATION, 'synthetic-test-ip', address)
        self.pm.cache_manager.set(self.pm.CacheType.WEATHER, address, weather)
        self.addCleanup(self.pm.cache_manager.delete, self.pm.CacheType.LOCATION, 'synthetic-test-ip')
        self.addCleanup(self.pm.cache_manager.delete, self.pm.CacheType.WEATHER, address)
        self.pm.context_data = '合成资料值 {{ this_is_data_not_a_template_variable }}'
        context = re.search(r'<context>(.*?)</context>', self.render(client_ip='synthetic-test-ip'), re.S)[1]
        for value in (address, weather, self.pm.context_data): self.assertIn(value, context)

    def test_memory_injection_preserves_role_protocols_and_context(self):
        text = self.render()
        d = Dialogue()
        d.update_system_message(text)
        before = d.get_llm_dialogue_with_memory('', {}, None)[0]['content']
        after = d.get_llm_dialogue_with_memory('离线合成偏好：喜欢天文学。', {}, None)[0]['content']
        for tag in ('identity', 'input_protocol', 'capability_protocol', 'output_protocol', 'data_protocol', 'safety_compliance', 'context', 'memory'):
            self.assertEqual(after.count('<'+tag+'>'), 1)
            self.assertEqual(after.count('</'+tag+'>'), 1)
        self.assertEqual(re.sub(r'<memory>.*?</memory>', '', before, flags=re.S),
                         re.sub(r'<memory>.*?</memory>', '', after, flags=re.S))
        self.assertNotIn('离线合成偏好', d.dialogue[0].content)
        self.assertNotIn('{{current_time}}', after)

    def test_emoji_device_modes_do_not_force_a_personality(self):
        off = self.render(emoji_enabled=False)
        on = self.render(emoji_enabled=True)
        self.assertNotIn('🙂', off)
        self.assertIn('🙂', on)
        self.assertIn('仅当角色允许', on)
        self.assertIn('不强制添加', on)
        self.assertIn('普通回答不输出 emoji', off)

    def test_language_uses_configured_tts_default_and_falls_back_when_missing(self):
        self.assertIn('默认输出语言为 普通话', self.render())
        self.config['TTS']['test_tts']['language'] = 'English'
        self.assertIn('默认输出语言为 English', self.render())
        self.config['TTS'] = {}
        self.assertIn('默认输出语言为 中文', self.render())

    def test_real_history_and_native_tool_messages_keep_their_structure(self):
        d = Dialogue()
        d.update_system_message(self.render())
        d.put(Message(role='user', content='合成输入：查资料。'))
        calls = [{'id':'synthetic-tool-id','type':'function','function':{'name':'synthetic_lookup','arguments':'{}'}}]
        d.put(Message(role='assistant', tool_calls=calls))
        d.put(Message(role='tool',tool_call_id='synthetic-tool-id',content='{"result":"合成工具返回"}'))
        d.put(Message(role='user',content='现在解释这个结果。'))
        messages = d.get_llm_dialogue_with_memory('', {}, None)
        self.assertEqual([m['role'] for m in messages], ['system','user','assistant','tool','user'])
        self.assertEqual(messages[2]['tool_calls'], calls)
        self.assertEqual(messages[3]['tool_call_id'], 'synthetic-tool-id')

    def test_unknown_speaker_does_not_inject_other_peoples_profiles(self):
        d = Dialogue()
        d.update_system_message(self.render())
        profile = {'speakers':['synthetic-id,测试姓名,合成档案内容']}
        unknown = d.get_llm_dialogue_with_memory('',profile,'未知说话人')[0]['content']
        known = d.get_llm_dialogue_with_memory('',profile,'测试姓名')[0]['content']
        self.assertNotIn('合成档案内容', unknown)
        self.assertIn('当前说话人：测试姓名', known)


if __name__ == '__main__':
    unittest.main(verbosity=2)
