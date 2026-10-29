"""A registered price must not replace the user's amount per counted object."""
import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.planning.metric_target_contract import object_average_contract
from app.planning.task_dag import (
    SingleQuestionPlanner, TaskInputBindingError, _parse_metric_catalog,
    _parse_metric_dependencies,
)
from app.planning.task_plan_stages import TaskBoundary, boundary_targets


SPEC = '''| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |
| --- | --- | --- | --- | --- | --- |
| 平均每单销售金额 | 每单平均金额 | 销售总金额 ÷ 订单总数，每单平均销售额 | 元 | 复合口径指标 | 含税销售总额, 订单笔数 |
| 订单级均价 | 整单平均售价 | 先按订单号汇总，含税销售总额 ÷ 销售总数量 | 元 | 复合口径指标 | 含税销售总额, 销售总数量 |
| 含税销售总额 | 含税销售额 | 含税金额合计 | 元 | 原子指标 | 无 |
| 销售总数量 | 销量 | 数量净额合计 | 件 | 原子指标 | 无 |
| 订单笔数 | 订单数,单据数量 | 指定期间按订单键去重的订单数量 | 笔 | 原子指标 | 无 |
'''
QUESTION = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'


def contract(question=QUESTION, spec=SPEC):
    return object_average_contract(question, spec, _parse_metric_catalog(spec), _parse_metric_dependencies(spec))


@pytest.mark.parametrize('question', [
    QUESTION,
    '列出2025年订单平均金额最低的前三位销售人员及所属公司',
    '统计各业务员的平均订单金额，按该金额降序取前五名',
    '查询平均每笔订单金额最高的业务员',
    '统计2025年各业务员的平均每单销售金额并返回销售公司',
    '按业务员统计每单平均金额',
])
def test_registered_count_denominator_is_selected_for_amount_per_order(question):
    selected = contract(question)
    assert selected.candidates == ('平均每单销售金额',)
    assert '订单笔数' in selected.instruction()
    assert '订单级均价' in selected.conflicting_ratios


@pytest.mark.parametrize('question', [
    '分析医院A采购商品B的订单级均价分布',
    '查询2025年整单平均售价最高的销售员',
    '查询每件商品的平均售价',
    '用含税销售总额除以销售总数量计算订单平均金额',
    '解释订单平均金额的业务口径',
    '分别返回平均每单销售金额和订单级均价',
    '查询每笔订单金额最高的前五张订单',
])
def test_explicit_price_formula_definition_and_single_order_values_are_preserved(question):
    assert contract(question) is None


def test_absent_definition_never_invents_a_metric_or_crosses_models():
    spec = '\n'.join(line for line in SPEC.splitlines() if not line.startswith('| 平均每单销售金额 |'))
    assert contract(spec=spec) is None


def test_two_current_registered_scopes_cannot_be_arbitrarily_chosen():
    spec = SPEC + '| 另一种每单金额 | 无 | 销售总金额 ÷ 订单总数，另一个有效样本范围 | 元 | 复合口径指标 | 含税销售总额, 订单笔数 |\n'
    selected = contract(spec=spec)
    assert len(selected.candidates) == 2
    with pytest.raises(ValueError, match='多个已登记口径'):
        selected.validate([TaskBoundary(question=QUESTION, metric_targets=['平均每单销售金额'])],
                          _parse_metric_catalog(spec), boundary_targets)


def test_same_rule_uses_another_domains_registered_names():
    spec = SPEC.replace('平均每单销售金额', '每次配送金额').replace('每单平均金额', '平均配送金额')
    spec = spec.replace('订单总数', '配送总数').replace('订单笔数', '配送次数').replace('订单数', '配送数')
    spec = spec.replace('含税销售总额', '结算总额').replace('销售总金额', '结算金额').replace('| 笔 |', '| 次 |')
    selected = contract('查询配送平均金额最高的站点', spec)
    assert selected.candidates == ('每次配送金额',)
    assert '配送次数' in selected.instruction()


@pytest.mark.asyncio
@pytest.mark.parametrize('correct_second_attempt', [True, False])
async def test_wrong_price_plan_is_rejected_before_parameters_and_corrected_once(monkeypatch, correct_second_attempt):
    async def reference(*args, **kwargs):
        return {'available': True, 'source': 'semantic_model_130.md', 'content': SPEC}
    monkeypatch.setattr(semantic_description, 'load_semantic_description', reference)
    calls, plan_calls, parameter_calls = [], [], []

    async def model(request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        calls.append(body)
        if '只绑定给定规范行' in system:
            payload = {'bindings': [{'metric': '平均每单销售金额', 'operation': 'divide',
                                    'numerator': '含税销售总额', 'denominator': '订单笔数', 'format': 'number'}]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})
        if '你是任务规划器' in system:
            plan_calls.append(body)
            assert '金额/计数口径合同' in system
            target = '平均每单销售金额' if correct_second_attempt and len(plan_calls) == 2 else '订单级均价'
            payload = {'tasks': [{'question': QUESTION, 'name': '查询2025年业务员平均金额前五名及销售公司',
                                 'metric_targets': [target], 'grain': ['业务员'],
                                 'expected_output': target + '最高的五名业务员及公司'}]}
        else:
            parameter_calls.append(body)
            planned = json.loads(system.split('已规划任务JSON：\n', 1)[1])
            payload = {'tasks': [{'task_index': t['task_index'], 'extraction': {
                '意图': '指标计算' if t['depends_on'] else '指标查询',
                '指标': [], '维度': ['业务员'], '展示字段': [
                    {'entity': '业务员', 'field': '销售员姓名'},
                    {'entity': '销售公司', 'field': '国药公司名称'}],
                '时间粒度': {'unit': '年', 'time_range': '2025年'},
                '排序': [{'field': '平均每单销售金额', 'order': 'desc'}], '限制': 5,
                '是否查询数据库': '否' if t['depends_on'] else '是'}} for t in planned['tasks']]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})

    settings = Settings(_env_file=None, env='test', intent_model_enabled=True,
                        multi_question_model_enabled=True, intent_model_api_key=SecretStr('fixture'))
    planner = SingleQuestionPlanner(settings, transport=httpx.MockTransport(model))
    if not correct_second_attempt:
        with pytest.raises(TaskInputBindingError, match='金额/计数口径'):
            await planner.plan_question(QUESTION, semantic_model_id=130)
        assert len(plan_calls) == 2 and not parameter_calls
        return
    result = await planner.plan_question(QUESTION, semantic_model_id=130)
    assert len(plan_calls) == 2 and len(parameter_calls) == 1
    assert len(result.tasks) == 3
    assert [t.structured['指标'] for t in result.tasks] == [
        [{'name': '含税销售总额'}], [{'name': '订单笔数'}], [{'name': '平均每单销售金额'}]]
    assert all(t.structured['维度'] == ['业务员'] for t in result.tasks)
    assert all(t.structured['限制'] is None for t in result.tasks[:2])
    assert result.tasks[-1].depends_on == (0, 1)
    assert result.tasks[-1].structured['计算绑定']['denominator']['column'] == '订单笔数'
    assert result.tasks[-1].structured['限制'] == 5
    assert result.tasks[-1].structured['展示字段'][-1]['field'] == '国药公司名称'
