"""Predicate owners survive extraction, canonical request and ASL verification."""
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.domain.models import CanonicalAnalysisRequest, PlannerExtraction, PrimaryIntent
from app.intent.structured import HybridIntentClassifier, StructuredFilter
from app.planning.task_dag import MultiQuestionPlanner, extraction_parser_prompt
from app.services.orchestrator import DataAnalysisOrchestrator


def request(filters):
    return CanonicalAnalysisRequest(conversation_id='owner-test',tenant_id='t',user_id='u',
        original_question='上海医院和上海经销商',primary_intent=PrimaryIntent.METRIC_QUERY,filters=filters)


@pytest.mark.parametrize('owner', ['医院','经销商','科室','商品','供应商'])
def test_structured_filter_carries_its_owner_not_the_query_subject(owner):
    items = [StructuredFilter(entity=owner,field='名称',value='样本',evidence_span=f'{owner}样本')]
    result, issues = HybridIntentClassifier._grounded_model_filters(items,f'查询{owner}样本的订单')
    assert not issues
    assert result == [{'entity':owner,'field':'名称','operator':'EQ','value':'样本'}]


def test_same_field_same_literal_different_owners_survive_deduplication():
    items = [StructuredFilter(entity=owner,field='省份',value='上海市',evidence_span=f'上海市{owner}')
             for owner in ['医院','经销商']]
    result, issues = HybridIntentClassifier._grounded_model_filters(items,'上海市医院与上海市经销商')
    assert not issues and len(result)==2
    assert [item['entity'] for item in result] == ['医院','经销商']


def test_legacy_filter_without_owner_is_backward_compatible():
    result, issues = HybridIntentClassifier._grounded_model_filters([
        StructuredFilter(field='商品名称',value='样本',evidence_span='样本商品')],'查询样本商品')
    assert not issues and 'entity' not in result[0]


def test_planner_sanitization_does_not_drop_owners():
    source = {'过滤条件':[{'entity':'医院','field':'省份','op':'=','value':['上海市']}]}
    result = MultiQuestionPlanner._sanitize_extraction(source,intent=PrimaryIntent.METRIC_QUERY)
    assert result['过滤条件'] == source['过滤条件']
    assert '条件归属' in extraction_parser_prompt()


def test_planner_final_role_guidance_keeps_names_separate_from_foreign_keys():
    from app.planning.task_dag import _EXTRACTION_ROLE_GUIDANCE
    assert '上海市各经销商→entity=经销商' in _EXTRACTION_ROLE_GUIDANCE
    assert '上海医院按经销商汇总→entity=医院' in _EXTRACTION_ROLE_GUIDANCE
    assert '不能因为目录只在医院/经销商下列出“关联省份ID”' in _EXTRACTION_ROLE_GUIDANCE


@pytest.mark.parametrize('owner', ['医院','经销商'])
def test_execution_entry_point_keeps_explicit_owner_over_legacy_region_role(owner):
    req = request([{'field':'业务城市','operator':'EQ','value':'上海市'}])
    extraction = PlannerExtraction(structured={'过滤条件':[
        {'entity':owner,'field':'省份','op':'=','value':['上海市']}]})
    DataAnalysisOrchestrator._apply_explicit_projection_mode(req,extraction)
    assert req.filters == [{'entity':owner,'field':'省份','operator':'EQ','value':'上海市'}]
    DataAnalysisOrchestrator._apply_explicit_projection_mode(req,extraction)
    assert len(req.filters)==1


def test_two_explicit_owners_are_not_collapsed_at_execution_entry():
    req = request([{'field':'业务城市','operator':'EQ','value':'上海市'}])
    extraction = PlannerExtraction(structured={'过滤条件':[
        {'entity':owner,'field':'省份','op':'=','value':['上海市']} for owner in ['医院','经销商']]})
    DataAnalysisOrchestrator._apply_explicit_projection_mode(req,extraction)
    assert [f['entity'] for f in req.filters] == ['医院','经销商']


def test_planner_restores_second_owner_condition_lost_by_single_region_rule():
    req=request([{'field':'业务城市','operator':'EQ','value':'北京市'}])
    extraction=PlannerExtraction(structured={'过滤条件':[
        {'entity':'医院','field':'省份','op':'=','value':['上海市']},
        {'entity':'经销商','field':'省份','op':'=','value':['北京市']}]})
    DataAnalysisOrchestrator._apply_explicit_projection_mode(req,extraction)
    assert {(f['entity'],f['value']) for f in req.filters} == {('医院','上海市'),('经销商','北京市')}


def test_owner_annotation_never_changes_unrelated_values_or_negation():
    filters = [{'field':'业务城市','operator':'NE','value':'上海市'},
               {'field':'商品名称','operator':'EQ','value':'样本'}]
    req = request(filters)
    extraction = PlannerExtraction(structured={'过滤条件':[
        {'entity':'医院','field':'省份','op':'=','value':['上海市']}]})
    DataAnalysisOrchestrator._apply_explicit_projection_mode(req,extraction)
    assert req.filters == filters


@pytest.mark.parametrize('owner,passes', [('医院',True),('经销商',False),(None,False)])
def test_name_to_owner_key_receipt_is_per_predicate(owner,passes):
    source = {'entity':'医院','field':'省份','op':'=','value':['上海市']}
    predicate = {'field':'hospital.province_id','operator':'=','value':'001'}
    repair = {'type':'RESOLVE_FILTER_BUSINESS_OWNER','source':'SCOPED_RELATION_AND_DICTIONARY',
              'source_filter':source,'resolved_filter':predicate,'owner_entity':'hospital'}
    req = request([{'entity':owner,'field':'省份','operator':'EQ','value':'上海市'}])
    if passes:
        HttpDataRetrievalAdapter._validate_request_filters({'filters':[predicate]},req,repairs=[repair])
    else:
        with pytest.raises(AdapterError):
            HttpDataRetrievalAdapter._validate_request_filters({'filters':[predicate]},req,repairs=[repair])
