from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.asl2 import ASL2Unsupported, lower_asl2, prove_asl2_result
from app.semantic_v2.authorized_contract import AuthorizedVersionMetadata
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.enums import CatalogType, SemanticRole
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from app.semantic_v2.registries import PayloadContractRegistry
from app.semantic_v2.slot_reducer import TaskPatch
from tools.phase25_1.fixtures import candidate_resolution
from test_v2_authorized_catalog_bridge import request, IDENTITY, OAGNET

SQL = OAGNET.parent/'sql-translator'
sys.path.append(str(SQL))
from test_pinned_catalog import snapshot, seal
from pinned_catalog import translate_pinned_catalog
from semantic_scope import RequestScope
from test_catalog_publication import CatalogPublication, RedisCatalogReleaseRegistry, FakeRedis, MemoryStore, embed


@pytest.fixture
def provider():
    data=snapshot(); store=MemoryStore()
    registry=RedisCatalogReleaseRegistry(store.catalog_target_identity,FakeRedis())
    service=CatalogPublication(store,registry,lambda *args: deepcopy(data))
    service.publish(81,[205],embed_fn=embed,publication_id='fixture-asl2',producer_revision='fixture',embedding_contract='fixture')
    return service,data,store,registry


def bind(session,kind,code,role):
    candidates=session.candidates(CatalogType(kind))
    selected=[c for c in candidates if c['canonical_code']==code]
    assert len(selected)==1, selected
    return session.bind(selected[0]['candidate_id'],SemanticRole(role))


def current(provider):
    return ScopedPlanSession(request(),IDENTITY,provider[0])


def payload(session, kind='SCALAR_AGGREGATE', filters=None):
    if kind=='DETAIL_ROWS':
        return m.DetailRowsPayload(source_entity=bind(session,'ENTITY','orders','SOURCE_ENTITY'),
            projection_spec=m.ProjectionSpec(items=[m.ProjectionItem(output_field_id='shown_name',
                ref=bind(session,'ATTRIBUTE','name','PROJECTION_FIELD'),role='PROJECTION_FIELD',position=0)]), filters=filters)
    metric=bind(session,'METRIC','total','MEASURE')
    if kind=='SCALAR_AGGREGATE':return m.ScalarAggregatePayload(measures=[metric],filters=filters)
    group=bind(session,'DIMENSION','customer_name','GROUP_BY')
    return m.GroupedAggregatePayload(measures=[metric],group_by=[group],filters=filters)


def args(session,payload):
    parsed=CurrentTurnSemanticParse()
    resolution=session.resolve_turn(parsed=parsed,task_patch=TaskPatch(base_task_version=0),
        semantic_resolution=candidate_resolution(payload))
    registry=PayloadContractRegistry.get(payload.payload_type)
    return dict(parsed=parsed,resolution=resolution,payload=payload,service_route=registry.allowed_service_routes[0],
        analysis_goals=sorted(registry.required_analysis_goals),task_version=1,
        versions=AuthorizedVersionMetadata(prompt_version='fixture',policy_version='fixture',adapter_version='fixture'))


def sql(pin,scope,asl,**policy):
    return translate_pinned_catalog(pin,RequestScope.from_request({'authorized_semantic_scope':scope.model_dump(mode='json')}),asl,**policy)


@pytest.mark.parametrize('kind',['SCALAR_AGGREGATE','GROUPED_AGGREGATE','DETAIL_ROWS'])
def test_typed_payload_to_actual_sql_and_result_columns(provider,kind):
    session=current(provider); p=payload(session,kind)
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.status=='SUPPORTED_PLAN_ONLY' and result['success']
    assert lowering.asl['version']=='2.0' and lowering.mode=='SHADOW_ONLY'
    assert lowering.can_execute_safely is False
    assert all(b.status=='UNKNOWN' and b.sql_alias in result['sql'] for b in lowering.output_bindings)
    columns=[b.sql_alias for b in lowering.output_bindings]
    proof=prove_asl2_result(lowering,columns=columns,rows=[dict.fromkeys(columns,1)],truncated=False)
    assert proof.status=='PASS',proof
    with pytest.raises(ValueError,match='ALREADY_FINISHED'):session.accept_catalog()


@pytest.mark.parametrize('operator,value,expected',[
    ('EQ',m.StringValue(value="A'lice"),'='),('NE',m.BooleanValue(value=False),'!='),
    ('GTE',m.NumberValue(value='123.45'),'>='),('BETWEEN',m.RangeValue(start=m.NumberValue(value=1),end=m.NumberValue(value=9)),'BETWEEN'),
    ('IN',m.ListValue(values=[m.StringValue(value='A'),m.StringValue(value='B')]),'IN')])
def test_typed_filters_keep_values_and_operators(provider,operator,value,expected):
    session=current(provider)
    p=payload(session,filters=m.Predicate(field_ref=bind(session,'ATTRIBUTE','name','FILTER_FIELD'),operator=operator,
        value=value,source='CURRENT_EXPLICIT',scope='ROW'))
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.status=='SUPPORTED_PLAN_ONLY' and result['success']
    assert lowering.asl['filters'][0]['operator']==expected
    if isinstance(value,m.StringValue):
        assert result['sql_parameters']=={'v2_p0':value.value}
        assert value.value not in result['sql']


@pytest.mark.parametrize('operator,supported',[('AND',True),('OR',False),('NOT',False)])
def test_boolean_relation_is_not_flattened_into_different_logic(provider,operator,supported):
    session=current(provider)
    predicate=m.Predicate(field_ref=bind(session,'ATTRIBUTE','name','FILTER_FIELD'),operator='EQ',value=m.StringValue(value='A'),source='CURRENT_EXPLICIT',scope='ROW')
    expression=m.BooleanFilterGroup(operator=operator,children=[predicate])
    p=payload(session,filters=expression)
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.asl is not None and result['success']
    # The old AND-only ASL list is sufficient only for AND. OR/NOT require
    # the new private policy and cannot silently reuse that old representation.
    assert (lowering.filter_contract is None)==supported
    if not supported:
        assert lowering.asl['filters']==[]
        assert lowering.filter_contract['expression']['operator']==operator
        assert result['filter_contract_hash']


@pytest.mark.parametrize('value',[m.NumberValue(value='0.123456789012345678901'),m.DateTimeValue(value='2026-01-01T00:00:00+08:00')])
def test_numeric_precision_and_timezone_are_not_silently_lost(provider,value):
    session=current(provider)
    p=payload(session,filters=m.Predicate(field_ref=bind(session,'ATTRIBUTE','amount','FILTER_FIELD'),operator='EQ',
        value=value,source='CURRENT_EXPLICIT',scope='ROW'))
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.status=='UNSUPPORTED' and lowering.asl is None and result is None


@pytest.mark.parametrize('kind',['SCALAR_AGGREGATE','GROUPED_AGGREGATE'])
def test_time_range_requires_governed_storage_timezone(provider,kind):
    session=current(provider); p=payload(session,kind)
    p.time=m.TimeSpec(anchor=bind(session,'ATTRIBUTE','ordered_at','TIME_FIELD'),grain='NONE',timezone='Asia/Shanghai',
        range=m.TimeRange(start='2026-01-01T00:00:00+08:00',end_exclusive='2026-02-01T00:00:00+08:00'),
        source='USER_EXPLICIT',as_of=datetime.fromisoformat('2026-01-01T00:00:00+08:00'))
    lowering,result=session.compile_asl2(sql_planner=lambda *args:pytest.fail('Unsupported plan reached SQL'),**args(session,p))
    assert lowering.asl is None and 'ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN' in lowering.blockers


@pytest.mark.parametrize('fault',['authority','inventory','scope_receipt','pin_receipt','projection_receipt'])
def test_plan_and_sql_share_one_pin_acceptance(provider,fault):
    session=current(provider); p=payload(session)
    def altered(pin,scope,asl):
        if fault=='authority':provider[1]['physical_catalog']['sql_translation_sources']['metrics'][0]['dependency_codes']=['changed'];seal(provider[1])
        if fault=='inventory':provider[2].records.pop(next(iter(provider[2].records)))
        result=sql(pin,scope,asl)
        if fault=='scope_receipt':result['authorized_scope_fingerprint']='other'
        if fault=='pin_receipt':result['catalog_pin']['activation_id']='other'
        if fault=='projection_receipt':result['sql_projection_aliases']=['other']
        return result
    with pytest.raises(ValueError,match='ASL2_'):session.compile_asl2(sql_planner=altered,**args(session,p))


@pytest.mark.parametrize('fault',['columns','truncated','row_cap'])
def test_result_binding_does_not_claim_completeness_or_guess_aliases(provider,fault):
    session=current(provider)
    lowering,_=session.compile_asl2(sql_planner=sql,**args(session,payload(session,'DETAIL_ROWS')))
    columns=[b.sql_alias for b in lowering.output_bindings]
    if fault=='columns':columns=['human label']
    rows=[dict.fromkeys(columns,1)]*(10000 if fault=='row_cap' else 1)
    with pytest.raises(ASL2Unsupported):prove_asl2_result(lowering,columns=columns,rows=rows,truncated=fault=='truncated')


def test_explicit_display_limit_is_not_an_implicit_completeness_claim(provider):
    session=current(provider); p=payload(session,'DETAIL_ROWS');p.limit=m.LimitSpec(limit=5)
    lowering,_=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.implicit_row_cap is None and lowering.asl['limit']==5
    columns=[b.sql_alias for b in lowering.output_bindings]
    assert prove_asl2_result(lowering,columns=columns,rows=[dict.fromkeys(columns,1)]*5,truncated=False).status=='PASS'
    with pytest.raises(TypeError):lowering.asl['limit']=9


def test_wrong_scope_plan_is_rejected_before_lowering(provider):
    session=current(provider); p=payload(session)
    plan=session._compile_logical(**args(session,p)).logical_plan
    other=ScopedPlanSession(request(conversation_id='other'),IDENTITY,provider[0])
    with pytest.raises(ASL2Unsupported,match='CURRENT_SCOPE_PIN'):lower_asl2(other,plan)


@pytest.mark.parametrize('ties,nulls', [('INCLUDE_TIES','EXCLUDE'),('EXCLUDE_TIES','LAST')])
def test_ranking_policies_are_not_reduced_to_plain_limit(provider,ties,nulls):
    session=current(provider)
    metric=bind(session,'METRIC','total','MEASURE'); group=bind(session,'DIMENSION','customer_name','GROUP_BY')
    p=m.RankingPayload(measures=[metric],group_by=[group],ranking_target=group,
        ranking=m.RankingSpec(rank_by=metric,direction='DESC',limit=5,ties_policy=ties,nulls_policy=nulls,
            stable_tiebreakers=[bind(session,'DIMENSION','customer_name','ORDER_BY')]))
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    if ties == 'INCLUDE_TIES':
        assert lowering.asl is None and lowering.blockers == ('ASL2_RANK_TIES_CONTRACT_UNSUPPORTED',)
    else:
        assert result['success'] and lowering.ordering_contract['order_by'][0]['nulls_policy'] == nulls
        assert len(lowering.ordering_contract['order_by']) == 2


def test_explicitly_cleared_time_does_not_revive_a_filter(provider):
    session=current(provider); p=payload(session)
    p.time=m.TimeSpec(anchor=bind(session,'ATTRIBUTE','ordered_at','TIME_FIELD'),grain='NONE',timezone='Asia/Shanghai',
        range=None,source='USER_EXPLICIT_UNBOUNDED',as_of=datetime.fromisoformat('2026-01-01T00:00:00+08:00'))
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.asl['time_context'] is None and lowering.asl['filters']==[] and result['success']


def test_output_labels_cannot_change_stable_column_identity(provider):
    session=current(provider); p=payload(session,'DETAIL_ROWS')
    p.projection_spec.items[0].display_label='friendly display label'
    lowering,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert lowering.output_bindings[0].output_field_id=='shown_name'
    assert lowering.output_bindings[0].sql_alias.startswith('v2_')
    assert dict(lowering.display_labels)['shown_name']=='friendly display label'
    assert 'friendly display label' not in result['sql']


def test_multiple_catalog_subjects_require_evidence_instead_of_list_order(provider):
    data=provider[1]; doc=data['documents'][0]
    other=deepcopy(doc['entities'][0]);other.update(entity_id='99',entity_code='other_orders')
    doc['entities'].append(other)
    doc['metrics'][0]['source_dependency']['bind_entity']=['orders','other_orders']
    seal(data)
    provider[0].publish(81,[205],embed_fn=embed,publication_id='fixture-many-owners',producer_revision='fixture',embedding_contract='fixture')
    session=current(provider)
    lowering,result=session.compile_asl2(sql_planner=lambda *a:pytest.fail('Primary subject guessed'),**args(session,payload(session)))
    assert lowering.asl is None and lowering.blockers==('ASL2_SUBJECT_OWNERSHIP_UNRESOLVED',)


def test_explicit_subject_disambiguates_only_a_declared_metric_owner(provider):
    data=provider[1];doc=data['documents'][0]
    other=deepcopy(doc['entities'][0]);other.update(entity_id='99',entity_code='other_orders')
    doc['entities'].append(other)
    doc['metrics'][0]['source_dependency']['bind_entity']=['orders','other_orders']
    seal(data)
    provider[0].publish(81,[205],embed_fn=embed,publication_id='fixture-explicit-owner',
                        producer_revision='fixture',embedding_contract='fixture')
    session=current(provider)
    selected=payload(session)
    selected.subject=bind(session,'ENTITY','orders','SUBJECT_ENTITY')
    plan=session._compile_logical(**args(session,selected)).logical_plan
    lowering=lower_asl2(session,plan)
    assert lowering.status=='SUPPORTED_PLAN_ONLY'
    assert lowering.asl['subject']=={'entity':'orders'}


def test_explicit_subject_outside_metric_dependencies_is_rejected(provider):
    data=provider[1];doc=data['documents'][0]
    other=deepcopy(doc['entities'][0]);other.update(entity_id='99',entity_code='other_orders')
    doc['entities'].append(other);seal(data)
    provider[0].publish(81,[205],embed_fn=embed,publication_id='fixture-wrong-owner',
                        producer_revision='fixture',embedding_contract='fixture')
    session=current(provider)
    selected=payload(session)
    selected.subject=bind(session,'ENTITY','other_orders','SUBJECT_ENTITY')
    plan=session._compile_logical(**args(session,selected)).logical_plan
    lowering=lower_asl2(session,plan)
    assert lowering.blockers==('ASL2_SUBJECT_OWNERSHIP_MISMATCH',)
