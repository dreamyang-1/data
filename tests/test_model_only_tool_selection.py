"""User contract: only structured model fields may select analytical tools."""
import copy
import inspect
from datetime import date

import pytest

from app.analysis import AnalysisEngine
from app.analysis.engine import AnalysisError
from app.analysis.intents import analysis_goals,normalize_locked_root_intent
from app.analysis.requirements import build_requirements
from app.analysis.tool_selection import select_tools,selected_for
from app.analysis.tools.plans import AnalysisToolPlan,AnalysisToolPlanRequired
from app.analysis.tools.requirements import build_model_requirements
from app.domain.models import CanonicalAnalysisRequest,MetricRef,KnowledgeContext,PrimaryIntent as I
from test_model_tool_catalog import CASES,proposal,parameters


@pytest.mark.parametrize('question',['环比分析','不要环比，只算同比','MAD预测归因趋势','看起来有点不对，帮我瞧瞧'])
def test_question_alone_never_selects_or_triggers_tools(question):
    with pytest.raises(AnalysisToolPlanRequired):select_tools(I.TREND_ANALYSIS,question)


def test_tool_selection_module_contains_no_text_patterns():
    import app.analysis.tool_selection as module
    assert not hasattr(module,'BROAD')
    assert not hasattr(module,'_mentioned')
    assert 're.search' not in inspect.getsource(module)
    assert 're.finditer' not in inspect.getsource(module)


@pytest.mark.parametrize('question',['只看环比','请计算同比并预测下月','不要计算环比','并没有任何工具关键词'])
def test_validated_model_tool_ids_are_not_overridden_by_question_text(question):
    plan=AnalysisToolPlan.model_validate(proposal(CASES[0],coverage_mode='explicit',capabilities=['period_over_period_change']))
    result=select_tools(I.TREND_ANALYSIS,question,plan=plan)
    assert result['capabilities']==['period_over_period_change']


def test_missing_plan_rejected_before_requirement_compilation_or_math():
    with pytest.raises(AnalysisToolPlanRequired):
        build_requirements(I.TREND_ANALYSIS,'环比分析',parameters(CASES[0]),today=date(2026,10,27))
    request=CanonicalAnalysisRequest(conversation_id='synthetic',tenant_id='t',user_id='u',application_id='a',
        primary_intent=I.TREND_ANALYSIS,original_question='环比趋势分析',metrics=[MetricRef(input='含税销售总额')],semantic_model_id=81)
    with pytest.raises(AnalysisError,match='ANALYSIS_TOOL_PLAN_REQUIRED'):
        AnalysisEngine().analyze(request,['日期','含税销售总额'],[
            {'日期':'2025-10','含税销售总额':100},{'日期':'2025-11','含税销售总额':120}],KnowledgeContext(query='synthetic'))


def test_model_intent_and_routes_are_not_reclassified_by_wording():
    assert normalize_locked_root_intent(I.COMPARISON_ANALYSIS,'各月环比趋势')==I.COMPARISON_ANALYSIS
    assert analysis_goals('分别分析趋势、异常和预测')==[]
    assert analysis_goals(routes=['anomaly','forecast'])==[I.ANOMALY_ANALYSIS,I.FORECAST_ANALYSIS]


@pytest.mark.parametrize('bad',[
    {'mode':'SELECTED','capabilities':['invented']},
    {'mode':'SELECTED','capabilities':['MAD']},
    {'mode':'SELECTED','capabilities':['period_over_period_change','period_over_period_change']},
    {'mode':'FULL','capabilities':[]},
    {'mode':'SELECTED','capabilities':['period_over_period_change'],'parameters':{'unknown':{}}},
])
def test_dispatch_rejects_invalid_structured_fields(bad):
    with pytest.raises(ValueError):select_tools(I.TREND_ANALYSIS,selection=bad)


def test_full_profile_does_not_add_incompatible_tools_to_funnel_input():
    plan=AnalysisToolPlan.model_validate({'chain':'composition','coverage_mode':'full_profile',
        'capabilities':['funnel_conversion_analysis'],'user_evidence':'分析同一队列的漏斗'})
    assert plan.selection()['capabilities']==['funnel_conversion_analysis']


def test_multiple_incompatible_input_shapes_are_not_silently_dropped():
    with pytest.raises(ValueError,match='INCOMPATIBLE_INPUT_SHAPES'):
        AnalysisToolPlan.model_validate({'chain':'composition','coverage_mode':'explicit',
            'capabilities':['funnel_conversion_analysis','share_shift'],'user_evidence':'两个独立分析目标'})


def test_share_filter_parameter_is_read_from_plan_not_question_words():
    plan=AnalysisToolPlan.model_validate({'chain':'composition','coverage_mode':'full_profile',
        'parameters':{'composition_share':{'minimum_share':.3,'inclusive':False}},'user_evidence':'只展示超过三成的渠道'})
    request=CanonicalAnalysisRequest(conversation_id='synthetic',tenant_id='t',user_id='u',application_id='a',
        primary_intent=I.COMPOSITION_ANALYSIS,original_question='不要从本句猜测阈值：占比超过99%',
        metrics=[MetricRef(input='含税销售总额')],semantic_model_id=81)
    request._analysis_contract=build_model_requirements(plan,{},today=date(2026,10,27))
    output=AnalysisEngine().analyze(request,['渠道','含税销售总额'],[
        {'渠道':'甲','含税销售总额':80},{'渠道':'乙','含税销售总额':20}],KnowledgeContext(query='synthetic'))
    assert output.facts['share_threshold']==.3
    assert [row['label'] for row in output.facts['qualified_shares']]==['甲']


@pytest.mark.asyncio
async def test_intent_model_failure_does_not_fall_back_to_keyword_routing(monkeypatch):
    from unittest.mock import AsyncMock
    from app.config import Settings
    from app.planning.compound_question import CompoundQuestionJudge
    from app.planning.task_dag import TaskPlanningError
    judge=CompoundQuestionJudge(Settings(_env_file=None,intent_model_enabled=True,
        multi_question_model_enabled=True,intent_model_api_key='fixture'))
    call=AsyncMock(side_effect=ValueError('invalid model JSON'))
    monkeypatch.setattr(judge,'_model_judge',call)
    with pytest.raises(TaskPlanningError,match='未退回关键词'):
        await judge.judge('分别分析趋势、检测异常、预测下月')
    assert call.await_count==2


def test_declared_period_roles_override_date_order():
    from app.analysis.tools.inputs import align_period_roles,require_observed_pair
    assert align_period_roles(('2025-11','2025-12'),[
        {'role':'BASE','start':'2025-12-01','end_exclusive':'2026-01-01'},
        {'role':'CURRENT','start':'2025-11-01','end_exclusive':'2025-12-01'}],'month')==('2025-12','2025-11')
    with pytest.raises(AnalysisError,match='未将缺失值补成0'):
        require_observed_pair('新渠道',{'2025-12':20},'2025-11','2025-12')
