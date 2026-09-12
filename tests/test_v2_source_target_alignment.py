"""Source requests are constrained by the consuming predicate before lookup."""
from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator

from test_v2_raw_turn_recognition import IDENTITY, binding, planner, request, turns
from test_v2_source_value_binding import catalog, source_step, source_edit, referenced_edit, values


@pytest.mark.asyncio
async def test_incompatible_field_is_rejected_before_current_lookup(catalog):
    operation = 'ADD'
    first = source_step(); engine, _ = planner(catalog, [first])
    before = (await turns(engine, [first]))[0]
    catalog[5]['name'].append('北京')  # Exact same text is real in a different field.
    catalog[6].clear()
    text, parsed, correct = source_edit('北京', operation)
    def wrong(context):
        draft = correct(context)
        draft['source_value_requests'][0]['field_binding_handles'] = [
            binding(context, '医院名称', 'FILTER_FIELD', kind='ATTRIBUTE')['binding_handle']]
        return draft
    engine, _ = planner(catalog, [(text, parsed, wrong)])
    original = before.next_state.model_dump(mode='json')
    with pytest.raises(ValueError, match='SOURCE_VALUE_FILTER_FIELD_MISMATCH'):
        await engine.run(request(question=text, message_id='wrong-field'), IDENTITY,
            state=before.next_state, plans=(before.plan_state,))
    assert before.next_state.model_dump(mode='json') == original
    assert ('name', '北京') not in catalog[6]
    assert all(column == 'city' and value == '上海' for column, value in catalog[6])


@pytest.mark.asyncio
@pytest.mark.parametrize('selector,operation', [('field', 'ADD'), ('target', 'REPLACE')])
async def test_admissible_selector_remains_runtime_compatible(catalog, selector, operation):
    step = (source_edit if selector == 'field' else referenced_edit)('北京', operation)
    engine, _ = planner(catalog, [source_step(), step])
    results = await turns(engine, [source_step(), step])
    assert values(results[-1]) == (['上海', '北京'] if operation == 'ADD' else ['北京'])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['REPLACE', 'REMOVE'])
async def test_exported_schema_requires_a_current_target_for_value_mutation(catalog, operation):
    step = referenced_edit('上海' if operation == 'REMOVE' else '北京', operation)
    engine, transport = planner(catalog, [source_step(), step])
    await turns(engine, [source_step(), step])
    # Inspect the actual schema supplied to the second-stage model.
    call = transport.calls[-1]
    schema = json.loads(call['messages'][0]['content'].split('JSON Schema:\n', 1)[1])
    source = schema['$defs']['SourceValueRequestDraft']
    validator = Draft202012Validator({'$defs': schema['$defs'], **source})
    target = source['allOf'][0]['then']['properties']['target_filter_handle']['enum'][0]
    accepted = dict(request_id='r', mention_id='m0', target_filter_handle=target)
    assert validator.is_valid(accepted)
    assert not validator.is_valid(dict(request_id='r', mention_id='m0', field_binding_handles=['offered-field']))
    assert not validator.is_valid({**accepted, 'target_filter_handle': 'invented'})


@pytest.mark.asyncio
async def test_initial_and_add_source_generation_keep_field_selector(catalog):
    steps = [source_step(), source_edit('北京', 'ADD')]
    engine, transport = planner(catalog, steps)
    await turns(engine, steps)
    for call in (transport.calls[1], transport.calls[3]):
        schema = json.loads(call['messages'][0]['content'].split('JSON Schema:\n', 1)[1])
        assert 'allOf' not in schema['$defs']['SourceValueRequestDraft']
    assert ('city', '北京') in catalog[6]
