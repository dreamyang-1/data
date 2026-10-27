"""按请求收集有限诊断证据；不读取全局日志文件，不采集变量或数据行。"""
from __future__ import annotations

import logging
import re
import threading
import traceback
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


def redact(value: Any, limit: int = 1200) -> str:
    text = str(value)
    # Redact before truncation, including quoted JSON and credential URLs.
    text = re.sub(r'(?i)([\w-]*(?:password|passwd|secret|token|authorization|cookie|api[_-]?key)[\w-]*[\s\"\x27]*[:=]\s*)("[^"]*"|\x27[^\x27]*\x27|[^\s,;}]+)', r'\1[已隐藏]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s,;\"\x27]+', 'Bearer [已隐藏]', text)
    text = re.sub(r'(?i)\b(?:https?|mysql|postgres(?:ql)?|redis)://[^\s\"\x27<>]+', '[服务地址]', text)
    text = re.sub(r'(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?', '[地址]', text)
    text = re.sub(r'(?:[A-Za-z]:[\\/]|/(?:root|home|etc|tmp|var|opt)/)[^\s\"\x27]+', '[路径]', text)
    text = re.sub(r'(?is)\b(?:SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\s+.*', '[查询文本已隐藏]', text)
    text = re.sub(r'(?is)[\"\x27]?(?:rows|records|data_rows|sample_rows|preview_rows)[\"\x27]?\s*[:=]\s*\[.*', '[数据行已隐藏]', text)
    return text[:limit]


def bounded_detail(value: Any, depth: int = 0) -> Any:
    """保留参数结构和校验原因，不携带结果行或连接配置。"""
    if depth >= 6:
        return '[嵌套内容省略]'
    if isinstance(value, dict):
        return {str(k): bounded_detail(v, depth + 1) for k, v in list(value.items())[:24]
                if not re.search(r'(?i)password|secret|token|authorization|cookie|api.?key|rows|records|sql', str(k))}
    if isinstance(value, (list, tuple)):
        items = [bounded_detail(v, depth + 1) for v in value[:12]]
        return items + ([f'[另有{len(value) - 12}项省略]'] if len(value) > 12 else [])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value, 1200)


@dataclass
class Diagnostics:
    question: str
    scope: dict | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=24))
    warnings: deque = field(default_factory=lambda: deque(maxlen=16))
    progress: deque = field(default_factory=lambda: deque(maxlen=32))
    faults: list = field(default_factory=list)
    blockers: deque = field(default_factory=lambda: deque(maxlen=12))
    semantic_reference: dict | None = None
    explaining: bool = False
    closed: bool = False
    explanation: str | None = None

    def record_progress(self, event: dict) -> None:
        # Deliberately exclude tool input/output, SQL, ASL and row previews.
        keys = ('stage', 'status', 'task_id', 'task_index', 'task_name',
                'progress_phase', 'task_result_status', 'error_code', 'reason')
        item = {k: redact(event[k], 300) for k in keys if event.get(k) is not None}
        if item and (not self.progress or self.progress[-1] != item):
            self.progress.append(item)

    def exception(self, exc: Exception) -> None:
        seen = set()
        while exc is not None and id(exc) not in seen and len(seen) < 4:
            seen.add(id(exc))
            frames = traceback.extract_tb(exc.__traceback__)[-5:]
            self.faults.append({
                'type': type(exc).__name__, 'message': redact(exc),
                'frames': [{'function': f.name, 'line': f.lineno} for f in frames],
            })
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)

    def payload(self, outcome: dict | None = None) -> dict:
        evidence = []
        for kind, items in [('progress', list(self.progress)), ('log', list(self.logs)),
                            ('warning', list(self.warnings)), ('exception', list(self.faults)),
                            ('structured_blocker', list(self.blockers)),
                            ('outcome', [outcome] if outcome else [])]:
            for item in items:
                evidence.append({'id': f'E{len(evidence) + 1}', 'kind': kind, 'detail': item})
        return {'question': redact(self.question, 1200), 'evidence': evidence,
                'authorized_scope': self.scope, 'semantic_reference': self.semantic_reference,
                'notice': '仅当前请求的有限日志；未出现某步骤记录不代表该步骤一定未执行。'}


_current: ContextVar[Diagnostics | None] = ContextVar('exception_explainer_request', default=None)
_install_lock = threading.Lock()


class _RequestLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        state = _current.get()
        if state is None or state.closed or state.explaining or not record.name.startswith('app.'):
            return
        try:
            target = state.warnings if record.levelno >= logging.WARNING else state.logs
            target.append({'level': record.levelname, 'module': record.name,
                           'message': redact(record.getMessage())})
            if record.exc_info and isinstance(record.exc_info[1], Exception):
                # Log evidence already carries the exception context, without frame locals.
                target.append({'type': type(record.exc_info[1]).__name__,
                               'message': redact(record.exc_info[1])})
        except Exception:
            pass  # Logging must never interrupt the user's operation.


def begin(question: str, *, scope: dict | None = None):
    with _install_lock:
        root = logging.getLogger()
        if not any(isinstance(h, _RequestLogHandler) for h in root.handlers):
            root.addHandler(_RequestLogHandler(logging.INFO))
    state = Diagnostics(question, scope=scope)
    return state, _current.set(state)


def end(state: Diagnostics, token) -> None:
    state.closed = True
    _current.reset(token)
