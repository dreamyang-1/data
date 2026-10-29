"""Calculator decisions must be fixed before query-parameter extraction."""
import json

import pytest

from app.domain.models import PrimaryIntent
from app.planning.calculation_plan import plan_calculations
from app.planning.task_plan_stages import TaskBoundary
from test_task_planning_stages import setup_planner, SPEC


@pytest.mark.asyncio
@pytest.mark.parametrize('sort_spelling', ['query_slots', 'calculator_operation'])
async def test_company_ranking_formula_and_inputs_cannot_be_rewritten_by_extraction(monkeypatch, sort_spelling):
    question = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'
    plan = {'tasks': [
        {'question': '统计2025年各业务员平均每单销售金额最高的五人，返回销售员编码',
         'expected_output': '平均每单销售金额最高的五名业务员及销售员编码'},
        {'question': '根据前序入选业务员编码查询所属销售公司', 'depends_on': [0],
         'expected_output': '入选业务员编码及公司名称',
         'dependency_bindings': [{'source_task_index': 0, 'source_column': '业务员.销售员编码'}]}]}
    stages = []

    def calculate(fixed):
        stages.append('calculation')
        assert fixed['calculation_task_indices'] == [2]
        assert len(fixed['tasks']) == 4 and fixed['tasks'][3]['depends_on'] == [2]
        assert fixed['registered_bindings']['2']['numerator']['source_task_index'] == 0
        ranking = {'排序': [{'field': '平均每单销售金额', 'order': 'desc'}], '限制': 5}
        if sort_spelling == 'calculator_operation':
            ranking = {'排序': [{'type': 'sort_limit', 'field': '平均每单销售金额',
                                 'descending': True, 'count': 5}], '限制': None}
        return {'tasks': [{'task_index': 2, 'calculation': ranking}]}

    def extract(fixed):
        stages.append('extraction')
        assert stages == ['calculation', 'extraction']
        assert fixed['calculation_task_indices'] == [2]
        return {'tasks': [{'task_index': t['task_index'], 'extraction': {
            '指标': [{'name': name} for name in t['metric_targets']],
            '展示字段': [{'entity': '业务员', 'field': '销售员编码'}], '维度': ['业务员'],
            '是否查询数据库': '是', '计算绑定': {'operation': 'multiply'},
            '结果操作': {'source_task_index': 99}, '限制': 1, '排序': []}}
            for t in fixed['tasks']]}

    planner, calls = setup_planner(monkeypatch, plan, extract=extract, calculate=calculate)
    result = await planner.plan_question(question, semantic_model_id=130, root_intent=PrimaryIntent.DETAIL_QUERY)
    assert len(calls) == 3 and len(result.tasks) == 4
    calc = result.tasks[2].structured
    assert calc['计算绑定']['operation'] == 'divide'
    assert calc['计算绑定']['numerator']['source_task_index'] == 0
    assert calc['计算绑定']['denominator']['source_task_index'] == 1
    assert calc['限制'] == 5 and calc['排序'][0]['order'] == 'desc'
    assert '结果操作' not in calc and calc['是否查询数据库'] == '否'
    assert result.tasks[3].depends_on == (2,)
    assert result.tasks[3].dependency_bindings[0]['source_task_index'] == 2
    assert all('计算绑定' not in result.tasks[i].structured for i in (0, 1, 3))


@pytest.mark.asyncio
async def test_extraction_can_omit_calculations_but_cannot_supply_a_different_operation(monkeypatch):
    plan = {'tasks': [
        {'question': '查询各订单含税销售总额，返回订单号'},
        {'question': '将前序每笔金额平方，不再查询数据库', 'depends_on': [0],
         'expected_output': '订单号及金额平方'}]}
    operation = {'operation': 'square', 'input': {'source_task_index': 0, 'column': '含税销售总额'},
                 'result_field': '金额平方', 'format': 'number'}
    planner, calls = setup_planner(monkeypatch, plan,
        calculate=lambda _: {'tasks': [{'task_index': 1, 'calculation': {'计算绑定': operation}}]},
        extract=lambda _: {'tasks': [{'task_index': 0, 'extraction': {
            '指标': [{'name': '含税销售总额'}], '维度': ['订单号'], '是否查询数据库': '是'}}]})
    result = await planner.plan_question('查询每笔订单金额的平方', semantic_model_id=130,
                                         root_intent=PrimaryIntent.METRIC_QUERY)
    assert result.tasks[1].structured['计算绑定'] == operation
    extraction_prompt = calls[-1]['messages'][0]['content'].split('已规划任务JSON：')[0]
    assert 'derive双操作数' not in extraction_prompt
    assert '最多5步pipeline' not in extraction_prompt


@pytest.mark.asyncio
async def test_multistep_formula_is_planned_as_a_complete_pipeline_before_extraction():
    spec = SPEC + '\n| 均方根 | 无 | 先按订单金额平方后求平均再开根 | 元 | 复合口径指标 | 含税销售总额 |'
    boundaries = [TaskBoundary(question='查询各订单含税销售总额', metric_targets=['含税销售总额']),
                  TaskBoundary(question='计算均方根，不再查询数据库', depends_on=[0], metric_targets=['均方根'])]
    operations = [
        {'type': 'derive', 'operator': 'square', 'field': '含税销售总额', 'result_field': '平方'},
        {'type': 'aggregate', 'group_by': [], 'aggregations': [{'field': '平方', 'function': 'avg', 'alias': '均方'}]},
        {'type': 'derive', 'operator': 'sqrt', 'field': '均方', 'result_field': '均方根'}]

    async def request_model(name, prompt, question):
        assert name == 'single-question-calculation-plan'
        fixed = json.loads(prompt.split('计算规划输入JSON：\n')[1])
        assert fixed['registered_bindings'] == {}
        return {'tasks': [{'task_index': 1, 'calculation': {'结果操作': {
            'source_task_index': 0, 'operation': {'type': 'pipeline', 'operations': operations}}}}]}

    result = await plan_calculations(boundaries, question='计算订单金额均方根', system_content=spec,
        spec=spec, catalog=(frozenset(['均方根', '含税销售总额']), {}),
        dependencies={'均方根': ['含税销售总额']}, request_model=request_model)
    assert result[1]['结果操作']['operation']['operations'] == operations

    # Verify that the planner's contract reaches the real deterministic tool.
    from app.domain.models import AtomicTask
    from app.planning.calculation_bindings import materialize_computation_binding
    from app.planning.calculation_plan import planned_calculation_parameters
    from app.services.calculation_execution import calculate
    from test_basic_arithmetic_contract import reference
    parameters = planned_calculation_parameters(boundaries[1], result[1])
    parameters = materialize_computation_binding(parameters, task_index=1, dependencies=['task-1'])
    rows = [{'订单号': 'A', '含税销售总额': 3}, {'订单号': 'B', '含税销售总额': 4}]
    ref = reference('samples', list(rows[0]), rows)
    task = AtomicTask(task_id='task-2', question=boundaries[1].question,
                      depends_on=['task-1'], extraction=parameters)
    _, values, _, _, _ = calculate(task, {'task-1': (ref, rows)}, lambda *_: None)
    assert values[0]['均方根'] == pytest.approx((12.5) ** 0.5)
