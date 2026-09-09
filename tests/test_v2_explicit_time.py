from copy import deepcopy
from datetime import date, datetime

import pytest

from app.intent.classifier import RuleBasedIntentClassifier
from app.semantic_v2.explicit_time import normalize_range
from app.semantic_v2.recognition_client import RecognitionFailure
from test_v2_raw_turn_recognition import (catalog as base_catalog, reseal, publish, parse,
    binding, edit, metric_step, planner, turns)

NOW=datetime.fromisoformat('2026-09-09T09:00:00+08:00')


@pytest.mark.parametrize('text,start,end',[
    ('2025年','2025-01-01','2026-01-01'),('2025年2月','2025-02-01','2025-03-01'),
    ('2024年2月29日','2024-02-29','2024-03-01'),('去年','2025-01-01','2026-01-01'),
    ('本季度','2026-07-01','2026-10-01'),('上季度','2026-04-01','2026-07-01'),
    ('最近一年','2025-09-09','2026-09-10'),('近7天','2026-09-03','2026-09-10'),
    ('2025年1月到3月','2025-01-01','2025-04-01'),('2025年第一季度','2025-01-01','2025-04-01'),
    ('2025年上半年','2025-01-01','2025-07-01'),('2025年1月1日至2025年1月3日','2025-01-01','2025-01-04'),
    ('25年3月','2025-03-01','2025-04-01'),('本月至今','2026-09-01','2026-09-10')])
def test_time_range_reuses_business_calendar_with_fixed_reference_and_exact_consumption(text,start,end):
    value=normalize_range(text,NOW)
    assert value.start==datetime.fromisoformat(start+'T00:00:00+08:00')
    assert value.end_exclusive==datetime.fromisoformat(end+'T00:00:00+08:00')


@pytest.mark.parametrize('text',['销售额2025年','2025年和2026年','2025年以后','2025年13月','2025年2月30日',
    '最近400天','ABC今天','今天或昨天','2025年订单','2025财年','去年销售额','最近一年之前','2025年一季度'])
def test_partial_ambiguous_unsupported_or_invalid_ranges_cannot_become_authoritative(text):
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_RANGE_NOT_NORMALIZABLE'):normalize_range(text,NOW)


def test_default_legacy_substring_contract_is_preserved_and_clock_is_explicit():
    assert RuleBasedIntentClassifier._time_range('2025年订单笔数').start==date(2025,1,1)
    assert RuleBasedIntentClassifier._time_range('2025年订单笔数',reference_date=date(2040,1,1),whole_expression=True) is None
    assert normalize_range('今天',datetime.fromisoformat('2026-09-08T23:00:00+00:00')).start==datetime.fromisoformat('2026-09-09T00:00:00+08:00')
    assert normalize_range('去年',datetime.fromisoformat('2030-01-01T00:00:00+08:00')).start==datetime.fromisoformat('2029-01-01T00:00:00+08:00')


@pytest.fixture
def catalog(base_catalog):
    source=deepcopy(base_catalog[4][(81,(205,))]);doc=source['documents'][0]
    entity=doc['entities'][0]
    for id,code,label in [(1291,'created_at','创建日期'),(1292,'posted_at','过账日期')]:
        entity['attributes'].append(dict(attribute_id=id,attr_code=code,attr_name=label,field_mapping='hospitals.'+code))
        source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=id,field_name=code,table_id=1))
    for metric in doc['metrics']:
        metric['time_caliber']=dict(time_anchor='hospitals.created_at',special_rule=None,stat_cycle=None)
    base_catalog[4][(81,(205,))]=reseal(source);publish(base_catalog[0],publication_id='governed-time')
    return base_catalog


def initial(*,time_first=False,explicit_field=False,grain=None,fault=None):
    text='2025年销售额'+('按过账日期' if explicit_field else '')+('按月' if grain else '')
    specs=[('2025年','TIME_RANGE','time_spec','SET'),('销售额','MEASURE','metrics','SET')]
    if explicit_field:specs.append(('过账日期','TIME_FIELD','time_spec','SET'))
    if grain:specs.append(('按月','TIME_GRAIN','time_spec','SET'))
    parsed=parse(text,specs,shape='TIME_SERIES' if grain else 'SCALAR_AGGREGATE')
    def draft(c):
        anchor=binding(c,'过账日期','TIME_FIELD','m2','ATTRIBUTE') if explicit_field else binding(c,'销售额','MEASURE','m1')
        if fault=='foreign':anchor={'binding_handle':'foreign'}
        if fault=='authority':anchor={'canonical_id':'invented'}
        value=dict(anchor=anchor,range=dict(start='1990-01-01T00:00:00+00:00',end_exclusive='1991-01-01T00:00:00+00:00'),
            grain='YEAR',timezone='+08:00',source='USER_EXPLICIT',as_of='1990-01-01T00:00:00+00:00')
        if fault=='watermark':value['data_watermark']='2025-12-31T00:00:00+08:00'
        if fault=='default':value['source']='SYSTEM_DEFAULT'
        metric=edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',))
        time=edit('time_spec',value,ids=tuple('m'+str(i) for i in range(len(specs)) if i!=1))
        return dict(payload_type='TIME_SERIES' if grain else 'SCALAR_AGGREGATE',edits=[time,metric] if time_first else [metric,time])
    return text,parsed,draft


@pytest.mark.asyncio
@pytest.mark.parametrize('time_first',[False,True])
async def test_metric_anchor_and_dates_are_owned_by_catalog_and_current_expression(catalog,time_first):
    steps=[initial(time_first=time_first)];result=(await turns(planner(catalog,steps)[0],steps))[0]
    time=result.plan['logical_plan']['payload']['time']
    assert time['anchor']['semantic_role']=='TIME_FIELD' and time['anchor']['canonical_code']=='created_at'
    assert time['timezone']=='Asia/Shanghai' and time['grain']=='NONE'
    assert datetime.fromisoformat(time['range']['start'])==datetime.fromisoformat('2025-01-01T00:00:00+08:00')
    assert datetime.fromisoformat(time['range']['end_exclusive'])==datetime.fromisoformat('2026-01-01T00:00:00+08:00')
    op=next(o for o in result.resolution['task_patch']['sets'] if o['slot_path']=='time_spec')
    assert op['reason_code']=='CURRENT_RANGE_NORMALIZED_WITH_GOVERNED_METRIC_ANCHOR'
    assert result.plan['backend_contract']['mode']=='SHADOW_ONLY'


@pytest.mark.asyncio
async def test_an_explicit_time_field_is_preserved_and_time_grain_requires_its_own_mention(catalog):
    steps=[initial(explicit_field=True,grain='MONTH')];result=(await turns(planner(catalog,steps)[0],steps))[0]
    time=result.plan['logical_plan']['payload']['time']
    assert time['anchor']['canonical_code']=='posted_at' and time['grain']=='MONTH'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason',[('foreign','V2_BINDING_HANDLE_NOT_OFFERED'),('authority','V2_MODEL_AUTHORITY_FIELD_FORBIDDEN'),
    ('watermark','V2_TIME_POLICY_EVIDENCE_REQUIRED'),('default','V2_TIME_POLICY_EVIDENCE_REQUIRED')])
async def test_normalization_does_not_accept_foreign_handles_or_model_authority(catalog,fault,reason):
    steps=[initial(fault=fault)]
    with pytest.raises(RecognitionFailure,match=reason):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason',[('missing','CATALOG_METRIC_TIME_ANCHOR_MISSING'),
    ('unknown_field','CATALOG_METRIC_TIME_ANCHOR_NOT_UNIQUE'),('special','CATALOG_METRIC_TIME_RULE_UNSUPPORTED')])
async def test_missing_or_unsupported_catalog_time_facts_do_not_become_defaults(catalog,fault,reason):
    source=deepcopy(catalog[4][(81,(205,))]);caliber=source['documents'][0]['metrics'][0]['time_caliber']
    if fault=='missing':caliber['time_anchor']=None
    if fault=='unknown_field':caliber['time_anchor']='unknown.field'
    if fault=='special':caliber['special_rule']='undocumented fiscal rule'
    catalog[4][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='bad-time-fact')
    steps=[initial()]
    with pytest.raises(RecognitionFailure,match=reason):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_explicit_range_clear_survives_a_later_metric_add(catalog):
    text='不限时间';clear=(text,parse(text,[(text,'TIME_RANGE','time_spec','CLEAR')],follow=True),
        dict(payload_type='INHERIT',temporal_edits=[dict(component='RANGE',operation='CLEAR',evidence_mention_ids=['m0'])]))
    steps=[initial(),clear,metric_step('再加销售数量','销售数量','ADD',True)]
    results=await turns(planner(catalog,steps)[0],steps)
    assert results[0].plan['logical_plan']['payload']['time']['range'] is not None
    assert all(r.plan['logical_plan']['payload']['time']['range'] is None for r in results[1:])
    assert {m['canonical_code'] for m in results[-1].plan['logical_plan']['payload']['measures']}=={'amount','quantity'}


@pytest.mark.asyncio
@pytest.mark.parametrize('extra,reason',[
    ({'calendar':'FISCAL','fiscal_calendar_id':'unverified'},'V2_MODEL_AUTHORITY_FIELD_FORBIDDEN'),
    ({'calendar':'FISCAL'},'V2_TIME_POLICY_EVIDENCE_REQUIRED'),
    ({'default_policy_id':'unverified'},'V2_MODEL_AUTHORITY_FIELD_FORBIDDEN'),
    ({'comparison':{'comparison_type':'YOY'}},'V2_TIME_POLICY_EVIDENCE_REQUIRED'),
    ({'include_incomplete_period':True},'V2_TIME_POLICY_EVIDENCE_REQUIRED'),
    ({'missing_period_policy':'FILL_ZERO'},'V2_TIME_POLICY_EVIDENCE_REQUIRED'),
    ({'boundary':'BOTH_CLOSED'},'V2_TIME_POLICY_EVIDENCE_REQUIRED'),
    ({'unrecognized_policy':True},'V2_EXPLICIT_TIME_EXTRA_FIELD')])
async def test_normalization_cannot_silently_erase_other_policy_or_comparison_instructions(catalog,extra,reason):
    text,parsed,original=initial()
    def draft(c):
        value=original(c)
        next(e['value'] for e in value['edits'] if e['slot_path']=='time_spec').update(extra)
        return value
    steps=[(text,parsed,draft)]
    with pytest.raises(RecognitionFailure,match=reason):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict',[False,True])
async def test_all_selected_metrics_must_share_one_current_catalog_anchor(catalog,conflict):
    if conflict:
        source=deepcopy(catalog[4][(81,(205,))])
        source['documents'][0]['metrics'][1]['time_caliber']['time_anchor']='hospitals.posted_at'
        catalog[4][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='conflicting-anchors')
    text='2025年销售额和销售数量'
    parsed=parse(text,[('2025年','TIME_RANGE','time_spec','SET'),('销售额','MEASURE','metrics','SET'),
        ('销售数量','MEASURE','metrics','SET')],shape='SCALAR_AGGREGATE')
    def draft(c):
        value=initial()[2](c)
        value['edits'][0]['value'].append(binding(c,'销售数量','MEASURE','m2'))
        value['edits'][0]['evidence_mention_ids'].append('m2')
        return value
    steps=[(text,parsed,draft)]
    if conflict:
        with pytest.raises(RecognitionFailure,match='CATALOG_METRIC_TIME_ANCHOR_CONFLICT'):
            await turns(planner(catalog,steps)[0],steps)
    else:
        result=(await turns(planner(catalog,steps)[0],steps))[0]
        assert len(result.plan['logical_plan']['payload']['measures'])==2
        assert result.plan['logical_plan']['payload']['time']['anchor']['canonical_code']=='created_at'


def test_naive_clock_and_unresolved_grain_are_rejected():
    from app.semantic_v2.explicit_time import normalize_grain
    from types import SimpleNamespace
    with pytest.raises(RecognitionFailure,match='V2_CLOCK_MUST_BE_AWARE'):
        normalize_range('今天',datetime(2026,9,9))
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_GRAIN_NOT_NORMALIZABLE'):
        normalize_grain([SimpleNamespace(surface='按月或者按季度')])
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_GRAIN_CONFLICT'):
        normalize_grain([SimpleNamespace(surface='按月'),SimpleNamespace(surface='按季度')])
