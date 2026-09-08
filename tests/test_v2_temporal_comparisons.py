"""Current-evidence temporal comparisons; no real model or execution calls."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from app.semantic_v2.recognition import SemanticTaskDraft
from app.semantic_v2.result_contract import prove_result_contract
from app.semantic_v2.temporal_comparisons import build_comparison
from test_v2_structured_edits import catalog, temporal_step, payload
from test_v2_raw_turn_recognition import NOW, IDENTITY, binding, edit, metric_step, parse, planner, request, turns, publish, reseal


YOY = dict(rule_type='PREVIOUS_YEAR')
BUCKET = dict(rule_type='PREVIOUS_PERIOD', period_unit='QUERY_GRAIN')
WINDOW = dict(rule_type='PREVIOUS_PERIOD', period_unit='CURRENT_WINDOW')


def span(start, end): return dict(start=start, end_exclusive=end)


def initial(rule=None, *, calculation='GROWTH_RATE', grain='MONTH',
        current=None):
    rule = rule or YOY
    period = '2025年' if current is None else current['start']+'至'+current['end_exclusive']+'（不含结束时刻）'
    time_label = period + {'NONE':'','MONTH':'按月','QUARTER':'按季度','YEAR':'按年','WEEK':'按周','DAY':'按天'}[grain]
    comparison_label = ('同比' if rule['rule_type']=='PREVIOUS_YEAR' else
        '与'+rule['baseline_range']['start']+'至'+rule['baseline_range']['end_exclusive']+'（不含结束时刻）比较' if rule['rule_type']=='EXPLICIT' else
        {'QUERY_GRAIN':'各期环比','CURRENT_WINDOW':'与前一整个时段比较','MONTH':'与前一个月比较'}[rule['period_unit']])
    comparison_label += '差值' if calculation=='ABS_DIFF' else '变化率'
    text = time_label+'销售额'+comparison_label
    current = current or span('2025-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')
    specs = [('销售额','MEASURE','metrics','SET'), (time_label,'TIME_FIELD','time_spec','SET'),
        (comparison_label,'COMPARISON_BASELINE','comparison_spec','SET')]
    def draft(c):
        return dict(payload_type='COMPARISON', edits=[edit('metrics',[binding(c,'销售额','MEASURE')]),
            edit('time_spec',dict(anchor=binding(c,'订单日期','TIME_FIELD','m1','DIMENSION'), range=current,
                grain=grain, timezone='Asia/Shanghai',source='USER_EXPLICIT',as_of=NOW.isoformat()),ids=('m1',))],
            comparison_edits=[dict(operation='SET',period_rule=rule,calculation=calculation,evidence_mention_ids=['m2'])])
    return text, parse(text,specs,shape='COMPARISON_SET'), draft


def comparison_step(text, operation='REPLACE', rule=None, calculation='GROWTH_RATE'):
    data = dict(operation=operation, evidence_mention_ids=['m0'])
    if operation != 'CLEAR': data.update(period_rule=rule or YOY,calculation=calculation)
    return text,parse(text,[(text,'COMPARISON_BASELINE','comparison_spec',operation)],follow=True),dict(
        payload_type='INHERIT',comparison_edits=[data])


def local_period(value):
    from zoneinfo import ZoneInfo
    return tuple(datetime.fromisoformat(value[k].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
        for k in ['start','end_exclusive'])


@pytest.mark.asyncio
@pytest.mark.parametrize('rule,kind,baseline', [(YOY,'YOY',('2024-01-01T00:00:00+08:00','2025-01-01T00:00:00+08:00')),
    (BUCKET,'MOM',('2024-12-01T00:00:00+08:00','2025-12-01T00:00:00+08:00'))])
async def test_raw_comparison_derives_periods_and_distinct_outputs(catalog,rule,kind,baseline):
    steps=[initial(rule)];engine,calls=planner(catalog,steps); result=(await turns(engine,steps))[0]
    p=payload(result); spec=p['comparison']
    assert spec['comparison_type']==kind and spec['current_period']==p['time']['range']
    assert local_period(spec['comparison_period'])==baseline
    assert spec['baseline']['range']==spec['comparison_period']
    outputs=result.plan['result_contract']['required_outputs']
    assert {o['comparison_role'] for o in outputs if 'comparison_role' in o}=={'CURRENT','BASELINE','DERIVED'}
    assert len({o['output_field_id'] for o in outputs})==len(outputs)==4
    assert len(calls.calls)==2 and result.plan['backend_contract']['mode']=='SHADOW_ONLY'
    assert AuthorizedLogicalPlan.model_validate(result.plan['logical_plan']).model_dump(mode='json')==result.plan['logical_plan']


@pytest.mark.asyncio
async def test_range_and_metric_edits_recompute_comparison_atomically(catalog):
    new=span('2026-01-01T00:00:00+08:00','2027-01-01T00:00:00+08:00')
    steps=[initial(),temporal_step('换成2026年','RANGE','REPLACE',new),
        metric_step('再加销售数量','销售数量','ADD',True),metric_step('不要销售额','销售额','REMOVE',True)]
    engine,_=planner(catalog,steps); results=await turns(engine,steps)
    assert local_period(payload(results[1])['comparison']['comparison_period'])==('2025-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')
    for result,expected in zip(results,[1,1,2,1]):
        p=payload(result)
        assert p['comparison']['output_metrics']==p['measures'] and len(p['measures'])==expected
        assert len([o for o in result.plan['result_contract']['required_outputs'] if 'comparison_role' in o])==expected*3
        state=next(iter(result.next_state.payload['tasks'].values()))
        assert state['versions'][-1]['semantics']['comparison_spec']==p['comparison']


@pytest.mark.asyncio
@pytest.mark.parametrize('rule,expected_kind,expected_start',[(YOY,'YOY','2024-01-01'),(BUCKET,'PERIOD_OVER_PERIOD','2024-10-01')])
async def test_grain_changes_only_declared_grain_dependency(catalog,rule,expected_kind,expected_start):
    steps=[initial(rule),temporal_step('按季度','GRAIN','REPLACE','QUARTER')]
    engine,_=planner(catalog,steps); a,b=await turns(engine,steps)
    assert payload(a)['time']['range']==payload(b)['time']['range']
    assert payload(b)['comparison']['comparison_type']==expected_kind
    assert local_period(payload(b)['comparison']['comparison_period'])[0].startswith(expected_start)


@pytest.mark.asyncio
async def test_fixed_month_comparison_does_not_silently_become_quarter(catalog):
    steps=[initial(dict(rule_type='PREVIOUS_PERIOD',period_unit='MONTH')),temporal_step('按季度','GRAIN','REPLACE','QUARTER')]
    engine,_=planner(catalog,steps)
    with pytest.raises(ValueError,match='PERIOD_GRAIN_CONFLICT'): await turns(engine,steps)


@pytest.mark.asyncio
async def test_previous_window_retains_unequal_calendar_month_lengths(catalog):
    current=span('2024-02-01T00:00:00+08:00','2024-03-01T00:00:00+08:00')
    steps=[initial(WINDOW,grain='NONE',current=current)];engine,_=planner(catalog,steps)
    result=(await turns(engine,steps))[0]
    assert local_period(payload(result)['comparison']['comparison_period'])==('2024-01-01T00:00:00+08:00','2024-02-01T00:00:00+08:00')
    assert result.plan['result_contract']['expected_cardinality']['kind']=='SCALAR'
    assert result.plan['result_contract']['row_bounds']['maximum']==1
    assert len(result.plan['result_contract']['required_outputs'])==3


@pytest.mark.asyncio
async def test_explicit_baseline_is_retained_when_current_period_changes(catalog):
    chosen=span('2020-03-01T00:00:00+08:00','2020-04-01T00:00:00+08:00')
    rule=dict(rule_type='EXPLICIT',baseline_range=chosen)
    steps=[initial(rule,calculation='ABS_DIFF'),temporal_step('换成2026年','RANGE','REPLACE',
        span('2026-01-01T00:00:00+08:00','2027-01-01T00:00:00+08:00'))]
    engine,_=planner(catalog,steps);a,b=await turns(engine,steps)
    assert payload(a)['comparison']['comparison_period']==payload(b)['comparison']['comparison_period']
    assert payload(b)['comparison']['comparison_type']=='CUSTOM_PERIOD'


@pytest.mark.asyncio
async def test_clear_comparison_preserves_query_and_does_not_revive(catalog):
    steps=[initial(),comparison_step('不做同比','CLEAR'),metric_step('再加销售数量','销售数量','ADD',True)]
    engine,_=planner(catalog,steps);a,b,c=await turns(engine,steps)
    for result in [b,c]:
        assert payload(result)['payload_type']=='TIME_SERIES' and 'comparison' not in payload(result)
        state=next(iter(result.next_state.payload['tasks'].values()))
        assert state['versions'][-1]['semantics']['comparison_spec'] is None and 'comparison_spec' in state['clear_barriers']
        assert payload(result)['time']==payload(a)['time']


@pytest.mark.asyncio
async def test_time_clear_requires_explicit_cancellation_of_comparison(catalog):
    steps=[initial(),temporal_step('不限时间','RANGE','CLEAR')];engine,_=planner(catalog,steps)
    with pytest.raises(ValueError,match='BOUNDED_TIME_REQUIRED'): await turns(engine,steps)
    text='不限时间也不做同比'
    step=text,parse(text,[('不限时间','TIME_FIELD','time_spec','CLEAR'),('不做同比','COMPARISON_BASELINE','comparison_spec','CLEAR')],follow=True),dict(
        payload_type='INHERIT',temporal_edits=[dict(component='RANGE',operation='CLEAR',evidence_mention_ids=['m0'])],
        comparison_edits=[dict(operation='CLEAR',evidence_mention_ids=['m1'])])
    steps=[initial(),step,metric_step('再加销售数量','销售数量','ADD',True)];engine,_=planner(catalog,steps)
    results=await turns(engine,steps)
    assert payload(results[-1])['time']['range'] is None and payload(results[-1])['payload_type']=='TIME_SERIES'


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['current_period','comparison_period','baseline','output_metrics'])
async def test_final_scoped_plan_rejects_forged_or_stale_derivation(catalog,field):
    steps=[initial()];engine,_=planner(catalog,steps);data=deepcopy((await turns(engine,steps))[0].plan['logical_plan'])
    spec=data['payload']['comparison']
    if field=='output_metrics': spec[field]=[]
    elif field=='baseline': spec['baseline']['range']=spec['current_period']
    else: spec[field]=span('2022-01-01T00:00:00+08:00','2023-01-01T00:00:00+08:00')
    with pytest.raises(ValueError,match='DERIVATION_MISMATCH'): AuthorizedLogicalPlan.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['period','outputs','policy','direct','evidence'])
async def test_model_cannot_supply_derived_authority_or_unsupported_edits(catalog,fault):
    first=initial()
    def corrupt(c):
        d=first[2](c)
        if fault=='period':d['comparison_edits'][0]['current_period']={}
        if fault=='outputs':d['comparison_edits'][0]['output_metrics']=[]
        if fault=='policy':d['comparison_edits'][0]['policy_version']='guessed'
        if fault=='direct':d['edits'].append(edit('comparison_spec',None))
        if fault=='evidence':d['comparison_edits'][0]['evidence_mention_ids']=['old']
        return d
    step=first[0],first[1],corrupt;engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match='MODEL_OUTPUT_INVALID|EVIDENCE_NOT_CURRENT'):await turns(engine,[step])


def typed_time(start,end,grain='NONE',zone='Asia/Shanghai'):
    from tools.phase25_1.fixtures import time_spec
    data=time_spec().model_dump();data.update(range=span(start,end),grain=grain,timezone=zone,calendar='NATURAL')
    return m.TimeSpec.model_validate(data)


@pytest.mark.parametrize('start,end,rule,grain,reason',[
    ('2024-02-29T00:00:00+08:00','2024-03-01T00:00:00+08:00',YOY,'NONE','CALENDAR_MAPPING_UNDEFINED'),
    ('2025-03-31T00:00:00+08:00','2025-04-01T00:00:00+08:00',dict(rule_type='PREVIOUS_PERIOD',period_unit='MONTH'),'NONE','CALENDAR_MAPPING_UNDEFINED'),
    ('2025-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00',BUCKET,'NONE','PERIOD_UNIT_REQUIRED'),
    ('2025-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00',YOY,'WEEK','WEEK_YEAR_POLICY_REQUIRED')])
def test_unsupported_calendar_policy_is_explicit(start,end,rule,grain,reason):
    with pytest.raises(ValueError,match=reason):build_comparison(typed_time(start,end,grain),[],m.ComparisonPeriodRule(**rule),'GROWTH_RATE')


@pytest.mark.parametrize('start,end,reason',[
    ('2025-03-10T02:30:00-04:00','2025-03-11T02:30:00-04:00','LOCAL_TIME_POLICY_REQUIRED'),
    ('2025-11-03T01:30:00-05:00','2025-11-04T01:30:00-05:00','LOCAL_TIME_POLICY_REQUIRED')])
def test_calendar_shift_rejects_nonexistent_and_ambiguous_local_times(start,end,reason):
    with pytest.raises(ValueError,match=reason):build_comparison(typed_time(start,end,zone='America/New_York'),[],
        m.ComparisonPeriodRule(rule_type='PREVIOUS_PERIOD',period_unit='DAY'),'GROWTH_RATE')


def test_day_comparison_preserves_local_boundaries_across_dst():
    time=typed_time('2025-03-10T00:00:00-04:00','2025-03-11T00:00:00-04:00',zone='America/New_York')
    spec=build_comparison(time,[],m.ComparisonPeriodRule(rule_type='PREVIOUS_PERIOD',period_unit='DAY'),'ABS_DIFF')
    assert (spec.comparison_period.end_exclusive-spec.comparison_period.start).total_seconds()==23*3600


def test_previous_window_reuses_the_explicit_dst_fold_endpoint():
    time=typed_time('2025-11-02T01:30:00-05:00','2025-11-03T01:30:00-05:00',zone='America/New_York')
    spec=build_comparison(time,[],m.ComparisonPeriodRule(**WINDOW),'ABS_DIFF')
    assert spec.comparison_period.end_exclusive==time.range.start
    assert (spec.comparison_period.end_exclusive-spec.comparison_period.start).total_seconds()==25*3600


@pytest.mark.parametrize('fault,reason',[('fiscal','CALENDAR_POLICY_UNSUPPORTED'),('rolling','CALENDAR_POLICY_UNSUPPORTED'),
    ('embedded','DUPLICATE_TIME_AUTHORITY'),('unbounded','BOUNDED_TIME_REQUIRED'),('calculation','CALCULATION_UNSUPPORTED')])
def test_undefined_time_authorities_and_calculations_fail_closed(fault,reason):
    time=typed_time('2025-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')
    data=time.model_dump()
    if fault=='fiscal':data.update(calendar='FISCAL',fiscal_calendar_id='governed-calendar',calendar_policy_version='v1')
    if fault=='rolling':data['calendar']='ROLLING'
    if fault=='embedded':data['comparison']=dict(comparison_type='YOY',comparison_range=time.range)
    if fault=='unbounded':data.update(range=None,source='USER_EXPLICIT_UNBOUNDED')
    with pytest.raises(ValueError,match=reason):build_comparison(m.TimeSpec.model_validate(data),[],m.ComparisonPeriodRule(**YOY),
        'RATIO' if fault=='calculation' else 'GROWTH_RATE')


@pytest.mark.asyncio
async def test_scalar_comparison_clear_returns_scalar_with_time_preserved(catalog):
    steps=[initial(grain='NONE'),comparison_step('不要比较','CLEAR')];engine,_=planner(catalog,steps);a,b=await turns(engine,steps)
    assert payload(b)['payload_type']=='SCALAR_AGGREGATE' and payload(b)['time']==payload(a)['time']


@pytest.mark.asyncio
async def test_add_comparison_again_is_an_explicit_current_edit(catalog):
    steps=[initial(),comparison_step('不要同比','CLEAR'),comparison_step('改看环比','SET',BUCKET)]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert payload(results[-1])['comparison']['comparison_type']=='MOM'
    assert 'comparison_spec' not in next(iter(results[-1].next_state.payload['tasks'].values()))['clear_barriers']


@pytest.mark.asyncio
async def test_frozen_legacy_guard_rejects_new_comparison_and_output_variants(catalog):
    steps=[initial()];engine,_=planner(catalog,steps);result=(await turns(engine,steps))[0]
    comparison=m.TemporalComparisonSpec.model_validate(payload(result)['comparison'])
    contract=m.ResultContract.model_validate(result.plan['result_contract'])
    for value in [comparison,contract]:
        with pytest.raises(ValueError,match='require scoped 0.2.2'):m.require_bounded_legacy_time(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('base,current,derived,calculation,passed',[
    (10,12,.2,'GROWTH_RATE',True),(-10,-8,.2,'GROWTH_RATE',True),(0,12,None,'GROWTH_RATE',True),
    (0,12,0,'GROWTH_RATE',False),(10,12,20,'GROWTH_RATE',False),(10,12,2,'ABS_DIFF',True),
    (0,12,12,'ABS_DIFF',True),(None,12,None,'GROWTH_RATE',True),(None,12,0,'ABS_DIFF',False),
    (10,12,None,'ABS_DIFF',False),(10,12,float('inf'),'GROWTH_RATE',False)])
async def test_result_checks_comparison_triplets_without_fabricating_rates(catalog,base,current,derived,calculation,passed):
    steps=[initial(calculation=calculation,grain='NONE')];engine,_=planner(catalog,steps);result=(await turns(engine,steps))[0]
    contract=m.ResultContract.model_validate(result.plan['result_contract']);values={'CURRENT':current,'BASELINE':base,'DERIVED':derived}
    columns=['c'+str(i) for i in range(3)];row={};bindings=[]
    for i,output in enumerate(contract.required_outputs):
        row[columns[i]]=values[output.comparison_role]
        bindings.append(m.OutputBindingProof(output_field_id=output.output_field_id,asl_projection_id='p'+str(i),sql_alias=columns[i],
            result_column_index=i,result_column_name=columns[i],status='PASS',semantic_fingerprint=contract.semantic_fingerprint))
    proof=prove_result_contract(contract,columns=columns,rows=[row],truncated=False,output_bindings=bindings,
        snapshot_id=contract.snapshot_requirement)
    assert (proof.status=='PASS')==passed
    assert next(c for c in proof.checks if c.check_id=='temporal_comparison_arithmetic').status==('PASS' if passed else 'FAIL')


@pytest.mark.asyncio
async def test_pending_metric_answer_materializes_comparison_outputs(catalog):
    from test_v2_pending_recognition import answer
    from app.semantic_v2.pending_recognition import RecognizedClarification
    service=catalog[0]; source=deepcopy(catalog[4][(81,(205,))])
    for metric in source['documents'][0]['metrics']:metric['synonyms']=['销售']
    catalog[4][(81,(205,))]=reseal(source);publish(service,publication_id='comparison-aliases')
    first=initial();text=first[0].replace('销售额','销售');parsed=deepcopy(first[1])
    parsed=parse(text,[('销售','MEASURE','metrics','SET'),('2025年按月','TIME_FIELD','time_spec','SET'),
        ('同比','COMPARISON_BASELINE','comparison_spec','SET')])
    def draft(c):
        d=first[2](c);d['edits']=d['edits'][1:]
        d['ambiguities']=[dict(mention_id='m0',slot_path='metrics',operation='SET',candidate_handles=[v['binding_handle']
            for v in c['catalog_candidates'] if v['mention_id']=='m0' and v['catalog_type']=='METRIC' and '销售' in v['aliases']])]
        return d
    step=text,parsed,draft;engine,_=planner(catalog,[step]);pending=await engine.run(request(question=text,message_id='ask'),IDENTITY)
    assert isinstance(pending,RecognizedClarification)
    result=await answer(catalog,pending,'销售额')
    assert payload(result)['comparison']['output_metrics']==payload(result)['measures']
    assert len([o for o in result.plan['result_contract']['required_outputs'] if 'comparison_role' in o])==3


@pytest.mark.asyncio
async def test_comparison_does_not_inherit_from_an_unrelated_new_task(catalog):
    steps=[initial(),metric_step('销售数量','销售数量')];engine,_=planner(catalog,steps);a,b=await turns(engine,steps)
    assert a.plan['logical_plan']['task_id']!=b.plan['logical_plan']['task_id']
    assert payload(b)['payload_type']=='SCALAR_AGGREGATE' and payload(b)['time'] is None


@pytest.mark.asyncio
async def test_comparison_result_rejects_duplicate_time_buckets(catalog):
    steps=[initial()];engine,_=planner(catalog,steps);result=(await turns(engine,steps))[0]
    contract=m.ResultContract.model_validate(result.plan['result_contract']);columns=[];row={};bindings=[]
    for i,output in enumerate(contract.required_outputs):
        name='c'+str(i);columns.append(name)
        row[name]=dict(CURRENT=12,BASELINE=10,DERIVED=.2).get(getattr(output,'comparison_role',None),'2025-01')
        bindings.append(m.OutputBindingProof(output_field_id=output.output_field_id,asl_projection_id='p'+str(i),
            sql_alias=name,result_column_index=i,result_column_name=name,status='PASS',semantic_fingerprint=contract.semantic_fingerprint))
    proof=prove_result_contract(contract,columns=columns,rows=[row,deepcopy(row)],truncated=False,output_bindings=bindings,
        snapshot_id=contract.snapshot_requirement,actual_time_grain='MONTH')
    assert proof.status=='FAIL' and next(c for c in proof.checks if c.check_id=='cardinality').status=='FAIL'


@pytest.mark.asyncio
async def test_projection_cannot_hide_comparison_time_key(catalog):
    from types import SimpleNamespace
    from app.semantic_v2.result_contract import ResultContractCompiler
    steps=[initial()];engine,_=planner(catalog,steps);result=(await turns(engine,steps))[0]
    p=m.ComparisonPayload.model_validate(payload(result))
    p.projection_spec=m.ProjectionSpec(items=[m.ProjectionItem(output_field_id='time_field:'+p.time.anchor.canonical_id,
        ref=p.measures[0],role='MEASURE',position=0)])
    plan=SimpleNamespace(payload=p,semantic_fingerprint='collision',snapshot_requirement=SimpleNamespace(data_snapshot_id=None))
    with pytest.raises(ValueError,match='COMPARISON_OUTPUT_ID_COLLISION'):ResultContractCompiler.compile(plan)
