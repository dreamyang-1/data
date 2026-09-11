"""Generic source-value identity and initial-edit closure for real-business failures."""
from copy import deepcopy

import pytest

from app.semantic_v2.recognition import SemanticTaskDraft, semantic_task_schema
from app.semantic_v2.source_value_repairs import align_source_value_requests
from test_v2_raw_turn_recognition import parse, planner, turns
from test_v2_source_value_binding import catalog, source_step, values


def omit_initial_filter(*, operation='SET', wrong_request_mention=None):
    text, parsed, original = source_step()
    parsed['operation_markers'][0]['operation_hint'] = operation

    def draft(context):
        value = original(context)
        current = value['edits'].pop()
        if operation == 'ADD':
            current['operation'] = 'ADD'
        if wrong_request_mention is not None:
            value['source_value_requests'][0]['mention_id'] = wrong_request_mention
            # Retain the filter when testing the pointer repair.
            value['edits'].append(current)
        return value

    return text, parsed, draft


@pytest.mark.asyncio
async def test_exact_current_add_request_initializes_omitted_filter(catalog, caplog):
    step = omit_initial_filter(operation='ADD')
    with caplog.at_level('INFO', logger='app.semantic_v2.source_value_recognition'):
        result = (await turns(planner(catalog, [step])[0], [step]))[0]
    assert values(result) == ['上海']
    assert result.next_state.source_value_bindings
    traces = [record.source_value_repairs for record in caplog.records
        if hasattr(record, 'source_value_repairs')]
    assert traces and traces[0][-1]['reason_code'] == 'CURRENT_SOURCE_REQUEST_INITIAL_FILTER'
    assert traces[0][-1]['operation'] == 'ADD'


@pytest.mark.asyncio
async def test_omitted_set_filter_stays_rejected_without_model_operand(catalog):
    step = omit_initial_filter(operation='SET')
    with pytest.raises(ValueError, match='V2_SOURCE_VALUE_REQUEST_NOT_APPLIED'):
        await turns(planner(catalog, [step])[0], [step])


@pytest.mark.asyncio
async def test_request_pointer_uses_same_current_identity_as_field_and_edit(catalog, caplog):
    step = omit_initial_filter(wrong_request_mention='m1')
    with caplog.at_level('INFO', logger='app.semantic_v2.source_value_recognition'):
        result = (await turns(planner(catalog, [step])[0], [step]))[0]
    assert values(result) == ['上海']
    traces = [record.source_value_repairs for record in caplog.records
        if hasattr(record, 'source_value_repairs')]
    pointer = next(item for item in traces[0]
        if item['reason_code'] == 'CURRENT_SOURCE_REQUEST_MENTION_IDENTITY')
    assert pointer['from_mention_id'] == 'm1' and pointer['to_mention_id'] == 'm0'


@pytest.mark.asyncio
async def test_omitted_add_repairs_copied_request_pointer_from_exact_field_identity(catalog):
    step = omit_initial_filter(operation='ADD', wrong_request_mention='m1')

    def without_filter(context):
        value = step[2](context)
        value['edits'] = [edit for edit in value['edits']
            if edit['slot_path'] != 'filter_expression']
        return value

    result = (await turns(planner(catalog, [(step[0], step[1], without_filter)])[0],
        [(step[0], step[1], without_filter)]))[0]
    assert values(result) == ['上海']


def test_field_handle_rebind_preserves_catalog_identity_and_current_evidence():
    parsed, draft, handles, base, target = repair_fixture()
    handles['field-old'] = (handles['field'][0], 'FILTER_FIELD', 'm1')
    draft.source_value_requests[0].field_binding_handles = ['field-old']
    repaired, traces = align_source_value_requests(parsed, draft, handles, base=base, target=target)
    assert repaired.source_value_requests[0].field_binding_handles == ['field']
    assert traces == [{'reason_code': 'CURRENT_SOURCE_FIELD_HANDLE_IDENTITY',
        'field': 'source_value_requests[0].field_binding_handles',
        'from_mention_ids': ['m1'], 'to_mention_id': 'm0'}]


def repair_fixture(*, request_mention='m0', handle_mention='m0', evidence=('m0',),
        roles=('FILTER_VALUE',), unresolved=(), target=None, base=0, include_filter=True):
    text = '上海销售额'
    parsed = parse(text, [('上海', 'FILTER_VALUE', 'filter_expression', 'SET'),
        ('销售额', 'MEASURE', 'metrics', 'SET')])
    parsed['mentions'][0]['candidate_roles'] = list(roles)
    parsed = __import__('app.semantic_v2.pipeline', fromlist=['CurrentTurnSemanticParse']).CurrentTurnSemanticParse.model_validate(parsed)
    request = {'request_id': 'value', 'mention_id': request_mention,
        'field_binding_handles': ['field']}
    edits = []
    if include_filter:
        edits.append({'slot_path': 'filter_expression', 'operation': 'SET',
            'evidence_mention_ids': list(evidence), 'value': {
                'node_type': 'PREDICATE', 'field_ref': {'value_field_request_id': 'value'},
                'operator': 'EQ', 'value': {'value_request_id': 'value'},
                'source': 'USER_EXPLICIT', 'scope': 'CURRENT_TASK'}})
    draft = SemanticTaskDraft.model_validate({'payload_type': 'SCALAR_AGGREGATE',
        'source_value_requests': [request], 'edits': edits,
        'unresolved_mention_ids': list(unresolved)})
    handles = {'field': ('attribute:city', 'FILTER_FIELD', handle_mention)}
    return parsed, draft, handles, base, target


@pytest.mark.parametrize('fault', [
    'wrong_identity', 'wrong_role', 'ambiguous', 'non_current', 'out_of_scope',
    'unresolved', 'existing_task', 'competing_filter',
])
def test_identity_repair_fails_closed_for_unproven_or_noncurrent_evidence(fault):
    kwargs = {}
    if fault == 'wrong_identity':
        kwargs.update(request_mention='m1', handle_mention='m0', evidence=('m1',))
    elif fault == 'wrong_role':
        kwargs.update(request_mention='m1', handle_mention='m0', evidence=('m1',))
    elif fault == 'ambiguous':
        kwargs.update(request_mention='m1', handle_mention='m0', evidence=('m0', 'm1'))
    elif fault == 'non_current':
        kwargs.update(request_mention='prior', handle_mention='prior', evidence=('prior',))
    elif fault == 'unresolved':
        kwargs.update(request_mention='m1', handle_mention='m0', evidence=('m0',), unresolved=('m0',))
    elif fault == 'existing_task':
        kwargs.update(include_filter=False, target=object(), base=1)
    elif fault == 'competing_filter':
        kwargs.update(include_filter=True)
    parsed, draft, handles, base, target = repair_fixture(**kwargs)
    if fault == 'wrong_role':
        parsed.mentions[0].candidate_roles = ['SUBJECT_ENTITY']
    if fault == 'ambiguous':
        parsed.mentions[1].candidate_roles = ['FILTER_VALUE', 'MEASURE']
    if fault == 'out_of_scope':
        handles = {}
    before = deepcopy(draft.model_dump(mode='json'))
    repaired, traces = align_source_value_requests(parsed, draft, handles, base=base, target=target)
    assert repaired.model_dump(mode='json') == before
    assert traces == []


def test_generation_schema_restricts_source_request_to_current_filter_values(catalog):
    text, parsed, _ = source_step()
    parsed = __import__('app.semantic_v2.pipeline', fromlist=['CurrentTurnSemanticParse']).CurrentTurnSemanticParse.model_validate(parsed)
    from app.semantic_v2.catalog_bridge import ScopedPlanSession
    from test_v2_raw_turn_recognition import IDENTITY, request
    session = ScopedPlanSession(request(question=text), IDENTITY, catalog[0])
    handles, candidates = __import__('app.semantic_v2.recognition', fromlist=['RawTurnPlanner']).RawTurnPlanner._candidates(session, parsed)
    schema = semantic_task_schema(parsed, {}, context_relation='NEW_TASK', candidates=candidates)
    assert schema['$defs']['SourceValueRequestDraft']['properties']['mention_id']['enum'] == ['m0']
    assert handles


def test_generation_schema_disables_source_requests_without_current_value(catalog):
    parsed = __import__('app.semantic_v2.pipeline', fromlist=['CurrentTurnSemanticParse']).CurrentTurnSemanticParse.model_validate(
        parse('销售额', [('销售额', 'MEASURE', 'metrics', 'SET')]))
    schema = semantic_task_schema(parsed, {}, context_relation='NEW_TASK', candidates=[])
    assert schema['properties']['source_value_requests']['maxItems'] == 0
