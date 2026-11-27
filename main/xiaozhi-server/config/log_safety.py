"""Fixed-vocabulary application logs. No business imports, request I/O or secrets."""
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger

_POLICY = json.loads(Path(__file__).with_name('log_fixed_messages.json').read_text())
_FIXED = frozenset(_POLICY['fixed_messages']) | {'标准库诊断信息已省略'}
_MODULES = frozenset(_POLICY['modules']) | {'config.log_safety'}
_MARKER = object()
_TZ = timezone(timedelta(hours=8))
_LEVELS = {5: 'TRACE', 10: 'DEBUG', 20: 'INFO', 25: 'SUCCESS', 30: 'WARNING', 40: 'ERROR', 50: 'CRITICAL'}
_ERRORS = frozenset({'TimeoutError', 'ValueError', 'TypeError', 'KeyError', 'RuntimeError',
    'OSError', 'ConnectionError', 'PermissionError', 'FileNotFoundError', 'CancelledError',
    'JSONDecodeError', 'AuthenticationError', 'ConnectionClosed', 'ConnectionClosedError',
    'ConnectionClosedOK', 'HTTPError', 'HTTPStatusError', 'ReadTimeout', 'ConnectTimeout'})
_NAMESPACES = frozenset({'websockets', 'aiohttp', 'httpx', 'httpcore', 'urllib3', 'asyncio', 'requests', 'root'})
_STATUSES = frozenset({'ok', 'found', 'empty', 'error', 'failed', 'pending', 'queued', 'ready', 'unavailable', 'not_found', 'disabled', 'unknown', 'partial'})


def _metadata(message):
    for prefix, event in [('核心记忆上下文: ', 'memory.context'), ('个人事实回答: ', 'memory.lookup')]:
        if not message.startswith(prefix):
            continue
        try:
            data = json.loads(message[len(prefix):])
        except (ValueError, TypeError):
            return event, {}
        if type(data) is not dict:
            return event, {}
        selected = {}
        status = data.get('status')
        if type(status) is str:
            selected['status'] = status if status in _STATUSES else 'unknown'
        for key in ['count', 'facts_count']:
            value = data.get(key)
            if type(value) is int and 0 <= value <= 1_000_000_000:
                selected[key] = value
        value = data.get('related_returned')
        if type(value) is bool:
            selected['related_returned'] = value
        return event, selected
    return None, {}


def sanitize_record(record):
    """Scrub before sinks/queues, then recheck after caller-bound patchers."""
    previous = record.get('_voice_safe_record')
    if (type(previous) is tuple and len(previous) == 2 and previous[0] is _MARKER
            and record.get('message') == previous[1]):
        # A later patcher may have rebound extra/exception after global sanitizing.
        record['extra'] = {}
        record['exception'] = None
        return
    try:
        level_no = record['level'].no
        payload = {'time': datetime.now(_TZ).isoformat(timespec='milliseconds'),
                   'level': _LEVELS.get(level_no, 'OTHER'), 'event': 'log.redacted'}
        module = record.get('name')
        payload['component'] = module if type(module) is str and module in _MODULES else 'external'
        line = record.get('line')
        if type(line) is int and 0 < line <= 1_000_000:
            payload['line'] = line
        exception = record.get('exception')
        if exception is not None:
            kind = getattr(getattr(exception, 'type', None), '__name__', '')
            payload['error_type'] = kind if kind in _ERRORS else 'Error'
        message = record.get('message')
        if type(message) is str and len(message) <= 4096:
            event, values = _metadata(message)
            extra = record.get('extra')
            std = extra.get('_stdlib_component') if type(extra) is dict else None
            if message == '标准库诊断信息已省略' and type(std) is str and std in _NAMESPACES:
                payload.update(event='stdlib.log', component=std)
            elif event:
                payload.update(event=event, **values)
            elif message in _FIXED:
                payload.update(event='log.fixed', text=message)
            elif 'Headers:' in message:
                payload['event'] = 'connection.headers.redacted'
            elif message.startswith('大模型收到用户消息:'):
                payload['event'] = 'llm.request'
            elif message.startswith('识别文本:'):
                payload['event'] = 'asr.result'
            elif message.startswith('LLM 处理出错'):
                payload['event'] = 'llm.failure'
            elif message.startswith('audio_play_priority_thread:'):
                payload['event'] = 'tts.failure'
            elif level_no >= 40:
                payload['event'] = 'log.error'
        safe = json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n'
    except Exception:
        # Never interpolate exception messages or the failed record in diagnostics.
        safe = '{"level":"ERROR","event":"log.sanitizer_failure"}\n'
    record['message'] = safe
    record['extra'] = {}
    record['exception'] = None
    record['_voice_safe_record'] = (_MARKER, safe)
    record.pop('selected_module', None)


def safe_filter(record):
    sanitize_record(record)
    return True


def safe_format(record):
    # Callable format avoids Loguru's implicit exception/newline suffix.
    # Message already contains exactly one JSON event and newline, even raw=True.
    return '{message}'


def private_opener(path, flags):
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.fchmod(fd, 0o600)
    return fd


def safe_diagnostic():
    """Replacement for app print/print_exc; caller expressions aren't evaluated."""
    logger.opt(depth=1).warning('诊断信息已省略')


class SafeStandardHandler(logging.Handler):
    def emit(self, record):
        try:
            name = record.name if type(record.name) is str else ''
            prefix = name.split('.', 1)[0]
            namespace = prefix if prefix in _NAMESPACES else 'root'
            logger.bind(_stdlib_component=namespace).opt(exception=record.exc_info or None).log(
                record.levelno, '标准库诊断信息已省略')
        except Exception:
            # logging.handleError() can dump original record.msg/args. Drop instead.
            return


def configure_standard_logging():
    """Route existing Python handlers through the same fixed-vocabulary output."""
    handler = SafeStandardHandler()
    root = logging.getLogger()
    root.handlers[:] = [handler]
    for value in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(value, logging.Logger):
            value.handlers.clear()
            value.propagate = True
