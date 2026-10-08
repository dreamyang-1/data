"""Native state and edit contracts with authored model outputs, not live scores."""
from copy import deepcopy
import json

import pytest

from app.semantic_v2.recognition_client import RecognitionFailure
from tools.cutover.harness_observation import RuntimeObserver
from tools.cutover.round58_semantic_checks import check_clear_transition
from test_v2_context_followup_critical_slice import context_catalog, context_case, initial
from test_v2_raw_turn_recognition import IDENTITY, binding, edit, metric_step, parse, planner, request
from test_v2_operation_round1 import active


async def observed(engine, steps):
    state = None; plans = {}; captures = []; results = []
    for index, (text, _, _) in enumerate(steps):
        before = deepcopy(state.model_dump(mode='json')) if state else None
        observer = RuntimeObserver()
        with observer:
            observer.begin_turn('TEST_ONLY', index)
            result = await engine.run(request(question=text, message_id='turn'+str(index)), IDENTITY,
                                      state=state, plans=tuple(plans.values()))
        assert (state.model_dump(mode='json') if state else None) == before
        captures.append(dict(before={'state': before}, result=result.model_dump(mode='json'), error=None,
                             events=observer.turns[0]['events'], state_unchanged=True))
        results.append(result); state = result.next_state
        plans[result.plan['logical_plan']['task_id']] = result.plan_state
    return results, captures


def historical_step(kind):
    text = {'pure': '返回上海销售额的任务', 'time': '返回上海销售额的任务，改成今年',
            'metric': '返回上海销售额的任务，再加订单笔数',
            'group': '返回上海销售额的任务，按城市分组',
            'unconsumed_subject': '返回上海销售额的任务，主体改成医院'}[kind]
    specs = {'pure': [], 'time': [('今年', 'TIME_FIELD', 'time_spec', 'REPLACE')],
             'metric': [('订单笔数', 'MEASURE', 'metrics', 'ADD')],
             'group': [('城市', 'GROUP_BY', 'dimensions', 'ADD')],
             'unconsumed_subject': [('医院', 'SUBJECT_ENTITY', 'subject', None)]}[kind]
    def draft(c):
        value = dict(payload_type='INHERIT', historical_task_handle=next(t['task_handle'] for t in c['tasks'] if t['filter_targets']))
        if kind == 'time':
            value['temporal_edits'] = [dict(component='RANGE', operation='REPLACE', evidence_mention_ids=['m0'],
                value=dict(start='2026-01-01T00:00:00+08:00', end_exclusive='2027-01-01T00:00:00+08:00'))]
        elif kind in ('metric', 'group'):
            slot, name, role = ('metrics', '订单笔数', 'MEASURE') if kind == 'metric' else ('dimensions', '城市', 'GROUP_BY')
            value['edits'] = [edit(slot, [binding(c, name, role)], 'ADD')]
            if kind == 'group': value['payload_type'] = 'GROUPED_AGGREGATE'
        return value
    return text, parse(text, specs, history=True), draft


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['pure', 'time', 'metric', 'group'])
async def test_reference_description_and_current_edit_are_separate(context_catalog, kind):
    steps = [initial('去年上海销售额'), metric_step('订单笔数', '订单笔数'), historical_step(kind)]
    engine, transport = planner(context_catalog, steps)
    results, _ = await observed(engine, steps)
    first, unrelated, returned = results
    assert returned.resolution['dialogue_act'] == 'RETURN_TO_TOPIC'
    assert returned.plan['logical_plan']['task_id'] == first.plan['logical_plan']['task_id']
    other_id = unrelated.plan['logical_plan']['task_id']
    assert returned.next_state.payload['tasks'][other_id] == unrelated.next_state.payload['tasks'][other_id]
    _, old = active(first); _, new = active(returned)
    expected = deepcopy(old['semantics']); actual = new['semantics']
    assert actual['filter_expression'] == expected['filter_expression']
    assert actual['subject'] == expected['subject']
    assert {m['canonical_code'] for m in actual['metrics']} == ({'amount', 'orders'} if kind == 'metric' else {'amount'})
    assert {d['canonical_code'] for d in actual['dimensions']} == ({'city'} if kind == 'group' else set())
    if kind != 'time': assert actual['time_spec'] == expected['time_spec']
    else:
        from app.semantic_v2.models import TimeRange
        assert TimeRange.model_validate(actual['time_spec']['range']) == TimeRange.model_validate(dict(
            start='2026-01-01T00:00:00+08:00', end_exclusive='2027-01-01T00:00:00+08:00'))
    current = json.loads(transport.calls[-1]['messages'][1]['content'])['parse']
    assert len(current['mentions']) == (0 if kind == 'pure' else 1)
    if kind == 'pure': assert actual == expected


@pytest.mark.asyncio
async def test_duplicate_historical_task_description_is_not_reapplied_as_current_edits(context_catalog):
    text = '返回刚才销售额任务'
    reference = (text, parse(text, [
        ('销售额', 'MEASURE', 'metrics', None),
    ], history=True), lambda c: dict(payload_type='INHERIT',
        historical_task_handle=next(t['task_handle'] for t in c['tasks'] if t['filter_targets'])))
    steps = [initial('去年上海销售额'), metric_step('订单笔数', '订单笔数'), reference]
    engine, transport = planner(context_catalog, steps)
    results, _ = await observed(engine, steps)
    first, unrelated, returned = results
    assert returned.resolution['dialogue_act'] == 'RETURN_TO_TOPIC'
    assert returned.plan['logical_plan']['task_id'] == first.plan['logical_plan']['task_id']
    assert active(returned)[1]['semantics'] == active(first)[1]['semantics']
    assert returned.next_state.payload['tasks'][unrelated.plan['logical_plan']['task_id']] == \
        unrelated.next_state.payload['tasks'][unrelated.plan['logical_plan']['task_id']]
    current = json.loads(transport.calls[-1]['messages'][1]['content'])['parse']
    assert current['mentions'] == [] and current['explicit_slot_mentions'] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit,operation', [(True, None), (False, None), (True, 'REPLACE'), (False, 'REPLACE')])
async def test_historical_relation_never_waives_unconsumed_explicit_subject(context_catalog, explicit, operation):
    steps = [initial('去年上海销售额'), metric_step('订单笔数', '订单笔数'), historical_step('unconsumed_subject')]
    steps[-1][1]['mentions'][0]['explicit'] = explicit
    if operation:
        steps[-1][1]['operation_markers'] = [dict(mention_id='m0', slot_name='subject', operation_hint=operation)]
    with pytest.raises(RecognitionFailure, match='V2_EXPLICIT_OPERATION_DROPPED' if operation else 'V2_EXPLICIT_SLOT_DROPPED'):
        await observed(planner(context_catalog, steps)[0], steps)


@pytest.fixture
async def clear_captures(context_catalog):
    steps = context_case(4)
    # An authored legal MODIFY proposal, independently of its CLEAR operation.
    from context_fixture_contract import fixture_proposal
    original = deepcopy(steps[1][1]); current = deepcopy(original)
    def modify(c):
        p = fixture_proposal(original, c); p['relation'] = 'MODIFY'; return p
    current['context_proposal'] = modify
    steps[1] = steps[1][0], current, steps[1][2]
    _, captures = await observed(planner(context_catalog, steps)[0], steps)
    return captures[1:]


@pytest.mark.asyncio
async def test_modify_relation_requires_actual_clear_and_next_turn_barrier(clear_captures):
    assert check_clear_transition(*clear_captures)['status'] == 'PASS_ON_DECLARED_AXES'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing_clear', 'missing_evidence', 'missing_barrier', 'wrong_target',
                                     'wrong_scope', 'resurrection', 'extra_metric', 'missing_ir'])
async def test_clear_evaluator_rejects_unsafe_equivalence(clear_captures, mutation):
    current, later = deepcopy(clear_captures); r = current['result']; target = r['resolution']['target_task_id']
    if mutation == 'missing_clear': r['resolution']['task_patch']['clears'] = []
    elif mutation == 'missing_evidence': r['resolution']['task_patch']['clears'][0]['evidence_mention_ids'] = []
    elif mutation == 'missing_barrier': r['next_state']['payload']['tasks'][target]['clear_barriers'] = []
    elif mutation == 'wrong_target': r['resolution']['target_task_id'] = 'nonexistent'
    elif mutation == 'wrong_scope': r['plan_state']['context']['authorized_scope']['business_domain_ids'] = []
    elif mutation == 'resurrection': later['result']['plan']['logical_plan']['payload']['filters'] = {'node_type': 'PREDICATE'}
    elif mutation == 'extra_metric': r['next_state']['payload']['tasks'][target]['versions'][-1]['semantics']['metrics'] = []
    elif mutation == 'missing_ir': current['events'] = []
    with pytest.raises((AssertionError, KeyError)):
        check_clear_transition(current, later)
