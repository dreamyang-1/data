"""唯一智能体适配入口；模型实现与业务执行逻辑隔离。"""
from __future__ import annotations

import json

from app.observability.langfuse_client import trace_generation
from app.services.error_explanations import explain_unexpected_failure

from .diagnostics import Diagnostics, begin, end, redact, bounded_detail, _current
from .model import ModelConfig, explain
from .prompt import SYSTEM_PROMPT
from .semantic_reference import load_reference


def record_parameter_blocker(request, *, source_stage: str) -> None:
    """在校验拦停处保留本次参数证据；不调用模型，不修改查询或 Pending。"""
    state = _current.get()
    if state is None or state.closed or state.explaining:
        return
    if state.scope and request.semantic_model_id != state.scope['semantic_model_id']:
        return
    if state.scope and state.scope['business_domain_ids']:
        domains = request.business_domain_ids or request.resolved_business_domain_ids
        if domains and not set(domains) <= set(state.scope['business_domain_ids']):
            return
    from app.services.progress import _task_progress_context
    task = _task_progress_context.get() or {}
    extraction = getattr(request, '_planner_extraction', None)
    ambiguities = [item.model_dump(mode='json') for item in request.semantic_ambiguities if item.blocking]
    structured = source_stage == 'STRUCTURED_ASL_BINDING' or bool(extraction) or any(
        '结构化参数' in str(item.get('question', '')) for item in ambiguities)
    state.blockers.append(bounded_detail({
        'task_id': task.get('task_id'), 'request_id': str(request.request_id),
        'source_stage': source_stage, 'structured': structured,
        'question': request.rewritten_question or request.original_question,
        'parameters': getattr(extraction, 'structured', None),
        'missing_slots': list(request.missing_slots), 'ambiguities': ambiguities,
        'metrics': list(request.metrics), 'dimensions': list(request.dimensions),
        'filters': list(request.filters),
    }))


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
    if config is not None and state.explanation is None and state.semantic_reference is None:
        state.semantic_reference = await load_reference(settings, state.scope)
    with trace_generation(name='异常原因解释', model=config.model if config else None,
                          messages=[{'role': 'system', 'content': SYSTEM_PROMPT},
                                    {'role': 'user', 'content': json.dumps(state.payload(outcome), ensure_ascii=False)}]) as span:
        return await explain(state, config, fallback=fallback, outcome=outcome,
                             recorder=span.set_response)


async def explain_response(state: Diagnostics, settings, response):
    """解释失败及结构化参数拦停；保留状态、候选、已完成结果和恢复信息。"""
    failed_statuses = {'FAILED', 'SAFE_FALLBACK', 'ERROR', 'TIMEOUT'}
    tasks = [t for t in response.task_results if t.status in failed_statuses]
    extensions = [t for t in response.extension_executions if t.status in failed_statuses]
    incomplete = bool(response.reliability and response.reliability.gates.get('analysis_completed') is False)
    traces = getattr(response, 'clarification_decision_traces', [])
    structured_stop = response.status == 'NEEDS_CLARIFICATION' and (
        any(t.source_stage == 'STRUCTURED_ASL_BINDING' for t in traces)
        or any(item.get('structured') for item in state.blockers)
    )
    if response.status not in failed_statuses | {'PARTIAL_SUCCESS'} and not tasks and not extensions and not incomplete and not structured_stop:
        return response
    outcome = {
        'status': response.status, 'error_code': redact(response.error_code or ''),
        'failed_tasks': [{'task_id': t.task_id, 'question': redact(t.question, 500), 'status': t.status, 'reported_reason': redact(t.answer, 600)} for t in tasks],
        'completed_tasks': [t.task_id for t in response.task_results if t.status == 'COMPLETED'],
        'extension_errors': [{'name': t.name, 'status': t.status, 'error': redact(t.error or '', 600)} for t in extensions],
        'warnings': [redact(w, 500) for w in (response.reliability.warnings if response.reliability else [])][-8:],
        'reported_answer': redact(response.answer, 1000) if response.status in failed_statuses else '',
        'parameter_diagnostics': bounded_detail({
            'missing_slots': getattr(response, 'missing_slots', []),
            'clarification_questions': getattr(response, 'clarification_questions', []),
            'decisions': [t.model_dump(mode='json') for t in traces],
            'requirements': [r.model_dump(mode='json') for r in getattr(response, 'requirements', [])],
            'awaiting_task_ids': getattr(response, 'awaiting_task_ids', []),
        }),
    }
    fallback = response.answer or '本次任务未完成，现有错误记录还不足以确定具体原因。'
    message = await _explain(state, settings, fallback, outcome)
    if message == redact(fallback, 500):
        return response
    updated = response.model_copy(deep=True)
    # Partial data, tables, files and status stay intact; explanation is only presentation.
    partial = response.status not in failed_statuses or bool(outcome['completed_tasks']) or bool(response.files)
    if structured_stop and not outcome['completed_tasks'] and not response.files:
        # 原说明可能含参数 JSON；最终回答只给通俗解释，候选和恢复信息留在原字段。
        updated.answer = message
    else:
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
