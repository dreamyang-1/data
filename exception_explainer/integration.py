"""唯一智能体适配入口；模型实现与业务执行逻辑隔离。"""
from __future__ import annotations

import json

from app.observability.langfuse_client import trace_generation
from app.services.error_explanations import explain_unexpected_failure

from .diagnostics import Diagnostics, begin, end, redact
from .model import ModelConfig, explain
from .prompt import SYSTEM_PROMPT


def _config(settings):
    key = settings.intent_model_api_key
    if not settings.intent_model_enabled or key is None:
        return None
    return ModelConfig(settings.intent_model_base_url, key.get_secret_value(),
                       settings.intent_model_name)


async def exception_message(state: Diagnostics, settings, exc: Exception) -> str:
    state.exception(exc)
    return await _explain(state, settings, explain_unexpected_failure(exc))


async def _explain(state, settings, fallback, outcome=None):
    config = _config(settings)
    with trace_generation(name='异常原因解释', model=config.model if config else None,
                          messages=[{'role': 'system', 'content': SYSTEM_PROMPT},
                                    {'role': 'user', 'content': json.dumps(state.payload(outcome), ensure_ascii=False)}]) as span:
        return await explain(state, config, fallback=fallback, outcome=outcome,
                             recorder=span.set_response)


async def explain_response(state: Diagnostics, settings, response):
    """处理工作流已捕获并返回的异常；正常成功、正常追问与无数据保持原样。"""
    failed_statuses = {'FAILED', 'SAFE_FALLBACK', 'ERROR', 'TIMEOUT'}
    tasks = [t for t in response.task_results if t.status in failed_statuses]
    extensions = [t for t in response.extension_executions if t.status in failed_statuses]
    incomplete = bool(response.reliability and response.reliability.gates.get('analysis_completed') is False)
    if response.status not in failed_statuses | {'PARTIAL_SUCCESS'} and not tasks and not extensions and not incomplete:
        return response
    outcome = {
        'status': response.status, 'error_code': redact(response.error_code or ''),
        'failed_tasks': [{'task_id': t.task_id, 'question': redact(t.question, 500), 'status': t.status, 'reported_reason': redact(t.answer, 600)} for t in tasks],
        'completed_tasks': [t.task_id for t in response.task_results if t.status == 'COMPLETED'],
        'extension_errors': [{'name': t.name, 'status': t.status, 'error': redact(t.error or '', 600)} for t in extensions],
        'warnings': [redact(w, 500) for w in (response.reliability.warnings if response.reliability else [])][-8:],
        'reported_answer': redact(response.answer, 1000) if response.status in failed_statuses else '',
    }
    fallback = response.answer or '本次任务未完成，现有错误记录还不足以确定具体原因。'
    message = await _explain(state, settings, fallback, outcome)
    if message == redact(fallback, 500):
        return response
    updated = response.model_copy(deep=True)
    # Partial data, tables, files and status stay intact; explanation is only presentation.
    partial = response.status not in failed_statuses or bool(outcome['completed_tasks']) or bool(response.files)
    updated.answer = (response.answer + '\n\n' + message) if partial else message
    return updated


def stream_failure_events(error: dict):
    """使用现有 SSE 事件类型；保留失败状态，不伪造完成或补发未执行的阶段。"""
    message = error['message']
    yield 'updata_state', {'step': '', 'data': 'output'}
    yield 'message_chunk', {'step': 'output', 'index': 0, 'content': message,
                            'role': 'assistant', 'node': 'data_analysis', 'is_last': True}
    yield 'answer', {'step': 'output', 'content': message, 'status': 'SAFE_FALLBACK'}
    yield 'error', error
