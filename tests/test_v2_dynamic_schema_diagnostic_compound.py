"""Compound-query dynamic schema diagnostics: bounded, redacted, fail-closed.

The positive case covers a multi-value filter, projection, metric and ranking
through the real issued dynamic schema. No raw model output, user text or
catalog values are recorded in diagnostics.
"""
import json

import httpx
import pytest

from app.config import Settings
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from app.semantic_v2.recognition import RawTurnPlanner, SemanticTaskDraft, semantic_task_schema
from app.semantic_v2.recognition_client import (
    RecognitionFailure, RecognitionModelClient, _validate_exact_dynamic_schema,
)
from test_v2_raw_turn_recognition import (
    IDENTITY, authority, edit, parse, planner, publish, request, reseal, system, turns,
)

COMPOUND_TEXT = '上海北京医院的城市销售额明细与销售额排序'


@pytest.fixture
def catalog():
    service, store, registry, redis, _, overrides = system()
    overrides[(81, (205,))] = reseal(authority())
    publish(service)
    return service, store, registry, redis, overrides


def compound_parse_dict():
    parsed = parse(COMPOUND_TEXT, [
        ('上海', 'FILTER_VALUE', 'filter_expression', 'SET'),
        ('北京', 'FILTER_VALUE', 'filter_expression', 'SET'),
        ('医院', 'SUBJECT_ENTITY', 'subject', 'SET'),
        ('城市', 'GROUP_BY', 'dimensions', 'SET'),
        ('销售额', 'MEASURE', 'metrics', 'SET'),
        ('明细', 'PROJECTION_FIELD', 'projection_spec', 'SET'),
        ('排序', 'ORDER_BY', 'ranking_spec', 'SET'),
    ])
    # Value mentions keep their lookup role while also offering the attribute
    # field that carries the multi-value predicate.
    parsed['mentions'][0]['candidate_roles'] = ['FILTER_VALUE', 'FILTER_FIELD']
    parsed['mentions'][1]['candidate_roles'] = ['FILTER_VALUE', 'FILTER_FIELD']
    parsed['mentions'][5]['candidate_roles'] = ['PROJECTION_FIELD']
    parsed['mentions'][6]['candidate_roles'] = ['ORDER_BY']
    return parsed


def compound_candidates(catalog):
    session = ScopedPlanSession(request(question=COMPOUND_TEXT), IDENTITY, catalog[0])
    parsed = CurrentTurnSemanticParse.model_validate(compound_parse_dict())
    handles, candidates = RawTurnPlanner._candidates(session, parsed)
    return parsed, handles, candidates


def handle(candidates, name, role, mention, kind=None):
    found = [c for c in candidates if c['name'] == name and c['role'] == role
        and c['mention_id'] == mention and (kind is None or c['catalog_type'] == kind)]
    assert len(found) == 1, found
    return {'binding_handle': found[0]['binding_handle']}


def compound_payload(candidates):
    tree = dict(node_type='PREDICATE',
        field_ref=handle(candidates, '城市', 'FILTER_FIELD', 'm0', 'ATTRIBUTE'),
        operator='IN', value=dict(value_type='LIST', values=[
            dict(value_type='STRING', value='甲城'),
            dict(value_type='STRING', value='乙城')]),
        source='USER_EXPLICIT', scope='CURRENT_TASK')
    projection = dict(mode='EXPLICIT', items=[dict(output_field_id='projection:city',
        ref=handle(candidates, '城市', 'PROJECTION_FIELD', 'm5', 'ATTRIBUTE'),
        role='PROJECTION_FIELD', position=0)])
    ranking = dict(rank_by=handle(candidates, '销售额', 'ORDER_BY', 'm6'),
        direction='DESC', limit=10)
    return dict(payload_type='RANKING', edits=[
        edit('subject', handle(candidates, '医院', 'SUBJECT_ENTITY', 'm2', 'ENTITY'), ids=('m2',)),
        edit('metrics', [handle(candidates, '销售额', 'MEASURE', 'm4')], ids=('m4',)),
        edit('dimensions', [handle(candidates, '城市', 'GROUP_BY', 'm3', 'DIMENSION')], ids=('m3',)),
        edit('filter_expression', tree, ids=('m0', 'm1')),
        edit('projection_spec', projection, ids=('m5',)),
        edit('ranking_spec', ranking, ids=('m6',)),
    ])


def client_returning(payload):
    def handler(request_):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})
    settings = Settings(_env_file=None, intent_model_api_key='test-only-key',
        intent_model_base_url='https://model.invalid/v1', intent_model_max_retries=0)
    return RecognitionModelClient(settings, httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_correct_compound_output_passes_issued_dynamic_schema(catalog):
    parsed, _, candidates = compound_candidates(catalog)
    schema = semantic_task_schema(parsed, {}, context_relation='NEW_TASK', candidates=candidates)
    payload = compound_payload(candidates)
    draft = await client_returning(payload).complete(
        stage='v2_semantic_edits', instruction='fixture', context={},
        output_model=SemanticTaskDraft, schema=schema)
    assert draft.payload_type == 'RANKING'
    assert [e.slot_path for e in draft.edits] == [
        'subject', 'metrics', 'dimensions', 'filter_expression', 'projection_spec', 'ranking_spec']


@pytest.mark.asyncio
async def test_compound_plan_completes_end_to_end(catalog):
    step = (COMPOUND_TEXT, compound_parse_dict(),
        lambda c: compound_payload(c['catalog_candidates']))
    engine, transport = planner(catalog, [step])
    results = await turns(engine, [step])
    payload = results[0].plan['logical_plan']['payload']
    assert payload['payload_type'] == 'RANKING'
    assert payload['ranking']['direction'] == 'DESC'
    assert payload['filters']['operator'] == 'IN'
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_unoffered_handle_still_fails_closed_end_to_end(catalog):
    def bad_handle(c):
        value = compound_payload(c['catalog_candidates'])
        value['edits'][0]['value'] = {'binding_handle': 'binding:not-offered'}
        return value
    step = (COMPOUND_TEXT, compound_parse_dict(), bad_handle)
    engine, _ = planner(catalog, [step])
    with pytest.raises(ValueError, match='^V2_BINDING_HANDLE_NOT_OFFERED$'):
        await turns(engine, [step])


@pytest.mark.asyncio
async def test_additional_property_is_rejected_with_bounded_diagnostics(catalog):
    parsed, _, candidates = compound_candidates(catalog)
    schema = semantic_task_schema(parsed, {}, context_relation='NEW_TASK', candidates=candidates)
    payload = compound_payload(candidates)
    payload['unauthorized_field'] = 'CONFIDENTIAL-ORIGINAL-VALUE'
    client = client_returning(payload)
    with pytest.raises(RecognitionFailure) as excinfo:
        await client.complete(stage='v2_semantic_edits', instruction='fixture', context={},
            output_model=SemanticTaskDraft, schema=schema)
    exc = excinfo.value
    assert str(exc) == 'V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'
    assert exc.stage == 'v2_semantic_edits'
    assert isinstance(exc.instance_path, tuple)
    assert isinstance(exc.schema_path, tuple)
    assert exc.validator == 'additionalProperties'
    blob = json.dumps([list(exc.instance_path), list(exc.schema_path), exc.validator])
    assert 'CONFIDENTIAL-ORIGINAL-VALUE' not in blob


@pytest.mark.asyncio
async def test_unoffered_source_request_mention_is_rejected(catalog):
    parsed, _, candidates = compound_candidates(catalog)
    schema = semantic_task_schema(parsed, {}, context_relation='NEW_TASK', candidates=candidates)
    payload = compound_payload(candidates)
    payload['source_value_requests'] = [{'request_id': 'r1', 'mention_id': 'not-offered',
        'field_binding_handles': []}]
    with pytest.raises(RecognitionFailure) as excinfo:
        await client_returning(payload).complete(
            stage='v2_semantic_edits', instruction='fixture', context={},
            output_model=SemanticTaskDraft, schema=schema)
    exc = excinfo.value
    assert str(exc) == 'V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'
    assert exc.instance_path[:2] == ('source_value_requests', 0)
    assert exc.validator is not None


def test_diagnostic_paths_are_bounded_and_value_free():
    schema = {'type': 'object'}
    current = schema
    instance = {}
    node = instance
    for index in range(20):
        name = 'level_' + str(index) + '_' + 'x' * 120
        current['properties'] = {name: {'type': 'object'}}
        current = current['properties'][name]
        node[name] = {}
        node = node[name]
    current['properties'] = {'leaf': {'type': 'integer'}}
    node['leaf'] = 'CONFIDENTIAL-LEAF-VALUE'
    with pytest.raises(RecognitionFailure) as excinfo:
        _validate_exact_dynamic_schema(instance, schema, stage='v2_semantic_edits')
    exc = excinfo.value
    assert str(exc) == 'V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'
    assert len(exc.instance_path) <= 16
    assert len(exc.schema_path) <= 16
    assert all(len(str(segment)) <= 80 for segment in exc.instance_path)
    assert all(len(str(segment)) <= 80 for segment in exc.schema_path)
    assert 'CONFIDENTIAL' not in json.dumps([list(exc.instance_path),
        list(exc.schema_path), exc.validator, str(exc), exc.stage])


def test_invalid_schema_error_code_stays_compatible():
    with pytest.raises(RecognitionFailure) as excinfo:
        _validate_exact_dynamic_schema({}, {'type': 'not-a-type'})
    assert str(excinfo.value) == 'V2_MODEL_DYNAMIC_SCHEMA_INVALID'
