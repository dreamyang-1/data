from copy import deepcopy
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.surface_asl import _region_owner_extraction
from app.domain.models import PlannerExtraction, PrimaryIntent
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks
from app.services.orchestrator import DataAnalysisOrchestrator
from test_surface_asl_client import Client, response, run


@pytest.mark.parametrize('owner', ['医院', '经销商'])
def test_region_retry_retains_explicit_business_owner(owner):
    original = {'实体': ['销售订单'], '过滤条件': [
        {'entity': owner, 'field': '省份', 'op': '=', 'value': ['上海']}]}
    before = deepcopy(original)
    fixed = _region_owner_extraction(original)
    assert fixed['过滤条件'] == original['过滤条件']
    assert owner in fixed['实体'] and original == before


def test_owner_retry_surfaces_current_error_instead_of_old_location_error():
    class RetryClient(Client):
        async def post(self, *args, **kwargs):
            await super().post(*args, **kwargs)
            result = response()
            message = '月份排序未绑定' if kwargs['idempotency_key'].endswith(':owner') else '旧的地区归属错误'
            result['result']['ambiguity'] = [{'type': 'context', 'question': message}]
            return result
    with pytest.raises(AdapterError) as error:
        run(RetryClient(response()), structured_extraction={
            '实体': ['销售订单'], '过滤条件': [{'entity': '医院', 'field': '省份', 'value': ['上海']}]})
    assert error.value.details[0]['question'] == '月份排序未绑定'


def test_month_on_month_query_selects_its_automatic_sort_dimension():
    structured = {'指标': [{'name': '含税销售总额'}], '实体': ['销售订单'],
                  '维度': [], '排序': [], '过滤条件': [],
                  '时间粒度': {'unit': '月', 'time_range': '2025年12月', 'anchor': None}}
    plan = build_analysis_task_plan('统计2025年12月销售额并进行环比分析',
        PlannerExtraction(intent=PrimaryIntent.TREND_ANALYSIS, structured=structured), today=date(2026, 10, 24))
    query = plan.tasks[0].extraction
    assert query['排序'][0]['field'] in query['维度']
    assert query['时间粒度']['time_range'] == '2025-11-01至2025-12-31'


def test_registered_average_is_not_replaced_with_comparison_analysis():
    plan = QuestionDecomposition(intent=PrimaryIntent.COMPARISON_ANALYSIS,
        metric_catalog=(frozenset({'平均每单销售金额'}), {'订单平均金额': '平均每单销售金额'}),
        metric_dependencies={'平均每单销售金额': ('含税销售总额', '订单笔数')},
        tasks=(DecomposedTask(question='统计含税销售总额与订单笔数', structured={'是否查询数据库': '是'}),
               DecomposedTask(question='计算订单平均金额，不再查询数据库', depends_on=(0,),
                              structured={'是否查询数据库': '否'})))
    assert _collapse_analysis_computation_tasks(plan) is plan


def test_owner_clarification_identifies_the_actual_missing_role():
    raw = '结构化参数【过滤条件归属】{"field":"dim_province.province_name","value":"上海市"}：无法确认该筛选作用于哪个业务实体或无法解析对应标准值'
    text = DataAnalysisOrchestrator._plain_structured_param_question(raw)
    assert '所在地' in text and '上海市' in text and '没有能准确对应' not in text


@pytest.mark.asyncio
async def test_registered_average_keeps_executable_formula_with_comparison_root(monkeypatch):
    from test_task_planning_stages import setup_planner
    plan = {'tasks': [
        {'question': '统计2025年上海市含税销售总额和订单笔数',
         'metric_targets': ['含税销售总额', '订单笔数']},
        {'question': '根据前序结果计算平均每单销售金额，不再查询数据库',
         'metric_targets': ['平均每单销售金额'], 'depends_on': [0]},
    ]}
    planner, calls = setup_planner(monkeypatch, plan)
    result = await planner.plan_question('2025年上海市平均每单销售金额是多少',
        semantic_model_id=130, root_intent=PrimaryIntent.COMPARISON_ANALYSIS)
    assert len(result.tasks) == 2 and len(calls) == 2
    assert result.tasks[-1].depends_on == (0,)
    binding = result.tasks[-1].structured['计算绑定']
    assert binding['numerator']['column'] == '含税销售总额'
    assert binding['denominator']['column'] == '订单笔数'
