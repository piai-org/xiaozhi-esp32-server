"""Context withdrawal contracts; model behavior is evaluated separately."""
import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.utils.dialogue import Dialogue, Message


class NamingContextRepairTests(unittest.TestCase):
    def build(self, correction='我没说过让我叫奶奶，是你说错了。'):
        d = Dialogue()
        d.update_system_message('合成角色<memory></memory>')
        for role, content in [('assistant', '我在这里哦！'), ('user', '你好。'),
                              ('assistant', '您之前说让我叫您奶奶，我记着呢。'),
                              ('user', correction)]:
            d.put(Message(role, content))
        return d

    def test_withdraws_rejected_plain_turn_but_preserves_original_record(self):
        d = self.build()
        before = [(m.uniq_id, m.role, m.content) for m in d.dialogue]
        repaired = d.get_llm_dialogue_with_memory(repair_rejected_naming=True)
        self.assertNotIn('您之前说让我叫您奶奶，我记着呢。', str(repaired))
        self.assertEqual([m['content'] for m in repaired[1:]],
                         ['我在这里哦！', '你好。', '我没说过让我叫奶奶，是你说错了。'])
        self.assertIn('本轮用户明确指出', repaired[0]['content'])
        self.assertEqual(before, [(m.uniq_id, m.role, m.content) for m in d.dialogue])
        plain = d.get_llm_dialogue_with_memory()
        self.assertIn('您之前说让我叫您奶奶，我记着呢。', str(plain))

    def test_repair_survives_followup_without_repeating_current_turn_notice(self):
        d = self.build()
        d.put(Message('assistant', '是我说错了，我先称呼您为您。'))
        d.put(Message('user', '我真正说了什么？'))
        messages = d.get_llm_dialogue_with_memory(repair_rejected_naming=True)
        self.assertNotIn('您之前说让我叫您奶奶，我记着呢。', str(messages))
        self.assertNotIn('本轮用户明确指出', messages[0]['content'])
        self.assertIn('是我说错了，我先称呼您为您。', str(messages))

    def test_new_explicit_nickname_in_correction_is_preserved(self):
        text = '我没让你叫我奶奶，以后叫我周老师。'
        messages = self.build(text).get_llm_dialogue_with_memory(repair_rejected_naming=True)
        self.assertEqual(messages[-1]['content'], text)
        self.assertIn('没有新的明确称呼要求时', messages[0]['content'])

    def test_plain_requests_quotes_third_party_and_double_negative_do_not_withdraw(self):
        for text in ['叫我奶奶吧。', '你还记得我让你怎么称呼我吗？',
                     '邻居说我没让你叫我奶奶。', '电影台词是“我没说过让我叫奶奶”。',
                     '我没说过不让你叫我奶奶。', '我没说过今天晴天。']:
            with self.subTest(text=text):
                messages = self.build(text).get_llm_dialogue_with_memory(repair_rejected_naming=True)
                self.assertIn('您之前说让我叫您奶奶，我记着呢。', str(messages))

    def test_only_direct_previous_naming_turn_and_no_tool_corruption(self):
        d = self.build()
        d.dialogue[-2].content = '今天天气晴。'
        self.assertIn('今天天气晴。', str(d.get_llm_dialogue_with_memory(repair_rejected_naming=True)))
        d = self.build()
        d.dialogue[-2].tool_calls = [{'id':'a','type':'function',
                                     'function':{'name':'lookup','arguments':'{}'}}]
        messages = d.get_llm_dialogue_with_memory(repair_rejected_naming=True)
        self.assertTrue(any(m.get('tool_calls') for m in messages))
        self.assertTrue(any(m.get('tool_call_id') == 'a' for m in messages))

    def test_json_speech_detection_preserves_raw_content(self):
        text = '{"speaker":"未知说话人","content":"我没说过让我叫奶奶，是你说错了。"}'
        messages = self.build(text).get_llm_dialogue_with_memory(repair_rejected_naming=True)
        self.assertEqual(messages[-1]['content'], text)
        self.assertNotIn('您之前说让我叫您奶奶，我记着呢。', str(messages))

    def test_connection_enables_repair_before_both_provider_paths(self):
        tree = ast.parse((ROOT / 'core/connection.py').read_text())
        chat = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'chat')
        calls = [n for n in ast.walk(chat) if isinstance(n, ast.Call)]
        assemble = next(n for n in calls if isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'get_llm_dialogue_with_memory')
        self.assertTrue(next(k.value.value for k in assemble.keywords
                             if k.arg == 'repair_rejected_naming'))
        responses = [n for n in calls if isinstance(n.func, ast.Attribute)
                     and ast.unparse(n.func.value) == 'self.llm'
                     and n.func.attr in ('response', 'response_with_functions')]
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(ast.unparse(n.args[1]) == 'llm_dialogue' for n in responses))

    def test_source_answer_quotes_only_users_not_role_or_wrong_assistant(self):
        d = self.build()
        d.put(Message('assistant', '您之前还叫过我小圆。'))
        d.put(Message('user', '你能说清这次对话里我真正说了什么吗？'))
        answer = d.get_naming_grounded_response()
        self.assertIn('‘你好。’', answer)
        self.assertIn('‘我没说过让我叫奶奶，是你说错了。’', answer)
        self.assertNotIn('您之前还叫过我小圆', answer)
        self.assertNotIn('合成角色', answer)

    def test_greeting_source_and_correction_are_grounded_without_generation(self):
        d = Dialogue()
        d.put(Message('user', '你好。'))
        self.assertEqual(d.get_naming_grounded_response(), '您好，我在呢。')
        d.put(Message('assistant', '您好，我在呢。'))
        d.put(Message('user', '我称呼你了吗？'))
        self.assertIn('‘你好。’', d.get_naming_grounded_response())
        self.assertNotIn('小圆', d.get_naming_grounded_response())
        self.assertEqual(self.build().get_naming_grounded_response(), '是我说错了。我先称呼您为‘您’。')

    def test_ordinary_chat_new_preference_and_non_source_name_requests_use_model(self):
        for text in ['你好，我今天拍到了一只猫。', '给我讲一个奶奶的故事。',
                     '以后就叫我奶奶，陪我聊会儿。', '现在叫我周老师。',
                     '我没让你叫我奶奶，以后叫我周老师。']:
            with self.subTest(text=text):
                self.assertIsNone(self.build(text).get_naming_grounded_response())

    def test_json_input_and_bounded_quote_disclose_recent_scope(self):
        d = Dialogue()
        d.put(Message('user', '很早以前的内容'))
        d.put(Message('user', '{"speaker":"未知说话人","content":"邻居喜欢被称呼为奶奶。"}'))
        d.put(Message('user', '长' * 100))
        d.put(Message('user', '到现在为止，我有没有告诉你该怎么称呼我？'))
        answer = d.get_naming_grounded_response()
        self.assertIn('最近原话', answer)
        self.assertIn('邻居喜欢被称呼为奶奶。', answer)
        self.assertNotIn('很早以前的内容', answer)
        self.assertNotIn('未知说话人', answer)
        self.assertIn('……', answer)


if __name__ == '__main__':
    unittest.main(verbosity=2)
