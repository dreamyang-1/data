"""Planning describes work; only extraction owns executable parameters."""
import copy
import json

import pytest

from app.domain.models import PrimaryIntent
from test_task_planning_stages import setup_planner


def query_parameters(**updates):
    result = {'意图': '指标查询', '业务域': [], '实体': ['销售订单', '商品科室关联'],
              '指标': [{'name': '含税销售总额'}], '维度': [], '展示字段': [],
              '过滤条件': [{'entity': '商品科室关联', 'field': '适用科室', 'op': '=', 'value': ['骨科']}],
              '排序': [], '限制': None, '时间粒度': {'unit': None, 'time_range': '最近12个月', 'anchor': None},
              '输出要求': None, '是否去重': None, '是否查询数据库': '是'}
    result.update(updates)
    return result


@pytest.mark.asyncio
async def test_planning_schema_has_only_task_description_and_dependencies(monkeypatch):
    planner, calls = setup_planner(monkeypatch, {'tasks': [{'question': '看看骨科那边卖得怎么样'}]},
        extract=lambda request: {'tasks': [{'task_index': 0, 'extraction': query_parameters()}]})
    await planner.plan_question('看看骨科那边卖得怎么样', semantic_model_id=130)
    schema = json.loads(calls[0]['messages'][0]['content'].split('JSON Schema：', 1)[1])
    properties = schema['$defs'][schema['properties']['tasks']['items']['$ref'].split('/')[-1]]['properties']
    assert set(properties) == {'question', 'name', 'depends_on', 'expected_output', 'definition_only'}


@pytest.mark.asyncio
@pytest.mark.parametrize('planned_metrics', [[], ['订单笔数']])
async def test_extracted_business_parameters_are_not_overwritten_by_plan(monkeypatch, planned_metrics):
    expected = query_parameters()
    original = copy.deepcopy(expected)
    plan = {'tasks': [{'question': '看看骨科那边卖得怎么样', 'metric_targets': planned_metrics,
        'grain': ['适用科室'], 'component_input': True, 'sample_requirements': ['订单笔数大于0'],
        'expected_output': '规划阶段交付说明', 'extraction': {'指标': [{'name': '订单笔数'}]},
        'postprocess': {'operation': {'type': 'limit', 'count': 1}}}]}
    planner, _ = setup_planner(monkeypatch, plan,
        extract=lambda request: {'tasks': [{'task_index': 0, 'extraction': expected}]})
    result = await planner.plan_question('看看骨科那边卖得怎么样', semantic_model_id=130)
    assert len(result.tasks) == 1
    assert result.tasks[0].structured == original
    assert expected == original


@pytest.mark.asyncio
async def test_orthopedics_filter_does_not_become_a_dimension(monkeypatch):
    expected = query_parameters(指标=[{'name': name} for name in ('含税销售总额', '销售总数量', '订单笔数')])
    planner, calls = setup_planner(monkeypatch, {'tasks': [{
        'question': '统计最近12个月骨科适用商品的销售情况',
        'metric_targets': ['含税销售总额', '销售总数量', '订单笔数'], 'grain': ['适用科室']}]},
        extract=lambda request: {'tasks': [{'task_index': 0, 'extraction': expected}]})
    result = await planner.plan_question('看看骨科那边卖得怎么样', semantic_model_id=130)
    assert result.tasks[0].structured == expected
    parameters_input = json.loads(calls[1]['messages'][0]['content'].split('已规划任务JSON：\n', 1)[1])
    assert parameters_input['tasks'][0]['grain'] == []


@pytest.mark.asyncio
async def test_explicit_empty_extraction_is_not_filled_from_business_wording(monkeypatch):
    expected = query_parameters(指标=[], 维度=[], 过滤条件=[], 时间粒度=None)
    planner, _ = setup_planner(monkeypatch, {'tasks': [{'question': '查询含税销售总额',
        'metric_targets': ['含税销售总额'], 'grain': ['经销商']}]},
        extract=lambda request: {'tasks': [{'task_index': 0, 'extraction': expected}]})
    result = await planner.plan_question('查询含税销售总额', semantic_model_id=130)
    assert result.tasks[0].structured == expected


@pytest.mark.asyncio
async def test_detail_label_does_not_erase_extracted_metrics_or_dimensions(monkeypatch):
    expected = query_parameters(意图='明细查询', 维度=['经销商'], 过滤条件=[], 时间粒度=None)
    planner, _ = setup_planner(monkeypatch, {'tasks': [{'question': '查看经销商销售情况'}]},
        extract=lambda request: {'tasks': [{'task_index': 0, 'extraction': expected}]})
    result = await planner.plan_question('查看经销商销售情况', semantic_model_id=130)
    assert result.tasks[0].structured == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('root_intent', [None, PrimaryIntent.DETAIL_QUERY])
async def test_local_operation_comes_from_extraction_without_planner_operation(monkeypatch, root_intent):
    operation = {'source_task_index': 0, 'operation': {'type': 'aggregate', 'group_by': [],
        'aggregations': [{'field': '订单笔数', 'function': 'variance_population', 'alias': '总体方差'}]}}
    plan = {'tasks': [{'question': '查询各经销商订单笔数'},
        {'question': '根据前序结果计算订单笔数的总体方差，不再查询数据库', 'depends_on': [0],
         'postprocess': {'source_task_index': 0, 'operation': {'type': 'limit', 'count': 1}}}]}
    def extract(request):
        return {'tasks': [{'task_index': 0, 'extraction': query_parameters(
            指标=[{'name': '订单笔数'}], 维度=['经销商'], 过滤条件=[])},
            {'task_index': 1, 'extraction': query_parameters(意图='指标计算', 指标=[], 过滤条件=[],
                是否查询数据库='否', 结果操作=operation)}]}
    planner, _ = setup_planner(monkeypatch, plan, extract=extract)
    result = await planner.plan_question('计算各经销商订单笔数的总体方差', semantic_model_id=130,
                                        root_intent=root_intent)
    assert len(result.tasks) == 2
    assert result.tasks[1].depends_on == (0,)
    assert result.tasks[1].structured['结果操作'] == operation
    assert result.tasks[1].structured['是否查询数据库'] == '否'
    if root_intent:
        assert result.intent is root_intent
        assert result.tasks[0].intent is root_intent


@pytest.mark.asyncio
async def test_component_queries_keep_the_parameters_extracted_for_each_scope(monkeypatch):
    extracted = []
    def extract(request):
        for task in request['tasks']:
            local = '不再查询数据库' in task['question']
            extracted.append(query_parameters(意图='指标计算' if local else '指标查询',
                指标=[{'name': name} for name in task['metric_targets']],
                维度=[] if local else ['经销商'], 展示字段=[{'entity': '经销商', 'field': '经销商名称'}],
                过滤条件=[{'entity': '经销商', 'field': '省份名称', 'op': '=', 'value': ['上海']}],
                排序=[], 限制=None, 是否查询数据库='否' if local else '是'))
        return {'tasks': [{'task_index': index, 'extraction': item} for index, item in enumerate(extracted)]}
    planner, _ = setup_planner(monkeypatch, {'tasks': [{'question': '统计上海各经销商平均每单销售金额'}]}, extract=extract)
    result = await planner.plan_question('统计上海各经销商平均每单销售金额', semantic_model_id=130)
    assert len(result.tasks) == 3
    assert result.tasks[-1].depends_on == (0, 1)
    for index, task in enumerate(result.tasks):
        for key in ('指标', '维度', '展示字段', '过滤条件', '排序', '限制', '时间粒度', '输出要求'):
            assert task.structured[key] == extracted[index][key]
    assert result.tasks[-1].structured['计算绑定']['numerator']['source_task_index'] == 0
