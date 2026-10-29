"""Exercise actual engine, delivery, root report and frozen progress order."""
import copy
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.analysis.engine import AnalysisEngine
from app.domain.models import AgentResponse, EvidenceItem, PlannerExtraction, PrimaryIntent as I
from app.services.progress import emit_progress, progress_scope
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.planning.compound_question import CompoundQuestionRouter
from app.presentation.analysis_details import trend_capability_text
from app.services.analysis_delivery import analysis_result_rows
from test_analysis_delivery_regressions import extraction, TODAY, METRIC
from test_complex_execution_correctness import service, context, persist, IDENTITY


async def deliver(monkeypatch, question, *, values=None, previous_year=None, period='2025年7月至12月'):
    agent = service()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    agent.analysis_engine = AnalysisEngine()
    monkeypatch.setattr(agent.sessions, 'delete_dag_checkpoint', AsyncMock())
    chat, _ = context()
    chat.question = question
    plan = build_analysis_task_plan(question, PlannerExtraction(intent=I.TREND_ANALYSIS,
        parameters=[], structured=extraction('趋势分析', period, ['业务日期'])), today=TODAY)
    values = values or [1000.0011, 2000.0022, 1500.0017, 250000000.1234, 60000000.0011, 72000000.0044]
    labels = [f'2025-{month:02}' for month in range(7, 13)]
    input_rows = [dict(zip(['业务日期', METRIC], pair)) for pair in zip(labels, values)]
    async def query(child, identity):
        window = child._planner_extraction.structured['时间粒度']['time_range']
        rows = copy.deepcopy((previous_year or []) if '2024' in window else input_rows)
        columns = ['业务日期', METRIC]
        ref = await persist(agent, child, columns, rows, conversation=child.conversation_id)
        child._dag_deferred_insight = {'summary': '已取得查询数据。',
            'facts': {'query_data': {'columns': columns, 'rows': rows}},
            'presentation': {'table': agent._markdown_result_table(columns, rows), 'chart': '', 'notes': []}}
        return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
            status='COMPLETED', intent=I.METRIC_QUERY, answer='已取得查询数据。', dataset_id=ref.dataset_id,
            evidence=[EvidenceItem(evidence_id='query', kind='QUERY_RESULT', source_ref='synthetic',
                payload={'row_count': len(rows)})])
    monkeypatch.setattr(agent, '_handle', query)
    # No model arithmetic; retain both deterministic insight fallback and the
    # final report, so their output policies are tested together.
    async def unavailable(*args, **kwargs):
        raise TimeoutError()
    agent.analysis_synthesizer = SimpleNamespace(synthesize_combined=unavailable)
    events = []
    with progress_scope(events.append):
        await emit_progress('INTENT_RECOGNITION', 'COMPLETED', 'test')
        await emit_progress('TASK_PLANNING', 'COMPLETED', 'test')
        response = await agent._handle_task_plan(chat, IDENTITY, plan)
    return response, events


def table_count(text):
    # Count Markdown separators, not the number of content rows.
    return sum(line.strip().startswith('|') and set(line.replace(' ', '')) <= set('|-:')
               for line in text.splitlines())


@pytest.mark.asyncio
async def test_independent_mom_has_one_final_table_and_no_progress_tables(monkeypatch):
    response, events = await deliver(monkeypatch, '对2025年7月到12月关联城市为上海市的销售总金额进行环比分析')
    assert all(task.status == 'COMPLETED' for task in response.task_results)
    assert table_count(response.answer) == 1
    assert '环比增减量' in response.answer and '环比变化率' in response.answer
    assert '250,000,000.1234' in response.answer
    assert '| 2025-07 | 1,000.0011 | — | — |' in response.answer
    assert 'e+08' not in response.answer and 'first_bucket_partial' not in response.answer
    assert '不可计算／不适用' not in response.answer
    messages = [event['message'] for event in events if event['stage'] in {'ANALYSIS_TOOL', 'INSIGHT_ANALYSIS'}]
    assert messages and all(table_count(message) == 0 for message in messages)
    assert any('时间完整性' in message and '未发现缺期' in message for message in messages)
    facts = next(item.payload['facts'] for item in response.evidence if item.kind == 'ANALYSIS_RESULT')
    assert facts['tool_selection'] == ['period_over_period_change']
    assert facts['period_over_period']['changes'][0]['change_rate'] == pytest.approx(1)
    assert len(facts['capability_results'][0]['rows']) == 5  # full audit details preserved
    order = {'INTENT_RECOGNITION': 0, 'TASK_PLANNING': 1, 'TASK_STATUS': 2, 'ANALYSIS_TOOL': 2,
             'RELIABILITY_CHECK': 3, 'INSIGHT_ANALYSIS': 4, 'FINAL_OUTPUT': 5}
    observed = [order[event['stage']] for event in events if event['stage'] in order]
    # This scheduler returns the final report; FINAL_OUTPUT is emitted by its
    # outer API caller. All five preceding main stages must occur in order.
    assert observed == sorted(observed) and set(observed) == set(range(5))


@pytest.mark.asyncio
async def test_full_trend_hides_unavailable_yoy_and_diagnostic_tables(monkeypatch):
    response, events = await deliver(monkeypatch, '分析2025年7月到12月的含税销售总额趋势')
    assert table_count(response.answer) == 1
    assert '同比增减量与变化率：不可计算' in response.answer
    table_lines = '\n'.join(line for line in response.answer.splitlines() if line.startswith('|'))
    assert '同比' not in table_lines
    assert all(key not in response.answer for key in ['basis_labels', 'decision_method', 'first_bucket_partial',
                                                     'theil_sen_pairwise_slope', '不可计算／不适用'])
    assert '方向拐点' in response.answer and '时间完整性' in response.answer
    assert '目标窗口' in response.answer and '完整时间桶走势' in response.answer
    assert all(table_count(e['message']) == 0 for e in events if e['stage'] == 'ANALYSIS_TOOL')


@pytest.mark.asyncio
async def test_computable_yoy_stays_in_the_single_combined_table(monkeypatch):
    history = [{'业务日期': f'2024-{month:02}', METRIC: 100} for month in range(7, 13)]
    response, _ = await deliver(monkeypatch, '分析2025年7月至12月含税销售总额的同比趋势', previous_year=history)
    assert table_count(response.answer) == 1
    assert '| 同比增减量 | 同比变化率 |' in response.answer
    assert '900.0011%' in response.answer


@pytest.mark.parametrize('status', ['NOT_COMPUTABLE', 'NOT_APPLICABLE'])
def test_unavailable_report_never_renders_placeholder_rows(status):
    report = {'name': '同比增减量与变化率', 'method': 'year_over_year', 'status': status,
              'reason': '缺少上年同期基期', 'rows': [{'current': 10, 'base': None}], 'columns': ['current', 'base']}
    assert '缺少上年同期基期' in trend_capability_text(report)
    assert '|' not in trend_capability_text(report)


def test_heterogeneous_summary_rows_have_explicit_scopes_and_chinese_prose():
    report = {'name': '整体趋势、极值与波动', 'method': 'theil_sen_pairwise_slope', 'status': 'COMPLETED',
        'rows': [{'start': 20000000, 'end': 6000000, 'absolute_change': -14000000,
                  'change_rate': -.7, 'minimum': 4000000, 'maximum': 20000000, 'point_count': 3},
                 {'direction': 'UP', 'robust_slope_per_period': 2000000, 'direction_consistency': 1,
                  'basis_labels': ['2025-11', '2025-12'], 'decision_method': 'internal'}]}
    before = copy.deepcopy(report)
    text = trend_capability_text(report)
    assert '首尾变化率：-70.0000%' in text and '完整时间桶走势：上升' in text
    assert '判定所用期间：2025-11、2025-12' in text
    assert '20,000,000' in text and '|' not in text and 'internal' not in text
    assert report == before


def test_temporal_gaps_and_partial_edges_are_prose_not_python_dicts():
    text = trend_capability_text({'name': '时间完整性', 'method': 'temporal_continuity', 'status': 'COMPLETED',
        'rows': [{'grain': 'month', 'is_complete': False, 'gaps': [{'from': '2025-07', 'to': '2025-09', 'intervals': 2}]},
                 {'first_bucket_partial': True, 'first_partial_label': '2025-07', 'last_bucket_partial': False}]})
    assert '2025-07至2025-09之间不连续' in text and '2025-07为残缺时间桶' in text
    assert all(token not in text for token in ['|', '{', 'False', 'intervals'])


@pytest.mark.parametrize('changes,status,expected', [
    ([], 'NOT_COMPUTABLE', False),
    ([{'to': '2025-07', 'absolute_change': None, 'change_rate': None}], 'COMPLETED', False),
    ([{'to': '2025-06', 'absolute_change': 10, 'change_rate': .1}], 'COMPLETED', False),
    ([{'to': '2025-07', 'absolute_change': 0, 'change_rate': None}], 'COMPLETED', True),
])
def test_unavailable_comparison_columns_hidden_without_mutating_facts(changes, status, expected):
    rows = [{'业务日期': '2025-07', METRIC: 100}]
    facts = {'result_scope': {'time_column': '业务日期', 'labels': ['2025-07']},
             'year_over_year': {'status': status, 'changes': changes}}
    before = copy.deepcopy((rows, facts))
    columns, output = analysis_result_rows(['业务日期', METRIC], rows, facts)
    assert ('同比增减量' in columns) is expected
    assert (rows, facts) == before
    if expected:
        assert output[0]['同比增减量'] == 0 and output[0]['同比变化率'] is None
