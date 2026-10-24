"""Optional public model-decision metadata. No chain of thought or credentials.

Native action IDs returned in MCP results provide exact correlation even for
concurrent tasks. There is no ambient 'current task' shared by MCP processes.
The bounded background queue cannot block the existing app-server callback.
"""
from __future__ import annotations
import json
import queue
import re
import threading
from pathlib import Path
from typing import Any

_queue: queue.Queue = queue.Queue(maxsize=64)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_SENSITIVE = re.compile(r'(?i)(password|token|secret|api.?key|验证码|密码|银行卡|聊天|支付)')


def _safe(value: Any, key='') -> Any:
    if _SENSITIVE.search(key) or key in ('text', 'image', 'data'):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_safe(v) for v in value[:20]]
    if isinstance(value, str):
        if _SENSITIVE.search(value): return '[REDACTED]'
        return re.sub(r'(?i)([\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)\d{6,}(?!\d)|sk-[\w-]+)', '[REDACTED]', value[:1000])
    return value


def _work():
    from android_mcp_server import AndroidBridge
    while True:
        state, body = _queue.get()
        try:
            AndroidBridge(Path(state), timeout=2).request('POST', '/api/trajectory/feedback', body)
        except Exception:
            pass  # Recorder absence/failure never fails a running task.
        finally:
            _queue.task_done()


def observe_engine_event(state: Path, task: dict, params: dict) -> None:
    global _thread
    item = params.get('item') or {}
    content = (item.get('result') or {}).get('content') or []
    ids = []
    for entry in content:
        if not isinstance(entry, dict) or entry.get('type') != 'text': continue
        try:
            value = json.loads(entry.get('text', ''))
        except (ValueError, TypeError):
            continue
        for candidate in (value, value.get('error', {}), value.get('tree', {})):
            ident = candidate.get('trajectory_action_id') if isinstance(candidate, dict) else None
            if isinstance(ident, str) and re.fullmatch(r'[0-9a-f-]{36}', ident): ids.append(ident)
    # Candidate lists and confidence are not invented when the provider omits them.
    arguments = item.get('arguments') or {}
    if isinstance(arguments, str):
        try: arguments = json.loads(arguments)
        except ValueError: arguments = {}
    decision = {'source': 'deepseek_agent', 'model': task.get('model'), 'tool_call_id': item.get('id'),
                'tool': item.get('tool'), 'selected_action': _safe(arguments),
                'candidate_actions': [], 'confidence': None}
    body_task = {'id': task['id'], 'user_task': _safe(task.get('prompt', '')),
                 'subgoal': _safe(task.get('_trajectory_subgoal', '')), 'stage': task.get('status', 'running'), 'source': 'agent'}
    for ident in set(ids):
        try:
            _queue.put_nowait((str(state), {'action_id': ident, 'task': body_task, 'decision': decision,
                            'verification': {'status': 'unverified', 'source': 'agent_claim'}}))
        except queue.Full:
            return
    if ids:
        with _lock:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_work, name='trajectory-feedback', daemon=True)
                _thread.start()
