"""Run with unittest inside the voice environment; no model API calls."""

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.providers.asr.aliyunbl_stream import ASRProvider


MODEL = "qwen-audio-3.1-asr-flash-message"
ENDPOINT = "wss://ws-example.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"


class RequestTests(unittest.TestCase):
    def test_message_accepts_workspace_and_string_form_values(self):
        provider = ASRProvider({
            "model": MODEL, "ws_url": ENDPOINT, "sample_rate": "16000",
            "keep_dialect": "true", "disfluency_removal_enabled": "false",
        }, True)
        params = provider._build_run_task_message()["payload"]["parameters"]
        self.assertEqual(provider.ws_url, ENDPOINT)
        self.assertEqual(params["sample_rate"], 16000)
        self.assertEqual(params["max_sentence_silence"], 800)
        self.assertIs(params["keep_dialect"], True)
        self.assertIs(params["disfluency_removal_enabled"], False)
        self.assertNotIn("semantic_punctuation_enabled", params)
        self.assertNotIn("multi_threshold_mode_enabled", params)

    def test_message_rejects_wrong_sample_rate(self):
        with self.assertRaises(ValueError):
            ASRProvider({"model": MODEL, "sample_rate": 24000}, True)

    def test_endpoint_rejects_other_hosts(self):
        with self.assertRaises(ValueError):
            ASRProvider({"model": MODEL, "ws_url": "wss://example.com/api-ws/v1/inference"}, True)

    def test_paraformer_retains_existing_controls(self):
        provider = ASRProvider({"model": "paraformer-realtime-v2", "vocabulary_id": "test-vocabulary"}, True)
        params = provider._build_run_task_message()["payload"]["parameters"]
        self.assertEqual(params["max_sentence_silence"], 200)
        self.assertEqual(params["vocabulary_id"], "test-vocabulary")
        self.assertNotIn("keep_dialect", params)


class ResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_stop_and_cleanup_send_finish_only_once(self):
        provider = ASRProvider({"model": MODEL}, True)
        socket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        provider.asr_ws = socket
        provider.task_id = "test-task"
        await provider._send_stop_request()
        await asyncio.gather(provider._cleanup(), provider.close())
        self.assertEqual(socket.send.await_count, 1)
        self.assertEqual(socket.close.await_count, 1)

    async def run_results(self, mode, sentences, stopped=False):
        provider = ASRProvider({"model": MODEL}, True)
        messages = asyncio.Queue()
        for sentence in sentences:
            messages.put_nowait(json.dumps({"header": {"event": "result-generated"}, "payload": {"output": {"sentence": sentence}}}))
        messages.put_nowait(json.dumps({"header": {"event": "task-finished"}, "payload": {}}))
        provider.asr_ws = SimpleNamespace(recv=messages.get, send=AsyncMock(), close=AsyncMock())
        provider.task_id = "test-task"
        conn = SimpleNamespace(stop_event=threading.Event(), asr_audio=[b"pcm"],
                               client_listen_mode=mode, client_voice_stop=stopped,
                               reset_audio_states=lambda: None)
        recognized = []

        async def handle_voice_stop(connection, audio):
            text, _ = await provider.speech_to_text(audio, "test-session")
            recognized.append(text)

        provider.handle_voice_stop = handle_voice_stop
        await provider._forward_results(conn)
        return provider, recognized

    async def test_auto_ignores_intermediate_and_heartbeat(self):
        _, recognized = await self.run_results("auto", [
            {"text": "", "sentence_begin": True},
            {"text": "你好", "sentence_end": False, "end_time": None},
            {"text": "noise", "heartbeat": True, "sentence_end": True, "end_time": 0},
            {"text": "你好，小圆。", "sentence_end": True, "end_time": 1200},
        ])
        self.assertEqual(recognized, ["你好，小圆。"])

    async def test_manual_preserves_final_sentences_until_stop(self):
        provider, recognized = await self.run_results("manual", [
            {"text": "不是明天。", "sentence_end": True, "end_time": 1000},
            {"text": "是后天。", "sentence_end": True, "end_time": 2000},
        ])
        self.assertEqual(recognized, [])
        self.assertEqual(provider.text, "不是明天。是后天。")


if __name__ == "__main__":
    unittest.main()
