"""Contrast, metamorphic and long-state replay assertions with declared scope."""
import json
from pathlib import Path
import random

import pytest

from app.semantic_v2.models import *
from app.semantic_v2.enums import *
from app.semantic_v2.pipeline import *
from app.semantic_v2.slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint
from app.semantic_v2.state_machine import *
from tools.phase25_1.fixtures import ref, conversation, NOW
from tools.phase25_1.scenarios import CONTRAST_CASES, METAMORPHIC_CASES, contrast_semantics

DOCS = Path(__file__).resolve().parents[1] / 'docs/phase25_1'


@pytest.mark.parametrize('case', CONTRAST_CASES, ids=lambda c:c['id'])
def test_contrast_contracts_distinguish_behavior(case):
    left, right = contrast_semantics(case)
    assert semantic_fingerprint(left) != semantic_fingerprint(right)
    category = case['category']
    if category == 'LIMIT_RANK':
        assert left.preserve_existing_order is True and not hasattr(right, 'preserve_existing_order')
        assert left.limit == right.limit == [5,10,3,2,8][case['parameter']-1]
    elif category == 'ADD_REPLACE':
        assert len(left.metrics) == 2 and len(right.metrics) == 1
        assert 'fixture:sales' in {r.canonical_id for r in left.metrics}
        assert 'fixture:sales' not in {r.canonical_id for r in right.metrics}
    elif category == 'FOLLOWUP_NEW':
        results = []
        for text, facts in ((case['left'], left), (case['right'], right)):
            parse = CurrentTurnParser.parse(text=text, turn_id='c', text_ref='c', parsed=facts)
            results.append(TurnResolver.resolve(parse, state=conversation(), task_patch=TaskPatch(base_task_version=1), semantic_resolution=SemanticResolutionContract(status='UNRESOLVED')))
        assert results[0].target_task_id == 'task:a'
        assert results[1].target_task_id == 'task:c'
        assert results[0].referential_completeness.depends_on_history is True
        assert results[1].referential_completeness.depends_on_history is False
    elif category == 'TREND_COMPARE':
        assert left.payload_type == 'TIME_SERIES' and right.payload_type == 'COMPARISON'
        assert left.group_by == right.group_by == []
    elif category == 'LIST_GROUP':
        assert left.projection_spec.items and right.group_by
    elif category == 'LINEAGE_DEFINITION':
        assert left.lineage_target.target_type == 'FIELD' and right.metric_refs
    elif category == 'REFRESH_REVISE':
        assert left.control_action == ControlAction.REFRESH and right.control_action == ControlAction.REVISE


@pytest.mark.parametrize('case', METAMORPHIC_CASES, ids=lambda c:c['id'])
def test_metamorphic_equivalent_metric_set_expressions(case):
    a, b = (ref(c) for c in case['codes'])
    left = TaskSemanticState(metrics=[a,b])
    right = TaskSemanticState(metrics=[b,a])
    assert semantic_fingerprint(left) == semantic_fingerprint(right)
    # Projection order remains a real user-visible semantic distinction.
    def projected(items):
        return TaskSemanticState(projection_spec=ProjectionSpec(items=[ProjectionItem(output_field_id=r.canonical_id, ref=r, role='PROJECTION_FIELD', position=i) for i,r in enumerate(items)]))
    assert semantic_fingerprint(projected([a,b])) != semantic_fingerprint(projected([b,a]))


SCENES = ['hospital_sales','dealer_orders','product_returns','warehouse_stock','department_purchases']


@pytest.mark.parametrize('scene', SCENES)
def test_twenty_turn_fixtures_replay_full_state_and_pending(scene):
    records = json.loads((DOCS / 'long_conversations' / (scene+'.json')).read_text(encoding='utf-8'))
    assert 20 <= len(records) <= 30
    prior = None
    for row in records:
        state = ConversationState.model_validate(row['state_before'])
        if prior is not None:
            assert state == prior
        parsed = CurrentTurnParseResult.model_validate(row['expected_parse'])
        rebuilt = CurrentTurnParser.parse(text=row['current_turn'], turn_id=parsed.turn_id, text_ref=parsed.text_ref,
            parsed=CurrentTurnSemanticParse.model_validate(parsed.model_dump(exclude={'turn_id','text_ref','text_digest'})))
        assert rebuilt == parsed
        mutation = StateMutation.model_validate(row['mutation'])
        next_state = apply_state_mutation(state, mutation)
        assert next_state == ConversationState.model_validate(row['expected_state_after'])
        assert apply_state_mutation(next_state, mutation) == next_state
        assert state.state_version + 1 == next_state.state_version
        assert next_state.active_topic_id == row['expected_topic']
        active = next_state.tasks[next_state.topics[next_state.active_topic_id].active_task_id]
        assert active.active_version == row['expected_task_version']
        pending = next_state.pending_records.get('pending:a')
        assert (pending.status if pending else 'NONE') == row['expected_pending']
        if row['expected_execution_attempt']:
            assert mutation.execution_attempt.task_version == active.active_version
            assert len(next_state.execution_attempts) == len(state.execution_attempts) + 1
        prior = next_state
    assert len(prior.execution_attempts) == 2
    assert prior.pending_records['pending:a'].status == 'RESOLVED'
    assert prior.tasks['task:a'].active_version == 11


def test_pending_wrong_option_patch_does_not_commit_or_clear_remaining_blockers():
    records = json.loads((DOCS/'long_conversations/hospital_sales.json').read_text(encoding='utf-8'))
    answer = records[11]
    state = ConversationState.model_validate(answer['state_before'])
    mutation = StateMutation.model_validate(answer['mutation'])
    bad_data = mutation.model_dump()
    bad_data['task_patch']['sets'][0]['new_value'] = [ref('not-selected').model_dump(mode='json')]
    with pytest.raises(ValueError, match='selected option value'):
        apply_state_mutation(state, StateMutation.model_validate(bad_data))
    assert state.pending_records['pending:a'].status == 'ACTIVE'
    result = apply_state_mutation(state, mutation)
    assert result.pending_records['pending:a'].active_blocker_id == 'dimension-choice'
    assert result.pending_records['pending:a'].status == 'ACTIVE'


def test_seeded_mutation_replays_do_not_add_versions():
    records = json.loads((DOCS/'long_conversations/dealer_orders.json').read_text(encoding='utf-8'))
    state = ConversationState.model_validate(records[-1]['expected_state_after'])
    rng = random.Random(251)
    for _ in range(100):
        row = rng.choice(records)
        state = apply_state_mutation(state, StateMutation.model_validate(row['mutation']))
    assert state == ConversationState.model_validate(records[-1]['expected_state_after'])


def test_dataset_root_and_current_reference_have_different_frozen_answers():
    fixture = json.loads((DOCS/'dataset_ancestry_fixture.json').read_text(encoding='utf-8'))
    for turn in fixture['turns']:
        source = fixture[turn['source_dataset_id']]
        assert source['rows'][:10] == turn['expected_rows']
    assert len(fixture['turns'][0]['expected_rows']) == 5
    assert len(fixture['turns'][1]['expected_rows']) == 10


def test_unlabeled_gold_fields_use_null_and_do_not_become_complete():
    rows = [json.loads(line) for line in (DOCS/'gold_v0_2_1.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 200
    for row in rows:
        assert row['annotation_status'] == 'PARTIAL'
        assert all(row[key] is None for key in row['unlabeled_fields'])
        assert 'NEEDS_BUSINESS_REVIEW' not in json.dumps(row)
