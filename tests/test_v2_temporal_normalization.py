from datetime import datetime

import pytest

from test_v2_explicit_time import base_catalog, catalog, initial
from test_v2_raw_turn_recognition import planner,turns,parse,metric_step
from app.semantic_v2.recognition_client import RecognitionFailure


def temporal(text,component,operation,value=None,role=None):
    return text,parse(text,[(text,role or ('TIME_RANGE' if component=='RANGE' else 'TIME_GRAIN'),
        'time_spec',operation)],follow=True),dict(payload_type='INHERIT',temporal_edits=[
            dict(component=component,operation=operation,evidence_mention_ids=['m0'],value=value)])


def wrong_range():
    return dict(start='1990-01-01T00:00:00+08:00',end_exclusive='1991-01-01T00:00:00+08:00')


@pytest.mark.asyncio
async def test_current_time_expression_overrides_model_generated_range_boundaries(catalog):
    steps=[initial(),temporal('2026年','RANGE','REPLACE',wrong_range())]
    results=await turns(planner(catalog,steps)[0],steps)
    value=results[-1].plan['logical_plan']['payload']['time']
    assert datetime.fromisoformat(value['range']['start'])==datetime.fromisoformat('2026-01-01T00:00:00+08:00')
    assert datetime.fromisoformat(value['range']['end_exclusive'])==datetime.fromisoformat('2027-01-01T00:00:00+08:00')


@pytest.mark.asyncio
async def test_current_grain_expression_overrides_model_generated_grain(catalog):
    steps=[initial(grain='MONTH'),temporal('按季度','GRAIN','REPLACE','YEAR')]
    results=await turns(planner(catalog,steps)[0],steps)
    assert results[-1].plan['logical_plan']['payload']['time']['grain']=='QUARTER'


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['2026年','换成2026年','改为2026年','改成2026年','设为2026年','设置为2026年'])
@pytest.mark.parametrize('role',['TIME_RANGE','TIME_FIELD'])
async def test_full_range_surface_and_bounded_assignment_cues_preserve_existing_representation(catalog,text,role):
    steps=[initial(),temporal(text,'RANGE','REPLACE',wrong_range(),role)]
    a,b=await turns(planner(catalog,steps)[0],steps)
    after=b.plan['logical_plan']['payload']['time'];before=a.plan['logical_plan']['payload']['time']
    assert datetime.fromisoformat(after['range']['start'])==datetime.fromisoformat('2026-01-01T00:00:00+08:00')
    assert after['grain']==before['grain'] and after['anchor']==before['anchor']


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['不要换成2026年','换成2026年但不含3月','换成2026年或者2027年','销售额2026年',
    '改为修改2026年','改为2026年第一季度或者第二季度','修改时间','订单日期'])
async def test_unknown_partial_or_conflicting_time_surfaces_cannot_authorize_model_dates(catalog,text):
    steps=[initial(),temporal(text,'RANGE','REPLACE',wrong_range())]
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_RANGE_NOT_NORMALIZABLE'):
        await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_range_clear_grain_edit_and_metric_add_cannot_restore_model_dates(catalog):
    steps=[initial(grain='MONTH'),temporal('不限时间','RANGE','CLEAR'),
        temporal('按季度','GRAIN','REPLACE','YEAR'),metric_step('再加销售数量','销售数量','ADD',True),
        temporal('2026年','RANGE','SET',wrong_range())]
    results=await turns(planner(catalog,steps)[0],steps)
    assert all(r.plan['logical_plan']['payload']['time']['range'] is None for r in results[1:4])
    assert results[2].plan['logical_plan']['payload']['time']['grain']=='QUARTER'
    assert datetime.fromisoformat(results[-1].plan['logical_plan']['payload']['time']['range']['start'])==datetime.fromisoformat('2026-01-01T00:00:00+08:00')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['canonical_id','binding_handle','extra_date_key'])
async def test_normalization_does_not_hide_invented_authority_or_extra_date_fields(catalog,fault):
    value=wrong_range();value[fault]='untrusted'
    steps=[initial(),temporal('2026年','RANGE','REPLACE',value)]
    with pytest.raises(RecognitionFailure,match='V2_MODEL_AUTHORITY_FIELD_FORBIDDEN|V2_BINDING_HANDLE_NOT_OFFERED|V2_CONTRACT_VALIDATION_FAILURE'):
        await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_relative_range_uses_current_business_clock_not_model_asof(catalog):
    steps=[initial(),temporal('去年','RANGE','REPLACE',wrong_range())]
    engine,_=planner(catalog,steps)
    engine.clock=lambda:datetime.fromisoformat('2030-01-01T00:00:00+08:00')
    a,b=await turns(engine,steps)
    value=b.plan['logical_plan']['payload']['time']
    assert datetime.fromisoformat(value['range']['start'])==datetime.fromisoformat('2029-01-01T00:00:00+08:00')
    assert value['as_of']=='2029-12-31T16:00:00Z'


@pytest.mark.asyncio
async def test_legacy_whole_time_field_mention_cannot_bypass_initial_date_and_grain_normalization():
    from test_v2_structured_edits import catalog as fixture,initial as old_initial
    service=fixture.__wrapped__();first=old_initial(time=True)
    def draft(c):
        value=first[2](c);time=value['edits'][-1]['value']
        time['range']=wrong_range();time['grain']='YEAR';time['timezone']='+08:00'
        return value
    steps=[(first[0],first[1],draft)]
    result=(await turns(planner(service,steps)[0],steps))[0]
    time=result.plan['logical_plan']['payload']['time']
    assert datetime.fromisoformat(time['range']['start'])==datetime.fromisoformat('2025-01-01T00:00:00+08:00')
    assert time['grain']=='MONTH' and time['timezone']=='Asia/Shanghai'


@pytest.mark.parametrize('surface',[
    '2025年按月按季度','2025年不含3月按月','2025年按月或按年',
    '2025-01-01至2026-01-01（不含结束时刻）',
    '2026-01-01T00:00:00+08:00至2025-01-01T00:00:00+08:00（不含结束时刻）'])
def test_combined_legacy_time_requires_full_consumption_and_aware_ordered_literal_bounds(surface):
    from app.semantic_v2.explicit_time import normalize_combined_surface
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_'):
        normalize_combined_surface(surface,datetime.fromisoformat('2026-09-09T09:00:00+08:00'))


@pytest.mark.asyncio
async def test_explicit_comparison_dates_come_from_current_literal_not_model_baseline():
    from test_v2_structured_edits import catalog as fixture
    from test_v2_temporal_comparisons import initial as compare_initial
    rule=dict(rule_type='EXPLICIT',baseline_range=dict(start='2020-03-01T00:00:00+08:00',end_exclusive='2020-04-01T00:00:00+08:00'))
    first=compare_initial(rule,calculation='ABS_DIFF')
    def draft(c):
        value=first[2](c);value['comparison_edits'][0]['period_rule']['baseline_range']=wrong_range()
        return value
    steps=[(first[0],first[1],draft)];result=(await turns(planner(fixture.__wrapped__(),steps)[0],steps))[0]
    span=result.plan['logical_plan']['payload']['comparison']['comparison_period']
    assert datetime.fromisoformat(span['start'])==datetime.fromisoformat('2020-03-01T00:00:00+08:00')


@pytest.mark.parametrize('surface',['同比','与2020年或者2021年比较','与2020年不含春节比较',
    '与2020年按季度比较','不要与2020年比较','与去年同期比较'])
def test_comparison_cannot_use_model_dates_without_a_supported_whole_explicit_baseline(surface):
    from app.semantic_v2.explicit_time import normalize_comparison_edits
    from app.semantic_v2.temporal_comparisons import ComparisonEditDraft
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    parsed=CurrentTurnSemanticParse.model_validate(parse(surface,[(surface,'COMPARISON_BASELINE','comparison_spec','SET')]))
    draft=ComparisonEditDraft(operation='SET',period_rule=dict(rule_type='EXPLICIT',baseline_range=wrong_range()),
        calculation='ABS_DIFF',evidence_mention_ids=['m0'])
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_TIME_|V2_COMPARISON_RANGE_GRAIN_CONFLICT'):
        normalize_comparison_edits(parsed,[draft],datetime.fromisoformat('2026-09-09T09:00:00+08:00'))
