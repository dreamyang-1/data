"""An upstream cohort year must not constrain a dependent company lookup."""
import pytest

from app.domain.models import PlannerExtraction, PrimaryIntent, TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier
from app.services.intent_asl_contract import build_intent_asl_contract
from app.services.orchestrator import DataAnalysisOrchestrator, _INTERNAL_ASSUMPTIONS


QUESTION = '根据前序任务选出的2025年平均每单销售金额最高的前五位业务员，查询他们所属的销售公司名称'


def apply_child(temporal, *, include_time=True, bound=True):
    request=RuleBasedIntentClassifier().classify(QUESTION,TrustedIdentity(tenant_id='t',user_id='u',roles=[]),'child')
    assert request.time_range is not None
    structured={'实体':['业务员','销售公司'],'指标':[], '维度':[],
        '展示字段':[{'entity':'业务员','field':'销售员姓名'},{'entity':'销售公司','field':'国药公司名称'}],
        '过滤条件':[], '排序':[], '限制':None}
    if include_time:structured['时间粒度']=temporal
    extraction=PlannerExtraction(intent=PrimaryIntent.DETAIL_QUERY,structured=structured)
    token=_INTERNAL_ASSUMPTIONS.set(('DAG_BOUND_STRUCTURED_QUERY',) if bound else ())
    try:
        DataAnalysisOrchestrator._apply_dag_structured_query_shape(request,extraction)
        DataAnalysisOrchestrator._apply_planned_temporal_shape(request,extraction)
    finally:_INTERNAL_ASSUMPTIONS.reset(token)
    request._planner_extraction=extraction
    return request


@pytest.mark.parametrize('temporal',[None,{}, {'unit':None,'time_range':None,'anchor':None}])
def test_company_lookup_with_explicit_empty_time_does_not_require_date_anchor(temporal):
    request=apply_child(temporal)
    assert request.time_range is None
    contract=build_intent_asl_contract(request)
    assert contract['time_policy']!='REQUIRED'
    assert contract['canonical_time_range'] is None
    assert '国药公司名称' in contract['required_projections']


def test_new_child_period_replaces_upstream_year_and_still_requires_time_anchor():
    request=apply_child({'time_range':'2024年'})
    assert request.time_range.start.year==2024
    assert build_intent_asl_contract(request)['time_policy']=='REQUIRED'


@pytest.mark.parametrize('include_time,bound',[(False,True),(True,False)])
def test_missing_legacy_slot_or_ordinary_question_keeps_existing_time(include_time,bound):
    request=apply_child(None,include_time=include_time,bound=bound)
    assert request.time_range.start.year==2025
