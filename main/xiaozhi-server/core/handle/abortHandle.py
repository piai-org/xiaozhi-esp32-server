import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    initiative=getattr(conn,'_initiative_runtime',None)
    if initiative:
        initiative.cancel('收到打断',abort=False)
        initiative.normal_busy=False
        # An output abort must not finish a newer, real user utterance.
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    interrupted_turn = getattr(conn, "sentence_id", None)
    from core.utils.companion_affect import cancel_affect_turn
    await asyncio.to_thread(cancel_affect_turn, conn, interrupted_turn, withdraw=True)
    from core.utils.companion_memory import mark_episode_delivery
    await asyncio.to_thread(mark_episode_delivery, conn, interrupted_turn, 'interrupted')
    dialogue = getattr(conn, "dialogue", None)
    if dialogue is not None:
        dialogue.mark_delivery(interrupted_turn, "interrupted")
    pending=getattr(conn,'_recent_reply_delivery',None)
    if not pending:
        sent = getattr(conn, '_recent_reply_sent', None)
        if sent and sent[0] == interrupted_turn:
            pending = sent
    if pending:
        try:
            await asyncio.to_thread(conn.memory.record_recent_reply,pending[1],status='interrupted')
        except Exception as exc:
            conn.logger.bind(tag=TAG).warning('近期打断状态保存不可用: '+type(exc).__name__)
        if getattr(conn,'_recent_reply_delivery',None)==pending:conn._recent_reply_delivery=None
        if getattr(conn,'_recent_reply_sent',None)==pending:conn._recent_reply_sent=None
    if hasattr(conn, "information_session"):
        conn.information_session.invalidate()
    cancel_audio = None
    if getattr(getattr(conn, "intent", None), "read_only", False):
        conn.sentence_id = 'cancelled-' + uuid.uuid4().hex
        cancel_audio = getattr(conn.tts, "cancel_pending_audio", None)
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id,
                    "reason": "interrupted", "turn_id": interrupted_turn})
    )
    conn.clearSpeakStatus()
    if cancel_audio:
        await cancel_audio()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
