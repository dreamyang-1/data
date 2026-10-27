"""A full task plan must distinguish delivered metrics from downstream context."""
import copy

import pytest

from app.planning.task_dag import _parse_metric_catalog, _parse_metric_dependencies
from app.planning.task_plan_stages import TaskBoundary, boundary_targets, complete_boundaries


SPEC = '''| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |
| --- | --- | --- | --- | --- | --- |
| 含税销售总额 | 销售总额 | 含税金额合计 | 元 | 原子指标 | 无 |
| 订单笔数 | 订单数 | 订单号去重数 | 笔 | 原子指标 | 无 |
| 平均每单销售金额 | 订单平均金额 | 含税销售总额 ÷ 订单笔数 | 元/笔 | 复合口径指标 | 含税销售总额, 订单笔数 |
| 有效订单去重医院数 | 有效订单医院数 | 有效销售订单关联去重医院数 | 家 | 原子指标 | 无 |
| 区域全部医院总数 | 全区医院数量 | 区域内全部建档医院数 | 家 | 原子指标 | 无 |
| 区域医院覆盖率 | 经销商医院覆盖率 | 有效订单去重医院数 ÷ 区域全部医院总数 | % | 复合口径指标 | 有效订单去重医院数, 区域全部医院总数 |
'''
CATALOG = _parse_metric_catalog(SPEC)
DEPENDENCIES = _parse_metric_dependencies(SPEC)
QUESTION = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'


def expand(tasks, question=QUESTION):
    original = copy.deepcopy(tasks)
    result = complete_boundaries(tasks, spec=SPEC, catalog=CATALOG,
        dependencies=DEPENDENCIES, source_question=question)
    assert tasks == original
    return result


def company_plan(purpose='作为计算平均每单销售金额的分子成分', key='业务员ID'):
    # The complete four-task outline recorded in the failed real model run.
    return [
        TaskBoundary(question=f'查询2025年各业务员的含税销售总额，保留业务员姓名、{key}及对应金额数值，{purpose}',
            expected_output=f'2025年各业务员的姓名、{key}及含税销售总额'),
        TaskBoundary(question=f'查询2025年各业务员的订单笔数，保留业务员姓名、{key}及对应订单笔数数值，作为计算平均每单销售金额的分母成分',
            expected_output=f'2025年各业务员的姓名、{key}及订单笔数'),
        TaskBoundary(question=f'根据前序任务返回的2025年各业务员含税销售总额和订单笔数结果，不再查询数据库，按{key}对齐后计算平均每单销售金额（含税销售总额÷订单笔数），按该指标降序排列取前五位销售人员',
            depends_on=[0, 1], expected_output=f'2025年平均每单销售金额最高的前五位销售人员姓名及{key}'),
        TaskBoundary(question=f'根据前序任务返回的前五位销售人员姓名及{key}名单，查询这些人员所属的销售公司名称',
            depends_on=[2], expected_output=f'前五位销售人员的姓名、{key}及所属销售公司名称'),
    ]


@pytest.mark.parametrize('purpose', [
    '作为计算平均每单销售金额的分子成分',
    '是后续平均每单销售金额计算所需的数据',
    '这些数据将参与平均每单销售金额的计算',
    '为下一步的订单平均金额计算提供输入',
    '用于平均每单销售金额计算',
])
@pytest.mark.parametrize('key', ['业务员ID', '销售员编码'])
def test_complete_company_plan_is_not_expanded_again(purpose, key):
    source = company_plan(purpose, key)
    result = expand(source)
    assert len(result) == 4
    assert [t.depends_on for t in result] == [[], [], [0, 1], [2]]
    assert [t.metric_targets for t in result] == [
        ['含税销售总额'], ['订单笔数'], ['平均每单销售金额'], []]
    assert all(t.component_input for t in result[:2])
    assert [t.question for t in result] == [t.question for t in source]
    assert all(key in t.question for t in result)
    assert '2025年' not in result[-1].question


def test_formula_ready_dependencies_remain_local_without_a_magic_phrase():
    source = company_plan()
    source[2].question = source[2].question.replace('不再查询数据库，', '')
    result = expand(source)
    assert len(result) == 4
    assert result[2].depends_on == [0, 1]
    assert '不再查询数据库' in result[2].question
    assert '不再查询数据库' not in result[3].question


def test_coverage_components_with_formula_context_remain_three_tasks():
    source = [
        TaskBoundary(question='统计上海市区域医院覆盖率的分子，即最近12个月内上海市产生有效销售订单的去重医院数',
            expected_output='最近12个月上海市有效订单去重医院数数值'),
        TaskBoundary(question='统计上海市区域医院覆盖率的分母，即上海市区域内全部建档医院总数，包含暂无销售订单的医院',
            expected_output='上海市区域全部医院总数数值'),
        TaskBoundary(question='根据前序上海市结果计算区域医院覆盖率，不再查询数据库',
            expected_output='上海市区域医院覆盖率百分比数值', depends_on=[0, 1]),
    ]
    result = expand(source, '统计上海市区域医院覆盖率')
    assert len(result) == 3
    assert [t.metric_targets for t in result] == [
        ['有效订单去重医院数'], ['区域全部医院总数'], ['区域医院覆盖率']]
    assert result[2].depends_on == [0, 1]
    assert '最近12个月' not in result[1].question


def test_registered_aliases_identify_the_actual_delivery():
    task = TaskBoundary(question='查询销售总额，作为订单平均金额计算的输入',
        expected_output='上海市业务员销售总额')
    assert boundary_targets(task, CATALOG) == ['含税销售总额']


def test_two_business_goals_still_expand_to_four_company_tasks():
    source = [
        TaskBoundary(question='统计2025年各业务员的平均每单销售金额，降序取前五，返回姓名及销售员编码',
            expected_output='2025年平均每单销售金额最高的前五位业务员及销售员编码'),
        TaskBoundary(question='根据前序平均每单销售金额最高的五名业务员查询其所属销售公司',
            expected_output='入选业务员姓名、销售员编码及所属销售公司名称', depends_on=[0]),
    ]
    result = expand(source)
    assert len(result) == 4
    assert result[2].depends_on == [0, 1]
    assert result[3].depends_on == [2]
    assert result[3].metric_targets == []


@pytest.mark.parametrize('delivery', [None, '', '返回逐人完整排名结果'])
def test_unspecified_metric_delivery_still_expands_a_requested_formula(delivery):
    result = expand([TaskBoundary(question='查询2025年各业务员的平均每单销售金额',
        expected_output=delivery)])
    assert len(result) == 3
    assert result[-1].metric_targets == ['平均每单销售金额']
    assert result[-1].depends_on == [0, 1]


def test_new_year_calculation_is_not_mistaken_for_prior_ranking_context():
    source = company_plan()
    source[-1] = TaskBoundary(question='根据2025年入选的五位业务员，查询其2024年的平均每单销售金额',
        expected_output='入选业务员2024年的平均每单销售金额', depends_on=[2])
    result = expand(source)
    assert len(result) == 6
    assert all('2024年' in t.question for t in result[3:])
    assert [t.depends_on for t in result[3:]] == [[2], [2], [3, 4]]


def test_independent_periods_are_never_merged_just_by_metric_name():
    result = expand([
        TaskBoundary(question='查询上海2024年平均每单销售金额', expected_output='上海2024年平均每单销售金额'),
        TaskBoundary(question='查询北京2025年平均每单销售金额', expected_output='北京2025年平均每单销售金额'),
    ], '分别查询上海2024年和北京2025年平均每单销售金额')
    assert len(result) == 6
    assert result[2].depends_on == [0, 1] and result[5].depends_on == [3, 4]
    assert all('上海2024年' in t.question for t in result[:3])
    assert all('北京2025年' in t.question for t in result[3:])


def test_metadata_delivery_does_not_generate_queries():
    result = expand([TaskBoundary(question='解释平均每单销售金额如何计算',
        expected_output='平均每单销售金额的统计口径', definition_only=True)])
    assert len(result) == 1 and result[0].definition_only


def test_reprocessing_a_completed_plan_keeps_tasks_and_dependencies():
    first = expand(company_plan())
    second = expand(first)
    assert len(first) == len(second) == 4
    assert [t.depends_on for t in first] == [t.depends_on for t in second]
    assert [t.metric_targets for t in first] == [t.metric_targets for t in second]
