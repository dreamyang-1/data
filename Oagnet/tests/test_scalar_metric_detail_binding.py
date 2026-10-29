import json
from types import SimpleNamespace as Obj

import pytest
from structured_binding import bind, _metric_having_expression, catalog_candidates


def fixture():
    fields = [
        {'attr_code':'dealer_name','attr_name':'经销商名称','field_mapping':'dealer.dealer_name'},
        {'attr_code':'dealer_code','attr_name':'经销商编码','field_mapping':'dealer.dealer_code'},
        {'attr_code':'first_order_date','attr_name':'首笔成交日期','field_mapping':'dealer.first_order_date'},
    ]
    knowledge = {'entities':[Obj(metadata={'entity_code':'dealer','entity_name':'经销商','attributes':fields})],
        'metrics':[Obj(metadata={'metric_code':'duration','metric_name':'合作时长',
            'calculation_rule':{'calc_formula':'duration=DATEDIFF(CURRENT_DATE, dealer.first_order_date)'},
            'source_dependency':{'bind_entity':['dealer']}})],
        'dimensions':[], '_vector_authorized_fields':[f['field_mapping'] for f in fields]}
    extraction = {'实体':['经销商'],'指标':[{'name':'合作时长'}],'维度':[],
        '展示字段':[{'entity':'经销商','field':f['attr_name']} for f in fields[:2]],
        '过滤条件':[{'field':'合作时长','op':'>','value':[90]}],
        '排序':[{'field':'合作时长','order':'desc'}],
        '时间粒度':{'unit':None,'time_range':None},'限制':1000}
    plan = {'subject':'dealer','metrics':[{'index':0,'key':'duration'}],
        'display_fields':[{'index':i,'key':f['field_mapping']} for i,f in enumerate(fields[:2])],
        'filters':[{'index':0,'key':'duration'}],'sort':[{'index':0,'key':'duration'}]}
    return extraction,knowledge,plan


def test_scalar_duration_preserves_names_codes_threshold_and_sort_without_grouping():
    extraction,knowledge,plan=fixture()
    ast,_=bind(extraction,knowledge,Obj(invoke=lambda _:Obj(content=json.dumps(plan))),row_level_detail=True)
    assert not ast['ambiguity']
    assert {d['name'] for d in ast['display_fields']} == {'dealer.dealer_name','dealer.dealer_code'}
    assert ast['dimensions'] == []
    assert ast['having'] == ['DATEDIFF(CURRENT_DATE, dealer.first_order_date) > 90']
    assert ast['sort']['field']=='duration'
    assert ast['time_context'] is None


def test_aggregate_without_grouping_does_not_invent_detail_grain():
    extraction,knowledge,plan=fixture()
    knowledge['metrics'][0].metadata['calculation_rule']['calc_formula']='duration=COUNT(dealer.dealer_code)'
    ast,_=bind(extraction,knowledge,Obj(invoke=lambda _:Obj(content=json.dumps(plan))),row_level_detail=True)
    assert not ast['ambiguity']
    assert ast['dimensions'] == []
    assert {d['name'] for d in ast['display_fields']} == {'dealer.dealer_name', 'dealer.dealer_code'}


@pytest.mark.parametrize('formula',[
    'duration=SUM(dealer.dealer_code)/COUNT(dealer.dealer_code)',
    'duration=DATEDIFF(CURRENT_DATE, MIN(dealer.first_order_date))',
])
def test_threshold_uses_whole_formula_not_first_aggregate(formula):
    _,knowledge,_=fixture()
    knowledge['metrics'][0].metadata['calculation_rule']['calc_formula']=formula
    assert _metric_having_expression('duration',{'op':'>','value':[90]},catalog_candidates(knowledge)) is None


def test_timestampdiff_preserves_catalog_month_unit_and_threshold():
    _,knowledge,_=fixture()
    knowledge['metrics'][0].metadata['calculation_rule']['calc_formula']='duration=TIMESTAMPDIFF(MONTH, dealer.first_order_date, CURRENT_DATE)'
    assert _metric_having_expression('duration',{'op':'>','value':[3]},catalog_candidates(knowledge)) == 'TIMESTAMPDIFF(MONTH, dealer.first_order_date, CURRENT_DATE) > 3'


def test_unregistered_formula_field_is_not_authorized():
    _,knowledge,_=fixture()
    knowledge['_vector_authorized_fields'].remove('dealer.first_order_date')
    assert _metric_having_expression('duration',{'op':'>','value':[90]},catalog_candidates(knowledge)) is None
