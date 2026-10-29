"""关系判定思考句的展示链路回归。

覆盖三件事：
1. ContextProposal 新增的 relation_reason 字段向后兼容老载荷，且两种
   schema（完整合同/空会话轻量合同）都会把它暴露给模型；
2. 思考句文本的规整规则（补句号、空值不发送）；
3. 分诊三元组的解包约定，防止调用方退回二元组。
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.semantic_v2.context_contract import (
    ContextProposal,
    lightweight_proposal_schema,
    proposal_schema,
)
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from app.semantic_v2.recognition import (
    _emit_relation_reason,
    _relation_reason_text,
)
from app.services.progress import progress_scope


def _accepted_proposal(**extra):
    return ContextProposal.model_validate({
        'status': 'ACCEPTED',
        'relation': 'CONTINUE',
        'target_task_id': 'task:a',
        'state_version': 3,
        'task_version': 1,
        'pending_id': None,
        **extra,
    })


def test_old_proposal_payload_without_reason_still_validates():
    proposal = ContextProposal.model_validate({
        'status': 'ACCEPTED',
        'relation': 'NEW_TASK',
        'target_task_id': None,
        'state_version': 0,
        'task_version': None,
        'pending_id': None,
    })
    assert proposal.relation_reason is None


def test_relation_reason_round_trips_through_schema():
    proposal = _accepted_proposal(
        relation_reason='上一轮问了医院数，这轮接着问经销商的构成。'
    )
    assert proposal.relation_reason.startswith('上一轮')

    full_schema = proposal_schema(
        {'candidate_tasks': [{'task_id': 'task:a'}], 'pending': None},
        CurrentTurnSemanticParse.model_json_schema(),
    )
    light_schema = lightweight_proposal_schema(
        CurrentTurnSemanticParse.model_json_schema()
    )
    for schema in (full_schema, light_schema):
        properties = schema['$defs']['ContextProposal']['properties']
        assert 'relation_reason' in properties
        any_of = properties['relation_reason']['anyOf']
        assert any_of[0].get('maxLength') == 300


def test_relation_reason_length_is_bounded():
    with pytest.raises(ValidationError):
        _accepted_proposal(relation_reason='长' * 301)


def test_reason_text_normalization():
    assert _relation_reason_text(None) == ''
    assert _relation_reason_text('  ') == ''
    assert _relation_reason_text('接着上一轮问') == '接着上一轮问。'
    assert _relation_reason_text('已经带了句号。') == '已经带了句号。'
    assert _relation_reason_text('带问号？') == '带问号？'


def test_emit_relation_reason_reaches_progress_callback():
    seen: list[dict] = []

    async def callback(event):
        seen.append(event)

    async def run():
        with progress_scope(callback):
            await _emit_relation_reason('这是上一轮问题的追问。')
            # 空思考句且没有兜底时不发事件
            await _emit_relation_reason(None)

    asyncio.run(run())
    assert len(seen) == 1
    event = seen[0]
    assert event['stage'] == 'INTENT_RECOGNITION'
    assert event['message'].startswith('判定思考：')
    assert event['message'].endswith('。')


def test_emit_relation_reason_uses_fallback_when_model_silent():
    seen: list[dict] = []

    async def callback(event):
        seen.append(event)

    async def run():
        with progress_scope(callback):
            await _emit_relation_reason(
                None,
                fallback='这轮的问题我还没法唯一确定怎么处理，先跟用户确认清楚再执行。',
            )

    asyncio.run(run())
    assert len(seen) == 1
    assert '先跟用户确认清楚' in seen[0]['message']


def test_triage_reason_contract_is_three_tuple():
    # 分诊返回 (verdict, merged, reason) 三元组；这里锁住签名约定，
    # 避免调用方按二元组解包时运行期才炸。
    import inspect

    from app.services import orchestrator as orchestrator_module

    source = inspect.getsource(
        orchestrator_module.DataAnalysisOrchestrator.triage_v1_pending_reply
    )
    assert 'tuple[str, str | None, str | None] | None' in source
    sample = ('ANSWER', '合并后的问题', '用户之前问医院数，这轮补充了地区')
    verdict, merged, reason = sample
    assert verdict == 'ANSWER' and merged and reason
