from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest, ChatRequest, DataQueryResult, Dataset,
    MetricRef, PrimaryIntent, TrustedIdentity, AnalysisOperator,
)
from app.presentation.root_report import render_root_report
from app.presentation.root_report import apply_requested_root_charts
from app.analysis.visualization import requested_chart_specs, render_chart_svg
from app.presentation.summary import brief_summary
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from test_analysis_orchestration import service


class FinalSummaryModel:
    async def synthesize(self, request, analysis, evidence, **kwargs):
        assert request.rewritten_question == '比较上海和江苏的订单笔数，用柱状图展示'
        assert len(analysis.facts['query_data']['rows']) == 2
        return '详细分析留在洞察节点，不重复进入最终回答。', SimpleNamespace(
            claims=[], final_answer={'overview': '上海订单35,206笔，江苏3,762笔；上海更多。'})


async def complete(question, *, rewritten=None, rows=None, synthesizer=None,
                   intent=PrimaryIntent.METRIC_QUERY, dispatcher=None, mcp=None, operators=None):
    rows = rows if rows is not None else [
        {'省份名称': '上海市', '省份（编码）': 310000, '订单笔数': 35206},
        {'省份名称': '江苏省', '省份（编码）': 320000, '订单笔数': 3762},
    ]
    request = CanonicalAnalysisRequest(
        conversation_id='final-output-test', tenant_id='t', user_id='u',
        original_question=question, rewritten_question=rewritten,
        primary_intent=intent, metrics=[MetricRef(input='订单笔数')],
        operators=operators or [],
    )
    chat = ChatRequest(application_id='app', conversation_id=request.conversation_id,
        message_id='m1', question=question, semantic_model_id=1, mcp=mcp or [])
    result = DataQueryResult(asl={'metrics': [{'name': 'order_count', 'alias': '订单笔数'}]},
        sql='SELECT approved_columns FROM approved_view',
        dataset=Dataset(columns=list(rows[0]) if rows else ['省份名称', '订单笔数'],
                        rows=rows, row_count=len(rows), snapshot_id='validated',
                        data_as_of=datetime.now(timezone.utc)))
    before = deepcopy(result.dataset.rows)
    progress = []
    with progress_scope(progress.append):
        response = await service(synthesizer=synthesizer, extension_dispatcher=dispatcher)._complete_query_result(
            chat, TrustedIdentity(tenant_id='t', user_id='u'), request, result)
    if rows:
        stages = [event['stage'] for event in progress]
        assert stages.index('RELIABILITY_CHECK') < stages.index('INSIGHT_ANALYSIS')
        assert all('<svg' not in event['message'] for event in progress)
    assert result.dataset.rows == before
    return response


@pytest.mark.asyncio
async def test_duplicate_ranking_note_is_omitted_but_rows_chart_and_diagnostic_survive():
    response = await complete('查询订单笔数最高的商品，用柱状图展示',
        rows=[{'商品名称': '同名商品', '商品编码': 'A', '订单笔数': 100},
              {'商品名称': '同名商品', '商品编码': 'B', '订单笔数': 80}],
        intent=PrimaryIntent.COMPARISON_ANALYSIS, operators=[AnalysisOperator.TOP_N])
    assert response.status == 'PARTIAL_SUCCESS'
    assert '分析说明：' not in response.answer and '排名对象为空或重复' not in response.answer
    assert '| A |' in response.answer and '| B |' in response.answer
    assert response.chart_specs and '<svg' in response.answer
    assert any('排名对象为空或重复' in warning for warning in response.reliability.warnings)
    assert response.answer.startswith('分析结果如下：')


@pytest.mark.asyncio
async def test_single_final_uses_short_summary_of_completed_question_and_data():
    response = await complete('改成柱状图',
        rewritten='比较上海和江苏的订单笔数，用柱状图展示', synthesizer=FinalSummaryModel())
    assert '上海订单35,206笔，江苏3,762笔；上海更多。' in response.answer
    assert '详细分析留在洞察节点' not in response.answer
    assert response.chart_specs[0].chart_type == 'BAR'
    assert response.chart_specs[0].y_fields == ['订单笔数']
    assert '<svg' in response.answer
    assert response.answer.startswith('分析结果如下：')


@pytest.mark.asyncio
async def test_metric_table_starts_with_completed_question_description():
    response = await complete('总数量是多少？',
        rewritten='2025年上海市所有医院的销售订单总数量是多少？',
        rows=[{'销售总数量': 16397267}])
    # STALE_TEST: user requested fluent leads, not a question fragment plus suffix.
    # Keep the completed-question/data summary and table contracts above/below.
    assert response.answer.startswith('查询结果如下：\n\n')
    assert '| 销售总数量 |' in response.answer and '16,397,267' in response.answer
    assert response.answer.count('结果如下：') == 1


@pytest.mark.asyncio
async def test_product_ranking_description_precedes_analysis_and_table():
    response = await complete('销售额最高的商品是什么？',
        rows=[{'商品名称': '商品甲', '销售额': 100}, {'商品名称': '商品乙', '销售额': 80}],
        intent=PrimaryIntent.COMPARISON_ANALYSIS)
    assert response.answer.startswith('分析结果如下：\n\n')
    assert '商品甲' in response.answer and '商品乙' in response.answer
    assert response.answer.count('结果如下：') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('word,kind', [('柱状图', 'BAR'), ('条形图', 'BAR'),
    ('折线图', 'LINE'), ('饼图', 'PIE'), ('散点图', 'SCATTER'), ('树状图', 'TREE')])
async def test_explicit_chart_is_generated_for_plain_metric_queries(word, kind):
    rows = [{'月份': '2025-10', '销量': 12, '金额': 30}, {'月份': '2025-11', '销量': 20, '金额': 60}]
    response = await complete('查询销量和金额，用' + word + '展示', rows=rows)
    assert response.chart_specs and all(spec.chart_type == kind for spec in response.chart_specs)
    assert '<svg' in response.answer


def test_root_summary_fallback_is_not_empty_template_when_model_omits_metadata():
    answer, _ = render_root_report('查询上海订单笔数', [{
        'task_id': 'result', 'status': 'COMPLETED', 'question': '统计订单',
        'summary': '上海订单笔数为35,206。', 'facts': {},
        'presentation': {'table': '| 订单笔数 |\n| --- |\n| 35206 |'},
    }], None)
    assert '上海订单笔数为35,206。' in answer
    assert answer.startswith('查询结果如下：\n\n')


@pytest.mark.parametrize('word', ['柱状图', '柱形图', '条形图', '折线图', '饼图',
    '饼状图', '树状图', '树形图', '层级图', 'bar chart', 'line chart', 'pie chart', 'tree diagram'])
def test_requested_chart_aliases_use_only_real_measures(word):
    rows = [{'名称': '甲', '编码': 123456, '销量': '12.25'}, {'名称': '乙', '编码': 999999, '销量': '8.5'}]
    specs, notes = requested_chart_specs('用' + word + '展示销量', list(rows[0]), rows, {})
    assert specs and all(spec.y_fields == ['销量'] for spec in specs)
    assert all(render_chart_svg(spec) for spec in specs)
    assert rows[0]['编码'] == 123456


@pytest.mark.parametrize('question', ['不要柱状图', '不要生成柱状图', '无需图表', '仅用表格'])
def test_negative_preferences_disable_automatic_charts(question):
    assert requested_chart_specs(question, ['名称', '销量'], [{'名称': '甲', '销量': 1}], {}) == ([], [])


def test_negative_preference_does_not_hide_positive_replacement():
    specs, _ = requested_chart_specs('不要柱状图，改成折线图', ['月份', '销量'],
        [{'月份': '2025-10', '销量': 1}], {})
    assert specs[0].chart_type == 'LINE'


@pytest.mark.parametrize('rows', [[{'名称': '甲', '销量': -1}],
    [{'名称': '甲', '销量': 0}], [{'名称': str(i), '销量': i + 1} for i in range(9)]])
def test_explicit_invalid_pie_explains_without_switching_to_bar(rows):
    specs, notes = requested_chart_specs('用饼图展示销量', list(rows[0]), rows, {})
    assert specs == [] and notes and '未替换成柱状图' in notes[0]


@pytest.mark.parametrize('question,columns,rows', [
    ('用柱状图展示', ['省份', '省份编码'], [{'省份': '上海', '省份编码': 310000}]),
    ('用散点图展示销量', ['名称', '销量'], [{'名称': '甲', '销量': 1}]),
    ('用柱状图展示', ['名称', '销量', '金额'], [{'名称': '甲', '销量': 1, '金额': 3}]),
    ('用雷达图展示销量', ['名称', '销量'], [{'名称': '甲', '销量': 1}]),
    ('用树状图展示', ['编码'], [{'编码': 123}]),
    ('用树状图展示销量', ['名称', '销量'], [{'名称': '甲', '销量': 1}, {'名称': '甲', '销量': 2}]),
    ('用折线图展示销量', ['月份', '销量'], [{'月份': '2025-10', '销量': 1}, {'月份': '2025-10', '销量': 2}]),
])
def test_inadequate_data_is_explained_without_inventing_values(question, columns, rows):
    before = deepcopy(rows)
    specs, notes = requested_chart_specs(question, columns, rows, {})
    assert specs == [] and notes
    assert rows == before


def test_default_intent_charts_are_untouched_without_explicit_preference():
    assert requested_chart_specs('分析销量趋势', ['月份', '销量'], [{'月份': '2025-10', '销量': 1}], {}) == (None, [])


def test_output_requirement_and_mixed_unsupported_chart_are_not_lost():
    specs, notes = requested_chart_specs('查询销量', ['名称', '销量'], [{'名称': '甲', '销量': 1}], {},
        output_requirement='用柱状图和雷达图展示')
    assert specs[0].chart_type == 'BAR' and '雷达图' in notes[0]


def test_explicit_tree_level_order_is_honored_without_reordering_query_data():
    rows = [{'医院': '甲医院', '省份': '上海', '销量': 10}]
    specs, _ = requested_chart_specs('按省份、医院分层，用树状图展示销量', list(rows[0]), rows, {})
    assert specs[0].data[0]['path'] == ['上海', '甲医院']
    assert list(rows[0]) == ['医院', '省份', '销量']


def test_tree_preserves_paths_values_and_escapes_without_parent_sums():
    rows = [{'省份': '上海', '医院': '<script>alert(1)</script>', '覆盖率': '40.50%'},
            {'省份': '上海', '医院': '乙', '覆盖率': '24.79%'}]
    specs, notes = requested_chart_specs('用树状图展示各省医院覆盖率', list(rows[0]), rows, {})
    svg = render_chart_svg(specs[0]).decode()
    assert '40.5%' in svg and '24.79%' in svg
    assert '65.29%' not in svg and '<script>' not in svg and '&lt;script&gt;' in svg
    assert specs[0].data[0]['path'] == ['上海', '<script>alert(1)</script>']
    assert '父节点未累加指标' in notes[0]


def test_tree_without_metric_displays_real_named_paths():
    rows = [{'省份': '上海', '医院': '甲医院'}, {'省份': '江苏', '医院': '乙医院'}]
    specs, _ = requested_chart_specs('用树状图展示医院名单', list(rows[0]), rows, {})
    assert specs[0].y_fields == []
    assert '甲医院' in render_chart_svg(specs[0]).decode()


@pytest.mark.parametrize('word', ['柱状图', '折线图'])
def test_multiple_group_dimensions_are_visible_not_silently_overlaid(word):
    rows = [{'月份': '2025-10', '省份': '上海', '医院': '甲', '销量': 1},
            {'月份': '2025-10', '省份': '上海', '医院': '乙', '销量': 2}]
    before = deepcopy(rows)
    specs, _ = requested_chart_specs('用' + word + '展示销量', list(rows[0]), rows, {})
    assert specs and '甲' in render_chart_svg(specs[0]).decode() and '乙' in render_chart_svg(specs[0]).decode()
    assert rows == before


def test_derived_root_charts_use_selected_full_data_not_inputs_or_preview():
    rows = [{'名称': '样本' + str(i), '合作医院数': i, '覆盖率': str(i / 10) + '%'} for i in range(210)]
    items = [{'task_id': 'input', 'status': 'COMPLETED', 'facts': {}, 'presentation': {'chart': 'input-chart'}},
        {'task_id': 'coverage', 'status': 'COMPLETED', 'facts': {'query_data': {'columns': list(rows[0]), 'rows': rows}},
         'presentation': {'table': 'only-20-rows'}}]
    before = deepcopy(rows)
    renderer = object.__new__(DataAnalysisOrchestrator)
    specs = apply_requested_root_charts('统计覆盖率，用柱状图展示', items, ['coverage'], renderer._render_inline_charts)
    assert len(specs[0].data) == 200 and specs[0].data_truncated
    assert specs[0].y_fields == ['覆盖率']
    assert items[0]['presentation']['chart'] == 'input-chart'
    assert '样本199' in items[1]['presentation']['chart']
    assert 'only-20-rows' == items[1]['presentation']['table'] and rows == before


@pytest.mark.asyncio
async def test_chart_override_and_multiple_requested_types_work_for_analysis():
    response = await complete('分析各地区订单笔数，用柱状图和树状图展示', intent=PrimaryIntent.COMPARISON_ANALYSIS)
    assert {spec.chart_type for spec in response.chart_specs} == {'BAR', 'TREE'}
    assert response.answer.count('<svg') == 2
    assert '35,206' in response.answer


@pytest.mark.asyncio
async def test_empty_results_do_not_invent_summary_or_chart():
    response = await complete('用柱状图展示订单笔数', rows=[])
    assert response.answer.startswith('查询结果如下：\n\n')
    assert response.chart_specs == []
    assert '没有' in response.answer or '0 条' in response.answer
    assert '<svg' not in response.answer


@pytest.mark.asyncio
async def test_mcp_success_for_second_metric_does_not_hide_first_chart_or_tree():
    from app.domain.models import ExtensionExecution, McpConfig
    from app.services.extension_dispatcher import ExtensionDispatcher
    class Dispatcher:
        async def execute_visualizations(self, **kwargs):
            return [ExtensionExecution(name='generate_column_chart', kind='MCP_TOOL', status='COMPLETED',
                result_metadata={'chart_spec_index': 1}, output={'url': 'https://charts.example/amount.png'})]
        visualization_url = staticmethod(ExtensionDispatcher.visualization_url)
        async def execute(self, **kwargs):
            return []
    response = await complete('查询销量和金额，用柱状图和树状图展示',
        rows=[{'名称': '甲', '销量': 10, '金额': 20}, {'名称': '乙', '销量': 11, '金额': 22}],
        dispatcher=Dispatcher(), mcp=[McpConfig(mcp_server_url='https://charts.example/sse')])
    assert '![金额柱状图](https://charts.example/amount.png)' in response.answer
    assert '![销量柱状图](https://charts.example/amount.png)' not in response.answer
    assert response.answer.count('<svg') == 2
    assert len(response.chart_specs) == 3


def test_summary_never_copies_tables_images_or_links():
    assert brief_summary('| 名称 |\n| --- |') == ''
    assert brief_summary('![图](https://charts.example/image.svg)') == ''
    assert brief_summary('金额从100增至125。共两期。未分析原因。多余详细段落。') == '金额从100增至125。共两期。未分析原因。'


@pytest.mark.asyncio
async def test_single_model_requests_final_summary_without_extra_model_call():
    from app.analysis.synthesis import QwenAnalysisSynthesizer
    from test_analysis_synthesis import settings, transport_for, request, analysis, evidence
    calls = []
    _, result = await QwenAnalysisSynthesizer(settings(), transport_for({
        'claims': [{'statement': '详细解释。'}], 'final_answer': {'overview': '简短回答。'}}, calls)).synthesize(request(), analysis(), evidence())
    assert result.final_answer['overview'] == '简短回答。' and len(calls) == 1
    assert '以 completed_question' in calls[0]['messages'][0]['content']
    assert '1至3句话' in calls[0]['messages'][0]['content']
