"""Synthetic storage contracts; these are not data-source certifications."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.authorized_contract import contract_digest
from app.semantic_v2.time_storage import TimeStorageContract
from test_v2_asl2_lowering import provider, current, payload, args, bind, sql, seal, embed


def timed(session, year=2025, query_zone='Asia/Shanghai', offset='+08:00'):
    p = payload(session)
    p.time = m.TimeSpec(anchor=bind(session,'ATTRIBUTE','ordered_at','TIME_FIELD'), grain='NONE',
        timezone=query_zone, range=m.TimeRange(start=f'{year}-01-01T00:00:00{offset}',
        end_exclusive=f'{year+1}-01-01T00:00:00{offset}'), source='USER_EXPLICIT',
        as_of=datetime.fromisoformat('2026-09-09T09:00:00+08:00'))
    return p


def evidence(session, p, storage='Asia/Shanghai', precision=0):
    f = session._pin.snapshot['physical_catalog']['tables'][0]['fields'][3]
    return TimeStorageContract(evidence_version='synthetic-v1', provenance='TEST_ONLY',
        evidence_reference='synthetic-unit-test-only', context=session.context,
        field_canonical_id=p.time.anchor.canonical_id, field_mapping='orders.ordered_at',
        physical_field_id=f['field_id'],table_id=f['table_id'],data_source_id=f['data_source_id'],
        physical_field_digest=contract_digest(f),storage_timezone=storage,
        storage_semantics='UTC_DATETIME' if storage=='UTC' else 'LOCAL_WALL_DATETIME',
        fractional_seconds_precision=precision,
        applicability=m.TimeRange(start='2000-01-01T00:00:00Z',end_exclusive='2099-01-01T00:00:00Z'))


def compile_time(session, p, e, **options):
    return session.compile_asl2(sql_planner=sql, time_storage=e,
        time_evidence_digest=options.pop('digest',e.fingerprint),
        allow_test_time_storage=options.pop('allow',True), **args(session,p), **options)


@pytest.mark.parametrize('year',[2025,2026])
@pytest.mark.parametrize('storage',['Asia/Shanghai','UTC'])
def test_annual_boundaries_actual_parameterized_sql(provider,year,storage):
    session=current(provider); p=timed(session,year);e=evidence(session,p,storage)
    low,result=compile_time(session,p,e)
    assert result and result['success'],low.blockers
    values=list(result['sql_parameters'].values())
    expected=([f'{year}-01-01 00:00:00',f'{year+1}-01-01 00:00:00'] if storage=='Asia/Shanghai'
              else [f'{year-1}-12-31 16:00:00',f'{year}-12-31 16:00:00'])
    assert values==expected
    assert '>=' in result['sql'] and ' < ' in result['sql'] and ' <= ' not in result['sql']
    assert 'SUM(orders.amount)' in result['sql'] and 'LIMIT 1' in result['sql']
    assert all(v not in result['sql'] for v in values)
    begin,end=map(datetime.fromisoformat,values)
    assert not begin <= begin-timedelta(microseconds=1) < end
    assert begin <= begin < end and begin <= end-timedelta(microseconds=1) < end
    assert not begin <= end < end
    assert low.time_storage_receipt['provenance']=='TEST_ONLY'


def test_distinct_query_zone_converts_boundary_once(provider):
    session=current(provider);p=timed(session,query_zone='Asia/Tokyo',offset='+09:00')
    low,result=compile_time(session,p,evidence(session,p))
    assert list(result['sql_parameters'].values())==['2024-12-31 23:00:00','2025-12-31 23:00:00']
    # Existing plan freezing normalizes instants to UTC; the instant is unchanged.
    assert p.time.range.start==datetime.fromisoformat('2025-01-01T00:00:00+09:00')


@pytest.mark.parametrize('fault',['digest','field','scope','pin','source','physical','precision','test_optin','period','zone','utc_zone','fiscal'])
def test_storage_evidence_cannot_relax_unknown_or_mismatched_contract(provider,fault):
    session=current(provider);p=timed(session);e=evidence(session,p);options={}
    d=e.model_dump(mode='json')
    if fault=='digest': options['digest']='stale'
    elif fault=='field': d['field_canonical_id']='wrong'
    elif fault=='scope': d['context']['authorized_scope']['semantic_model_id']=82
    elif fault=='pin': d['context']['catalog_pin']['activation_id']='stale'
    elif fault=='source': d['data_source_id']=999
    elif fault=='physical': d['physical_field_digest']='other'
    elif fault=='precision': d['fractional_seconds_precision']=6
    elif fault=='test_optin': options['allow']=False
    elif fault=='period': d['applicability']['start']='2026-01-01T00:00:00Z'
    elif fault=='zone': d['storage_timezone']='America/New_York'
    elif fault=='utc_zone': d['storage_semantics']='UTC_DATETIME'
    elif fault=='fiscal':
        from app.semantic_v2.enums import CalendarType
        p.time.calendar=CalendarType.FISCAL;p.time.fiscal_calendar_id='test';p.time.calendar_policy_version='test'
    e=TimeStorageContract.model_validate(d)
    low,result=compile_time(session,p,e,**options)
    assert low.status=='UNSUPPORTED' and low.asl is None and result is None


@pytest.mark.parametrize('precision',[0,3,6])
def test_physical_fraction_precision_is_preserved_or_rejected(provider,precision):
    data=provider[1]
    data['physical_catalog']['tables'][0]['fields'][3]['data_type']=f'DATETIME({precision})'
    seal(data);provider[0].publish(81,[205],embed_fn=embed,publication_id='precision',producer_revision='test',embedding_contract='fixture')
    session=current(provider);p=timed(session)
    p.time.range=m.TimeRange(start='2025-01-01T00:00:00.123456+08:00',end_exclusive='2026-01-01T00:00:00.123456+08:00')
    low,result=compile_time(session,p,evidence(session,p,precision=precision))
    if precision==6:
        assert result['success'] and list(result['sql_parameters'].values())[0].endswith('.123456')
    else: assert result is None and low.blockers==('ASL2_TIME_PRECISION_LOSS',)


def test_original_boolean_tree_and_time_are_conjoined(provider):
    session=current(provider);p=timed(session)
    field=bind(session,'ATTRIBUTE','name','FILTER_FIELD')
    p.filters=m.BooleanFilterGroup(operator='OR',children=[m.Predicate(field_ref=field,operator='EQ',
        value=m.StringValue(value=v),source='CURRENT_EXPLICIT',scope='ROW') for v in ['A','B']])
    before=p.model_dump(mode='json');low,result=compile_time(session,p,evidence(session,p))
    assert result['success'] and ' OR ' in result['sql'] and ' AND ' in result['sql']
    assert list(result['sql_parameters'].values())==['A','B','2025-01-01 00:00:00','2026-01-01 00:00:00']
    assert p.model_dump(mode='json')==before


@pytest.mark.parametrize('end',['2024-01-01T00:00:00Z','2025-01-01T00:00:00Z'])
def test_invalid_half_open_interval_never_enters_compiler(end):
    with pytest.raises(ValueError):m.TimeRange(start='2025-01-01T00:00:00Z',end_exclusive=end)


def test_independent_no_time_query_uses_native_contract(provider):
    session=current(provider);p=payload(session)
    assert p.time is None
    low,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert result['success'] and low.asl['time_context'] is None and low.asl['filters']==[]
    assert low.time_storage_receipt is None
