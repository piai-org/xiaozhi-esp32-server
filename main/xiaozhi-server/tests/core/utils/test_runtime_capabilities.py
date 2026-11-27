"""Offline request-state tests; run directly with unittest, without pytest conftest."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.utils.dialogue import Dialogue, Message
from core.utils.runtime_capabilities import build_runtime_capabilities


class RuntimeCapabilitiesTests(unittest.TestCase):
    def test_nomem_is_not_a_retention_policy_and_secrets_are_excluded(self):
        config = {'selected_module': {'Memory': 'custom'}, 'Memory': {
            'custom': {'type': 'nomem', 'api_key': 'SYNTHETIC_SECRET'}}}
        state = build_runtime_capabilities(config, memory_str='')
        self.assertEqual(state['cross_session_recall'], 'unavailable')
        self.assertFalse(state['memory_returned_this_request'])
        self.assertIn('not_provided', state['conversation_retention_policy'])
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(state))
        self.assertEqual(state['tools_available_this_request'], [])

    def test_tools_follow_each_request_not_bound_plugin_settings(self):
        config = {'plugins': {'unavailable_plugin': {'api_key': 'SYNTHETIC_SECRET'}}}
        functions = [{'type': 'function', 'function': {'name': 'direct_answer'}},
                     {'type': 'function', 'function': {'name': 'weather_lookup'}}]
        state = build_runtime_capabilities(config, functions)
        self.assertEqual(state['tools_available_this_request'], ['weather_lookup'])
        self.assertEqual(build_runtime_capabilities(config, None)['tools_available_this_request'], [])
        self.assertNotIn('unavailable_plugin', json.dumps(state))

    def test_enabled_memory_does_not_promise_complete_history(self):
        state = build_runtime_capabilities({'selected_module': {'Memory': 'retriever'}},
                                           memory_str='合成已提供偏好', intent_type='intent_llm')
        self.assertTrue(state['memory_returned_this_request'])
        self.assertIn('未保证完整历史', state['cross_session_recall'])
        self.assertIn('前置意图路由', state['intent_routing'])

    def test_injection_is_per_request_preserves_memory_and_native_tool_result(self):
        d = Dialogue()
        original = '合成角色<memory></memory>'
        d.update_system_message(original)
        d.put(Message('assistant', tool_calls=[{'id': 'test-call', 'type': 'function',
                      'function': {'name': 'lookup', 'arguments': '{}'}}]))
        d.put(Message('tool', content='合成真实返回值', tool_call_id='test-call'))
        available = build_runtime_capabilities({}, [{'function': {'name': 'lookup'}}], '合成记忆')
        first = d.get_llm_dialogue_with_memory('合成记忆', runtime_capabilities=available)
        second = d.get_llm_dialogue_with_memory('', runtime_capabilities=build_runtime_capabilities({}))
        self.assertIn('合成记忆', first[0]['content'])
        self.assertIn('"tools_available_this_request": ["lookup"]', first[0]['content'])
        self.assertIn('"tools_available_this_request": []', second[0]['content'])
        self.assertNotIn('合成记忆', second[0]['content'])
        self.assertEqual(second[0]['content'].count('<runtime_capabilities>'), 1)
        self.assertEqual(first[2], {'role': 'tool', 'tool_call_id': 'test-call', 'content': '合成真实返回值'})
        self.assertEqual(d.dialogue[0].content, original)
        self.assertNotIn('runtime_capabilities', d.get_llm_dialogue()[0]['content'])

    def test_connection_supplies_actual_functions_and_memory_to_both_response_paths(self):
        # Structural wiring check, not a simulated WebSocket/device integration test.
        tree = ast.parse((ROOT / 'core/connection.py').read_text())
        chat = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'chat')
        calls = [n for n in ast.walk(chat) if isinstance(n, ast.Call)]
        build = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == 'build_runtime_capabilities')
        self.assertEqual([ast.unparse(a) for a in build.args],
                         ['self.config', 'functions', 'memory_str', 'self.intent_type'])
        responses = [n for n in calls if isinstance(n.func, ast.Attribute)
                     and ast.unparse(n.func.value) == 'self.llm'
                     and n.func.attr in ('response', 'response_with_functions')]
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(ast.unparse(n.args[1]) == 'llm_dialogue' for n in responses))


if __name__ == '__main__':
    unittest.main(verbosity=2)
