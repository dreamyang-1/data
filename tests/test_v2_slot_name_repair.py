"""A model's internal slot typo must not mutate the user's business request."""
from copy import deepcopy
import json

import httpx
import pytest

from app.config import Settings
from app.semantic_v2.pipeline import CurrentTurnParser, CurrentTurnSemanticParse
from app.semantic_v2.recognition import current_turn_schema
from app.semantic_v2.recognition_client import RecognitionFailure, RecognitionModelClient


def parsed(slot='display_fields'):
    text = '给出具体的销售订单明细'
    start = text.index('销售订单')
    value = CurrentTurnSemanticParse.model_validate({
        'mentions': [dict(mention_id='m', surface='销售订单', normalized_surface='销售订单',
                         start_char=start, end_char=start+4, candidate_roles=['SUBJECT_ENTITY'],
                         source_turn_id='turn')],
        'operation_markers': [dict(mention_id='m', slot_name=slot, operation_hint='SET')],
        'explicit_slot_mentions': {slot: ['m']},
        'query_shape_prediction': 'DETAIL_ROWS',
        'followup_signals': ['DRILL_DOWN'],
    }).model_dump(mode='json')
    return text, value


def client(outputs, *, stream=False):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert len(calls) <= len(outputs), 'must not retry without a bound'
        value = outputs[len(calls)-1]
        if isinstance(value, httpx.Response):
            return value
        content = json.dumps(value, ensure_ascii=False)
        if stream:
            chunks = [{'choices': [{'delta': {'content': content}, 'finish_reason': None}]},
                      {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                text=''.join('data: '+json.dumps(c, ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
    settings = Settings(_env_file=None, intent_model_api_key='test-key',
                        intent_model_base_url='https://model.invalid/v1', intent_model_max_retries=0)
    return RecognitionModelClient(settings, httpx.MockTransport(handler), force_stream=stream), calls


async def complete(model, *, stage='v2_current_turn', schema=None):
    return await model.complete(stage=stage, instruction='test schema contract',
        context={'question': '给出具体的销售订单明细'}, output_model=CurrentTurnSemanticParse,
        schema=current_turn_schema() if schema is None else schema)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('fault', ['both', 'marker', 'map', 'multiple'])
async def test_slot_correction_keeps_every_other_value_and_evidence(stream, fault):
    text, raw = parsed()
    if fault == 'marker':
        raw['explicit_slot_mentions'] = {'projection_spec': ['m']}
    if fault == 'map':
        raw['operation_markers'][0]['slot_name'] = 'projection_spec'
    fixes = {'display_fields': 'projection_spec'}
    if fault == 'multiple':
        raw['operation_markers'].append(dict(mention_id='m', slot_name='entity', operation_hint='SET'))
        raw['explicit_slot_mentions']['entity'] = ['m']
        fixes['entity'] = 'subject'
    original = deepcopy(raw)
    model, calls = client([raw, {'replacements': fixes}], stream=stream)
    result = await complete(model)
    expected = deepcopy(raw)
    for mark in expected['operation_markers']:
        mark['slot_name'] = fixes.get(mark['slot_name'], mark['slot_name'])
    expected['explicit_slot_mentions'] = {fixes.get(k,k): v for k,v in expected['explicit_slot_mentions'].items()}
    assert result.model_dump(mode='json') == expected and raw == original
    CurrentTurnParser.parse(text=text, turn_id='turn', text_ref='turn', parsed=result)
    assert len(calls) == 2
    repair = json.loads(calls[1]['messages'][1]['content'])
    assert repair['current_parse'] == original
    issued = json.loads(calls[1]['messages'][0]['content'].split('JSON Schema:\n')[1])
    assert set(issued['properties']['replacements']['properties']) == set(fixes)
    assert issued['properties']['replacements']['additionalProperties'] is False


@pytest.mark.asyncio
async def test_valid_slot_does_not_add_model_call():
    _, raw = parsed('projection_spec')
    model, calls = client([raw])
    assert (await complete(model)).model_dump(mode='json') == raw
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('correction', [
    {'replacements': {'display_fields': None}},
    {'replacements': {'display_fields': 'another_unknown'}},
    {'replacements': {}},
    {'replacements': {'display_fields': 'projection_spec', 'time_spec': 'metrics'}},
    {'replacements': {'display_fields': 'projection_spec'}, 'completed_question': 'change the question'},
    httpx.Response(503),
])
async def test_failed_correction_preserves_original_error_and_stops(correction):
    _, raw = parsed()
    model, calls = client([raw, correction])
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_VIOLATION$') as error:
        await complete(model)
    assert error.value.stage == 'v2_current_turn'
    assert error.value.instance_path[0] in {'operation_markers', 'explicit_slot_mentions'}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_key_collision_does_not_merge_or_drop_evidence():
    _, raw = parsed()
    raw['explicit_slot_mentions']['projection_spec'] = ['m']
    model, calls = client([raw, {'replacements': {'display_fields': 'projection_spec'}}])
    with pytest.raises(RecognitionFailure):
        await complete(model)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['operation', 'mention_type', 'query_shape', 'authority', 'empty_schema_constraint'])
async def test_non_slot_schema_errors_cannot_trigger_repair(fault):
    _, raw = parsed()
    schema = current_turn_schema()
    if fault == 'operation': raw['operation_markers'][0]['operation_hint'] = 'DROP_DATABASE'
    if fault == 'mention_type': raw['operation_markers'][0]['mention_id'] = 42
    if fault == 'query_shape': raw['query_shape_prediction'] = 'UNKNOWN_QUERY'
    if fault == 'authority': raw['semantic_model_id'] = 999
    if fault == 'empty_schema_constraint': schema['properties']['mentions']['maxItems'] = 0
    model, calls = client([raw])
    with pytest.raises(RecognitionFailure):
        await complete(model, schema=schema)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_other_stage_slot_enum_does_not_trigger_repair():
    _, raw = parsed()
    model, calls = client([raw])
    with pytest.raises(RecognitionFailure):
        await complete(model, stage='v2_semantic_edits')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slot_correction_does_not_bypass_current_mention_validation():
    text, raw = parsed()
    raw['operation_markers'][0]['mention_id'] = 'not-in-current-turn'
    model, _ = client([raw, {'replacements': {'display_fields': 'projection_spec'}}])
    result = await complete(model)
    with pytest.raises(ValueError, match='nonexistent mention'):
        CurrentTurnParser.parse(text=text, turn_id='turn', text_ref='turn', parsed=result)


@pytest.mark.asyncio
async def test_planner_explains_slot_contract(catalog):
    from test_v2_raw_turn_recognition import IDENTITY, planner, request, metric_step
    from app.semantic_v2.recognition import SLOT_NAME_RULES
    step = metric_step('销售额')
    engine, transport = planner(catalog, [step])
    await engine.run(request(question=step[0], message_id='slot-prompt'), IDENTITY)
    assert SLOT_NAME_RULES in transport.calls[0]['messages'][0]['content']


@pytest.mark.asyncio
async def test_detail_followup_recovers_without_changing_completed_question_or_context(catalog, monkeypatch):
    from test_v2_raw_turn_recognition import IDENTITY, NOW, planner, request, parse
    from app.semantic_v2.context_question import publish_context_task
    from app.semantic_v2.models import ContextQuestionState
    first = request(question='统计2025年第四季度上海市销售额', message_id='previous')
    engine, _ = planner(catalog, [(first.question, parse(first.question), {})])
    engine.defer_new_task_binding = True
    initial = await engine.run(first, IDENTITY, allow_standalone_new_task_passthrough=True)
    state = publish_context_task(initial.next_state, chat=first, created_at=NOW,
        frame=ContextQuestionState(original_question=first.question,
            execution_question=first.question, source_message_id=first.message_id))
    text = '给出具体的销售订单明细'
    completed = '查询2025年第四季度上海市销售订单明细'
    current = parse(text, [('销售订单', 'SUBJECT_ENTITY', 'subject', 'SET'),
                           ('明细', 'PROJECTION_FIELD', 'display_fields', 'SET')],
                    shape='DETAIL_ROWS', follow=True)
    current['completed_question'] = completed
    engine, transport = planner(catalog, [(text, current, {})])
    engine.defer_new_task_binding = True
    repairs = []
    def respond(request_):
        body = json.loads(request_.content)
        context = json.loads(body['messages'][1]['content'])
        if 'invalid_slot_names' in context:
            repairs.append(context)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'replacements': {'display_fields': 'projection_spec'}})}}]})
        return transport(request_)
    engine.model.transport = httpx.MockTransport(respond)
    def forbidden(*args, **kwargs):
        raise AssertionError('context repair must not bind catalog or execute SQL')
    monkeypatch.setattr(engine, '_candidates', forbidden)
    result = await engine.run(request(question=text, message_id='current'), IDENTITY,
        state=state, allow_standalone_new_task_passthrough=True)
    assert result.completed_question == completed
    assert result.fallback_reason == 'ASL_OWNS_CONTEXT_BINDING'
    assert len(transport.calls) == len(repairs) == 1
    assert result.parse.query_shape_prediction == 'DETAIL_ROWS'
    assert result.parse.operation_markers[1].slot_name == 'projection_spec'
    assert repairs[0]['current_parse']['completed_question'] == completed
    assert repairs[0]['current_parse']['context_proposal']['relation'] != 'NEW_TASK'


@pytest.fixture
def catalog():
    from test_v2_raw_turn_recognition import system, authority, reseal, publish
    service, store, registry, redis, _, overrides = system()
    overrides[(81, (205,))] = reseal(authority())
    publish(service)
    return service, store, registry, redis, overrides
