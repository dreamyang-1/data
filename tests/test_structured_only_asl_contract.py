import copy
import json

import pytest
from app.domain.models import PlannerExtraction, PrimaryIntent, MetricRef
from app.services.intent_asl_contract import build_intent_asl_contract
from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from test_http_adapters import request, StubClient, IDENTITY


def extracted_request():
    req=request().model_copy(update={'primary_intent':PrimaryIntent.DETAIL_QUERY,
        'entity':'经销商','fields':['经销商名称']})
    req._planner_extraction=PlannerExtraction(structured={
        '实体':['经销商'],'指标':[], '维度':[],
        '展示字段':[{'entity':'经销商','field':'经销商名称'}],
        '过滤条件':[], '排序':[], '时间粒度':{'unit':None,'time_range':None},
        '限制':None,'是否去重':'否'})
    return req


def test_contract_empty_arrays_do_not_inherit_canonical_parameters():
    req=extracted_request()
    req.metrics=[MetricRef(input='额外指标',metric_id='81:extra')]
    req.dimensions=['额外维度'];req.filters=[{'field':'省份','operator':'=','value':'上海'}]
    req.ranking_limit=5
    contract=build_intent_asl_contract(req)
    assert contract['required_metrics']==contract['required_metric_codes']==[]
    assert contract['required_groupings']==contract['filters']==[]
    assert contract['sorting'] is None
    assert contract['required_projections']==['经销商名称']


def test_contract_keeps_overlapping_roles():
    req=extracted_request()
    req._planner_extraction.structured['维度']=[{'field':'经销商名称'}]
    contract=build_intent_asl_contract(req)
    assert contract['required_groupings']==contract['required_projections']==['经销商名称']


@pytest.mark.asyncio
@pytest.mark.parametrize('metrics', [[], [{'name':'sales','alias':'销售额'}]])
async def test_adapter_passes_bound_display_metric_and_sort_unchanged(metrics):
    req=extracted_request()
    req._planner_extraction.structured['指标']=[{'name':'销售额'}] if metrics else []
    req._planner_extraction.structured['排序']=[{'field':'销售额','order':'desc'}] if metrics else []
    asl={'version':'2.0','intent':'query','subject':{'entity':'dealer'},'metrics':metrics,
         'dimensions':[{'name':'dealer.dealer_name'}] if metrics else [],
         'display_fields':[{'name':'dealer.dealer_name','alias':'经销商名称'}],
         'filters':[],'having':[],'time_context':None,'limit':None,
         'sort':{'field':'sales','field_type':'metric','direction':'DESC'} if metrics else None,
         'ambiguity':[]}
    before=copy.deepcopy(asl)
    client=StubClient([{'success':True,'result':json.dumps(asl)},
        {'success':True,'sql':'SELECT 1'},
        {'success':True,'sql':'SELECT 1','data':[{'经销商名称':'样本'}],
         'columns':['经销商名称'],'row_count':1}])
    await HttpDataRetrievalAdapter(Settings(adapter_mode='http'),client).query(
        req,IDENTITY,semantic_model_id=81,business_domain_id=205)
    translated=client.calls[1][2]['asl']
    if isinstance(translated,str):translated=json.loads(translated)
    assert translated==before


def test_asl_display_exposes_two_independent_arrays():
    from app.presentation.intent_recognition import render_asl_extraction_json
    asl={'dimensions':[{'name':'dealer.code'}],'display_fields':[{'name':'dealer.name'}]}
    text=render_asl_extraction_json(asl)
    assert '"dimensions":' in text and '"display_fields":' in text
    assert 'dimensions / display_fields' not in text
