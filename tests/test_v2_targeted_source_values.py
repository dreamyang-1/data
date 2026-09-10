from copy import deepcopy

import pytest

from test_v2_source_value_probe import catalog, engine, accept
from test_v2_source_value_binding import catalog as base_catalog, source_step, values
from test_v2_raw_turn_recognition import turns


@pytest.mark.parametrize('fields,target,valid', [
    (['offered'], None, True), ([], 'current-filter', True),
    (['offered'], 'current-filter', False), ([], None, False),
])
def test_source_request_generation_schema_and_runtime_agree(fields, target, valid):
    from app.semantic_v2.source_value_recognition import SourceValueRequestDraft
    from jsonschema import Draft202012Validator
    payload=dict(request_id='request',mention_id='mention',field_binding_handles=fields,target_filter_handle=target)
    validator=Draft202012Validator(SourceValueRequestDraft.model_json_schema())
    assert validator.is_valid(payload)==valid
    if valid: SourceValueRequestDraft.model_validate(payload)
    else:
        with pytest.raises(ValueError): SourceValueRequestDraft.model_validate(payload)


@pytest.mark.asyncio
async def test_orphan_source_request_is_producer_contract_failure_not_missing_consumer(catalog):
    step = source_step('甲')
    def orphan(context):
        draft = step[2](context)
        draft['edits'] = [e for e in draft['edits'] if e['slot_path'] != 'filter_expression']
        return draft
    planner, choices = engine(catalog, [(step[0], step[1], orphan)])
    with pytest.raises(ValueError, match='V2_SOURCE_VALUE_REQUEST_NOT_APPLIED'):
        await turns(planner, [(step[0], step[1], orphan)])
    assert choices == [] and catalog[0][6] == [] and catalog[1] == []


@pytest.mark.asyncio
async def test_same_request_with_explicit_consuming_filter_uses_existing_consumer(catalog):
    step = source_step('甲')
    planner, choices = engine(catalog, [step], [accept('甲市')])
    result = (await turns(planner, [step]))[0]
    assert values(result) == ['甲市'] and len(choices) == 1


@pytest.fixture
def targeted(catalog, monkeypatch):
    import catalog_value_candidates as backend
    catalog[0][5]['city'][:] = ['甲市', '甲县', *[str(i) for i in range(65)]]
    calls = []
    def candidates(scope, field, query, limit):
        assert scope['semantic_model_id'] == 81 and scope['business_domain_ids'] == [205]
        assert field['attr_code'] == 'city' and limit == 8
        calls.append((query, limit))
        return sorted(v for v in catalog[0][5]['city'] if v.startswith(query))[:limit + 1]
    monkeypatch.setattr(backend, 'query_candidates', candidates)
    return catalog, calls


@pytest.mark.asyncio
async def test_large_field_targeted_discovery_selects_then_exactly_revalidates(targeted):
    catalog, calls = targeted
    step = source_step('甲'); planner, choices = engine(catalog, [step], [accept('甲市')])
    result = (await turns(planner, [step]))[0]
    assert values(result) == ['甲市']
    assert len(choices) == 1 and len(choices[0]['candidates']) == 2
    assert calls == [('甲', 8), ('甲', 8)]  # lookup + pin.finish
    assert result.next_state.source_value_bindings[0].ref.resolution_source == 'VERIFIED_SOURCE_EXACT_LOOKUP'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['REJECTED', 'AMBIGUOUS', 'UNRESOLVED'])
async def test_targeted_hits_cannot_auto_bind_even_single_hit(targeted, status):
    catalog, _ = targeted; catalog[0][5]['city'].remove('甲县')
    step = source_step('甲'); planner, choices = engine(catalog, [step], [dict(status=status, candidate_id=None)])
    with pytest.raises(ValueError): await turns(planner, [step])
    assert len(choices) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['invented', 'disappeared', 'drift', 'too_many'])
async def test_targeted_source_safety_invariants(targeted, fault):
    catalog, calls = targeted; step = source_step('甲')
    def decide(context):
        decision = accept('甲市')(context)
        if fault == 'invented': decision['candidate_id'] = 'forged'
        if fault == 'disappeared': catalog[0][5]['city'].remove('甲市')
        if fault == 'drift': catalog[0][5]['city'].append('甲区')
        return decision
    if fault == 'too_many': catalog[0][5]['city'].extend('甲'+str(i) for i in range(9))
    planner, choices = engine(catalog, [step], [decide])
    with pytest.raises(ValueError): await turns(planner, [step])
    if fault == 'too_many': assert choices == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['scope', 'field', 'pin', 'hash', 'source', 'bound'])
async def test_targeted_receipt_cannot_cross_boundaries(targeted, monkeypatch, fault):
    import catalog_value_candidates as backend
    from catalog_release import digest
    original = backend.observe_candidates
    def corrupt(*args):
        receipt = deepcopy(original(*args))
        if fault == 'scope': receipt['scope']['business_domain_ids'] = [206]
        if fault == 'field': receipt['field']['attr_code'] = 'name'
        if fault == 'hash': receipt['observation_hash'] = 'forged'
        if fault == 'source': receipt['source'] = 'VERIFIED_SOURCE_EXACT_LOOKUP'
        if fault == 'bound': receipt['values'] = [str(i) for i in range(9)]
        if fault == 'pin': receipt['query_hash'] = digest('other request')
        if fault != 'hash':
            receipt['observation_hash'] = digest({k:v for k,v in receipt.items() if k not in {'observation_hash','observed_at'}})
        return receipt
    monkeypatch.setattr(backend, 'observe_candidates', corrupt)
    catalog, _ = targeted; step = source_step('甲'); planner, choices = engine(catalog, [step])
    with pytest.raises(ValueError): await turns(planner, [step])
    assert choices == []
