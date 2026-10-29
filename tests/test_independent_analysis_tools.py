"""Selected tools execute independently and do not fetch unrelated baselines."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analysis import AnalysisEngine
from app.analysis.tool_selection import select_tools
from app.domain.models import PlannerExtraction, PrimaryIntent as I
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.services.progress import progress_scope
from test_complex_execution_correctness import service, context, IDENTITY
from test_analysis_delivery_regressions import METRIC, TODAY, extraction


@pytest.mark.parametrize('intent,question,expected', [
    (I.TREND_ANALYSIS, '对2025年7月到12月上海市销售总金额进行环比分析', ['period_over_period_change']),
    (I.TREND_ANALYSIS, '对销售金额进行同比分析', ['year_over_year_change']),
    (I.TREND_ANALYSIS, '识别销售金额拐点和连续涨跌区间', ['turning_points', 'continuous_runs']),
    (I.TREND_ANALYSIS, '趋势分析，只计算环比', ['period_over_period_change']),
    (I.COMPOSITION_ANALYSIS, '只计算各渠道金额的HHI集中度', ['concentration']),
    (I.COMPOSITION_ANALYSIS, '计算各渠道销售金额累计贡献', ['cumulative_contribution']),
    (I.ANOMALY_ANALYSIS, '使用IQR检测每日金额异常', ['IQR']),
    (I.ANOMALY_ANALYSIS, '使用MAD和Z分数进行异常检测', ['MAD', 'Z_SCORE']),
    (I.ROOT_CAUSE_ANALYSIS, '只分析拉升拖累和抵消', ['positive_negative_offset']),
    (I.FORECAST_ANALYSIS, '只用线性趋势外推预测下一月', ['model:linear_drift']),
    (I.FORECAST_ANALYSIS, '只比较模型的回测误差', ['forecast_backtest']),
])
def test_explicit_capability_selection(intent, question, expected):
    assert select_tools(intent, question) == {'mode':'SELECTED', 'capabilities':expected}


@pytest.mark.parametrize('question,intent', [
    ('2025年7至12月上海市销售总金额趋势分析', I.TREND_ANALYSIS),
    ('分析销售趋势，计算各月环比，识别拐点', I.TREND_ANALYSIS),
    ('各渠道销售额占比分析', I.COMPOSITION_ANALYSIS),
    ('检测每日销售额是否存在异常波动', I.ANOMALY_ANALYSIS),
    ('分析11月与12月销售额变化的原因', I.ROOT_CAUSE_ANALYSIS),
    ('基于6个月销售额预测下一月', I.FORECAST_ANALYSIS),
])
def test_general_chain_still_runs_full_capabilities(question,intent):
    assert select_tools(intent,question)['mode'] == 'FULL'


@pytest.mark.asyncio
@pytest.mark.parametrize('keyword,expected,windows', [
    ('环比', 'period_over_period_change', ['2025-06-01至2025-12-31']),
    ('同比', 'year_over_year_change', ['2025-07-01至2025-12-31','2024-07-01至2024-12-31']),
    ('拐点', 'turning_points', ['2025-07-01至2025-12-31']),
])
async def test_real_task_only_calls_requested_operator_and_preserves_scope(monkeypatch,keyword,expected,windows):
    question = f'对2025年7月到12月上海市销售总金额进行{keyword}分析'
    data = extraction('指标查询','2025年7月至12月',['业务日期'])
    data['过滤条件'] = [{'entity':'销售订单','field':'城市','op':'=','value':'上海市'}]
    plan = build_analysis_task_plan(question,PlannerExtraction(intent=I.METRIC_QUERY,parameters=[],structured=data),today=TODAY)
    assert [t.extraction['时间粒度']['time_range'] for t in plan.tasks[:-1]] == windows
    assert all(t.extraction['过滤条件'] == data['过滤条件'] for t in plan.tasks)
    task = plan.tasks[-1]
    binding = task.extraction['分析绑定']
    assert binding['requirements']['methods'] == [expected]
    agent = service()
    agent.analysis_engine = AnalysisEngine()
    calls = []
    execute = agent.analysis_engine.operator_registry.execute
    def spy(name,*args,**kwargs):
        calls.append(name)
        return execute(name,*args,**kwargs)
    monkeypatch.setattr(agent.analysis_engine.operator_registry,'execute',spy)
    monkeypatch.setattr(agent.analysis_engine,'_trend_unscoped',lambda *a,**k: pytest.fail('full trend must not execute'))
    rows = [{'月份':f'2025-{m:02}',METRIC:10*m} for m in range(6 if keyword=='环比' else 7,13)]
    refs = {'task-1':(SimpleNamespace(columns=['月份',METRIC]),rows)}
    if keyword == '同比':
        refs['task-2'] = (SimpleNamespace(columns=['月份',METRIC]),[{'月份':f'2024-{m:02}',METRIC:5*m} for m in range(7,13)])
    monkeypatch.setattr(agent,'_load_task_result_references',AsyncMock(return_value=refs))
    monkeypatch.setattr(agent,'_save_analysis_derived_dataset',AsyncMock(return_value=SimpleNamespace(dataset_id='derived')))
    chat,_ = context(); chat.question=question
    events=[]
    with progress_scope(events.append):
        response = await agent._respond_analysis_task(chat,IDENTITY,task,{}, {},binding)
    assert response.status == 'COMPLETED'
    facts = response.evidence[0].payload['facts']
    assert calls == ([expected] if keyword in {'环比','同比'} else [])
    assert facts['result_scope']['labels'] == [f'2025-{m:02}' for m in range(7,13)]
    assert len(facts['capability_results']) == 2  # selected tool plus input checks
    if keyword == '环比':
        assert 'year_over_year' not in facts and 'change_points' not in facts
        assert len(facts['period_over_period']['changes']) == 6
        assert all('同比' not in event['message'] for event in events)


@pytest.mark.parametrize('question,intent,allowed', [
    ('使用IQR检测每日金额异常', I.ANOMALY_ANALYSIS, {'IQR'}),
    ('只计算各渠道金额HHI集中度', I.COMPOSITION_ANALYSIS, {'hhi_and_entropy'}),
    ('只用线性趋势外推预测下一月', I.FORECAST_ANALYSIS,
        {'forecast_readiness_check','expanding_window_one_step_backtest','train_prefix_predict_next','selected_model_refit'}),
])
def test_independent_other_chains_only_expose_selected_results(question,intent,allowed):
    from app.domain.models import CanonicalAnalysisRequest,MetricRef,KnowledgeContext
    request = CanonicalAnalysisRequest(conversation_id='offline',tenant_id='t',user_id='u',application_id='a',
        original_question=question,rewritten_question=question,primary_intent=intent,
        metrics=[MetricRef(input=METRIC)],semantic_model_id=81)
    columns=['日期',METRIC]
    rows=[{'日期':f'2025-{m:02}',METRIC:m*10} for m in range(1,13)]
    if intent==I.COMPOSITION_ANALYSIS:
        columns=['渠道',METRIC];rows=[{'渠道':'甲',METRIC:60},{'渠道':'乙',METRIC:40}]
    output=AnalysisEngine().analyze(request,columns,rows,KnowledgeContext(query=question,documents=[]))
    methods={r['method'] for r in output.facts['capability_results']}
    if intent==I.ANOMALY_ANALYSIS:
        assert {d['name'] for d in output.facts['detectors']} == allowed
    else:
        assert methods==allowed
    if intent==I.FORECAST_ANALYSIS:
        assert set(output.facts['candidate_scores']) == {'linear_drift'}


def test_backtest_only_does_not_fit_or_predict_the_future(monkeypatch):
    from app.analysis.forecasting import DeterministicForecastSelector
    seen=[]
    original=DeterministicForecastSelector._linear
    def linear(history):
        seen.append(len(history))
        return original(history)
    monkeypatch.setattr(DeterministicForecastSelector,'_linear',staticmethod(linear))
    result=DeterministicForecastSelector().fit_predict([10.0*i for i in range(12)],
        non_negative=True,model_names=['linear_drift'],predict_next=False)
    assert seen and max(seen)<12
    assert result.forecast is None and result.lower is None and result.upper is None
    assert result.backtests and result.candidate_scores['linear_drift']['mae']==pytest.approx(0)


def test_backtest_goal_is_not_misclassified_as_business_comparison():
    question='仅比较2025年1月至12月销售额预测模型的回测误差'
    plan=build_analysis_task_plan(question, PlannerExtraction(intent=I.COMPARISON_ANALYSIS,parameters=[],
        structured=extraction('对比分析','2025年1月至12月',['业务日期'])),today=TODAY)
    binding=plan.tasks[-1].extraction['分析绑定']
    assert binding['intent']==I.FORECAST_ANALYSIS.value
    assert binding['requirements']['tool_selection']=={'mode':'SELECTED','capabilities':['forecast_backtest']}


@pytest.mark.parametrize('values',[ [0,0,0,0,0,0], [0,10,20,30,40,50], [100,100,100,100,100,100] ])
def test_zero_and_constant_inputs_do_not_produce_nonfinite_rates(values):
    import math
    from app.analysis.operators import PeriodOverPeriodOperator
    result=PeriodOverPeriodOperator().run(['月份','销售额'],
        [{'月份':f'2025-{i+1:02}','销售额':v} for i,v in enumerate(values)])
    assert all(r['change_rate'] is None or math.isfinite(r['change_rate']) for r in result.facts['changes'])
    if values[0]==0:
        assert result.warnings and result.facts['changes'][0]['change_rate'] is None
