from copy import deepcopy
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app.semantic_v2.recognition import SemanticTaskDraft, semantic_task_schema
from test_v2_source_value_binding import catalog as source_catalog, source_step, values
from test_v2_raw_turn_recognition import catalog as base_catalog, planner, turns


def schema():
    return semantic_task_schema(SimpleNamespace(reference_signals=[], topic_shift_signals=[]), {})


def predicate():
    return dict(node_type='PREDICATE', field_ref={'value_field_request_id': 'request'},
        operator='EQ', value={'value_request_id': 'request'}, source='USER_EXPLICIT', scope='CURRENT_TASK')


def valid(edit):
    return Draft202012Validator(schema()).is_valid(dict(payload_type='SCALAR_AGGREGATE', filter_edits=[edit]))


@pytest.mark.parametrize('fault', ['bare_value', 'dataset_scope', 'missing_operator', 'authority', 'nonexplicit'])
def test_generation_rejects_incomplete_or_noncurrent_new_predicates(fault):
    operand = predicate()
    if fault == 'bare_value': operand = {'value_request_id': 'request'}
    if fault == 'dataset_scope': operand['scope'] = 'CURRENT_DATASET'
    if fault == 'missing_operator': operand.pop('operator')
    if fault == 'authority': operand['field_ref'] = {'canonical_id': 'invented'}
    if fault == 'nonexplicit': operand['source'] = 'INHERITED'
    edit = dict(operation='ADD', target_handle=None, value=operand, evidence_mention_ids=['current'])
    # Reproduce the old exported-contract hole without changing runtime parsing.
    assert Draft202012Validator(SemanticTaskDraft.model_json_schema()).is_valid(
        dict(payload_type='SCALAR_AGGREGATE', filter_edits=[edit]))
    assert not valid(edit)


@pytest.mark.parametrize('operation,target,operand,accepted', [
    ('ADD', None, predicate(), True),
    ('ADD', 'current', {'value_request_id': 'request'}, True),
    ('REPLACE', 'current', {'value_request_id': 'request'}, True),
    ('REPLACE', 'current', {'value_type': 'LIST', 'values': [{'value_request_id': 'request'}]}, True),
    ('REMOVE', 'current', {'value_request_id': 'request'}, True),
    ('REMOVE', 'current', None, True),
    ('CLEAR', 'current', None, True),
    ('CLEAR', 'current', {'value_request_id': 'request'}, False),
    ('REPLACE', None, {'value_request_id': 'request'}, False),
    ('REMOVE', None, None, False),
    ('REPLACE', 'current', predicate(), False),
    ('ADD', 'current', predicate(), False),
])
def test_operand_alternatives_preserve_operations(operation, target, operand, accepted):
    assert valid(dict(operation=operation, target_handle=target, value=operand, evidence_mention_ids=['current'])) == accepted


def test_schema_preserves_alias_predicates_and_is_stable():
    item = predicate(); item.update(node_type='ALIASED_PREDICATE', entity_alias='node:1')
    assert valid(dict(operation='ADD', value=item, evidence_mention_ids=['current']))
    first = schema(); Draft202012Validator.check_schema(first)
    assert first == schema()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'bare_value', 'dataset_scope'])
async def test_actual_runtime_keeps_native_binding_and_rejection(source_catalog, fault):
    text, parsed, original = source_step(); parsed['operation_markers'] = []
    def draft(context):
        output = original(context); item = output['edits'].pop()
        if fault == 'bare_value': item['value'] = item['value']['value']
        if fault == 'dataset_scope': item['value']['scope'] = 'CURRENT_DATASET'
        output['filter_edits'] = [dict(operation='ADD', target_handle=None,
            value=item['value'], evidence_mention_ids=item['evidence_mention_ids'])]
        return output
    steps = [(text, parsed, draft)]; engine, transport = planner(source_catalog, steps)
    if fault:
        with pytest.raises(ValueError, match='V2_SLOT_OPERATION_CONFLICT'):
            await turns(engine, steps)
    else:
        result = (await turns(engine, steps))[0]
        assert values(result) == ['上海'] and result.plan_state.source_value_bindings
        assert len(transport.calls) == 2
