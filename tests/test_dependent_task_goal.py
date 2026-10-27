"""A predecessor's ranking measure is context, not a new calculation target."""
import copy
import json

import pytest

from app.domain.models import PrimaryIntent
from app.planning.task_dag import _parse_metric_catalog, _parse_metric_dependencies
from app.planning.task_plan_stages import TaskBoundary, boundary_targets, complete_boundaries
from test_task_planning_stages import SPEC, setup_planner


QUESTION = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'
METRIC = '平均每单销售金额'
CATALOG = _parse_metric_catalog(SPEC)
DEPENDENCIES = _parse_metric_dependencies(SPEC)


def expand(child, **parent_updates):
    parent = TaskBoundary(question='查询2025年平均每单销售金额前五名业务员',
        expected_output='2025年平均每单销售金额最高的五名业务员及编码', **parent_updates)
    original = [parent, child]
    before = copy.deepcopy(original)
    result = complete_boundaries(original, spec=SPEC, catalog=CATALOG,
        dependencies=DEPENDENCIES, source_question=QUESTION)
    assert original == before
    return result


@pytest.mark.parametrize('reference', ['平均每单销售金额', '每单平均金额'])
@pytest.mark.parametrize('output', ['销售员姓名、销售员编码、国药公司名称',
    '前序选出的五位业务员及其所属公司名单'])
def test_company_lookup_does_not_repeat_predecessor_formula(reference, output):
    child = TaskBoundary(question=f'获取2025年{reference}前五名业务员所属销售公司',
        name='获取前五名业务员所属销售公司', depends_on=[0], expected_output=output)
    result = expand(child)
    assert len(result) == 4
    assert [task.metric_targets for task in result] == [
        ['含税销售总额'], ['订单笔数'], [METRIC], []]
    assert [task.depends_on for task in result] == [[], [], [0, 1], [2]]
    assert result[-1].question == child.question
    assert not result[-1].component_input
    assert '不再查询数据库' not in result[-1].question


def test_child_can_query_a_different_measure_without_repeating_parent_formula():
    child = TaskBoundary(question='统计平均每单销售金额前五名业务员的销售总数量',
        depends_on=[0], expected_output='入选业务员的销售总数量')
    result = expand(child)
    assert len(result) == 4
    assert result[-1].metric_targets == ['销售总数量']
    assert result[-1].depends_on == [2]


def test_child_genuinely_recalculating_another_year_keeps_formula_dependencies():
    child = TaskBoundary(question='查询2025年平均每单销售金额前五名业务员在2024年的平均每单销售金额',
        depends_on=[0], expected_output='入选业务员2024年的平均每单销售金额')
    result = expand(child)
    assert len(result) == 6
    assert [t.metric_targets for t in result[3:]] == [['含税销售总额'], ['订单笔数'], [METRIC]]
    assert [t.depends_on for t in result[3:]] == [[2], [2], [3, 4]]
    assert all('2024年' in t.question for t in result[3:])


def test_dependent_ranking_that_requires_new_calculation_still_expands():
    parent = TaskBoundary(question='查询上海业务员名单', expected_output='上海业务员编码名单')
    child = TaskBoundary(question='查询前序入选业务员2025年平均每单销售金额的前五名',
        depends_on=[0], expected_output='按2025年平均每单销售金额降序选出的五名业务员及金额')
    result = complete_boundaries([parent, child], spec=SPEC, catalog=CATALOG, dependencies=DEPENDENCIES)
    assert len(result) == 4
    assert [t.depends_on for t in result] == [[], [0], [0], [1, 2]]
    assert result[-1].metric_targets == [METRIC]


@pytest.mark.parametrize('child', [
    TaskBoundary(question='计算平均每单销售金额，不再查询数据库', depends_on=[0], expected_output='计算结果'),
    TaskBoundary(question='查询2024年的平均每单销售金额', depends_on=[0]),
    TaskBoundary(question='解释平均每单销售金额', depends_on=[0], expected_output='口径说明', definition_only=True),
    TaskBoundary(question='统计新的成分', depends_on=[0], expected_output='成分数据', metric_targets=['含税销售总额']),
])
def test_local_definition_legacy_and_explicit_internal_targets_are_preserved(child):
    assert boundary_targets(child, CATALOG) == (child.metric_targets or [METRIC])


@pytest.mark.asyncio
async def test_real_planner_hands_four_tasks_to_extraction_and_preserves_company_query(monkeypatch):
    plan = {'tasks': [
        {'question': '查询2025年平均每单销售金额前五名业务员',
         'expected_output': '2025年平均每单销售金额最高的五名业务员及编码'},
        {'question': '获取平均每单销售金额前五名业务员所属销售公司',
         'name': '获取前五名业务员所属销售公司', 'depends_on': [0],
         'expected_output': '销售员姓名、销售员编码及所属销售公司名称'}]}

    def extract(request):
        assert len(request['tasks']) == 4
        tasks = []
        for t in request['tasks']:
            index = t['task_index']
            value = {'实体': ['业务员', '销售订单'] if index < 3 else ['业务员', '销售公司'],
                '指标': [{'name': name} for name in t['metric_targets']],
                '维度': ['业务员'] if index < 3 else [],
                '展示字段': [{'entity': '业务员', 'field': '销售员姓名'},
                    {'entity': '业务员', 'field': '销售员编码'}], '过滤条件': [],
                '时间粒度': {'time_range': '2025年'} if index < 3 else {},
                '排序': [{'field': METRIC, 'order': 'desc'}] if index == 2 else [],
                '限制': 5 if index == 2 else None,
                '是否查询数据库': '否' if index == 2 else '是'}
            if index == 3:
                value['展示字段'].append({'entity': '销售公司', 'field': '国药公司名称'})
            tasks.append({'task_index': index, 'extraction': value})
        return {'tasks': tasks}

    planner, calls = setup_planner(monkeypatch, plan, extract=extract)
    result = await planner.plan_question(QUESTION, semantic_model_id=130, root_intent=PrimaryIntent.DETAIL_QUERY)
    assert len(calls) == 2
    assert len(result.tasks) == 4
    assert all(t.intent == PrimaryIntent.DETAIL_QUERY for t in result.tasks)
    assert [t.depends_on for t in result.tasks] == [(), (), (0, 1), (2,)]
    assert result.tasks[2].structured['限制'] == 5
    assert result.tasks[2].structured['计算绑定']['denominator']['column'] == '订单笔数'
    company = result.tasks[-1]
    assert company.structured['指标'] == []
    assert company.structured['是否查询数据库'] == '是'
    assert not company.structured.get('计算绑定')
    assert company.structured['限制'] is None
    assert company.structured['展示字段'][-1] == {'entity': '销售公司', 'field': '国药公司名称'}
    schema = json.loads(calls[0]['messages'][0]['content'].split('JSON Schema：', 1)[1])
    assert 'extraction' not in str(schema) and 'metric_targets' not in str(schema)
