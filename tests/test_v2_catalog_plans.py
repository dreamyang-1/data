"""Pinned catalog defaults and declared relation plans through raw V2 turns."""
from copy import deepcopy

import pytest

from app.semantic_v2.catalog_plans import default_projection, relationship
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.enums import CatalogType
from test_v2_raw_turn_recognition import (IDENTITY, authority, binding, edit, parse, planner,
    publish, request, reseal, system, turns)
from test_v2_authorized_catalog_bridge import compile_plan


def catalog_source():
    source = authority(); doc = source['documents'][0]; hospital = doc['entities'][0]
    hospital['entity_alias'] = ['机构']
    hospital['attributes'].append(dict(attribute_id=1210, attr_code='name', attr_name='医院名称',
        is_main_attribute=True, field_mapping='hospitals.name'))
    product = dict(entity_id=210, entity_code='product', entity_name='商品', business_domain=205,
        attributes=[dict(attribute_id=1211, attr_code='name', attr_name='商品名称',
            is_main_attribute=True, field_mapping='products.name')], relations=[])
    clinic = deepcopy(hospital); clinic.update(entity_id=211, entity_code='clinic', entity_name='诊所', relations=[])
    for i, attr in enumerate(clinic['attributes']): attr['attribute_id'] = 1220+i
    doc['entities'].extend([product, clinic])
    hospital['relations'] = [dict(relation_code='partner', relation_name='合作', target_entity='product',
        relation_type='1:N', join_key=dict(source_field='hospitals.id', target_field='products.hospital_id'))]
    source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=9, field_name='name', table_id=1))
    source['physical_catalog']['tables'].append(dict(table_id=2, table_name='products', data_source_id=7, semantic_model_id=81,
        fields=[dict(field_id=10, field_name='name', table_id=2)]))
    return source


def make_catalog(change=None):
    service, store, registry, redis, _, overrides = system(); source = catalog_source()
    if change: change(source['documents'][0])
    overrides[(81, (205,))] = reseal(source); publish(service)
    return service, store, registry, redis, overrides


@pytest.fixture
def catalog(): return make_catalog()


def detail(name='医院', operation='SET', follow=False, explicit=False):
    text = '列出' + name + ('城市字段' if explicit else '')
    specs = [(name, 'SUBJECT_ENTITY', 'subject', operation)]
    if explicit: specs.append(('城市', 'PROJECTION_FIELD', 'projection_spec', 'SET'))
    def draft(c):
        edits = [edit('subject', binding(c, name, 'SUBJECT_ENTITY', kind='ENTITY'), operation)]
        if explicit:
            edits.append(edit('projection_spec', dict(items=[dict(output_field_id='city',
                ref=binding(c, '城市', 'PROJECTION_FIELD', 'm1', 'ATTRIBUTE'), role='PROJECTION_FIELD', position=0)]), ids=('m1',)))
        return dict(payload_type='DETAIL_ROWS', edits=edits)
    return text, parse(text, specs, follow=follow, shape='DETAIL_ROWS'), draft


def relation(direction='FORWARD', operation='SET', follow=False):
    text = '医院合作商品' if direction == 'FORWARD' else '商品合作医院'
    return text, parse(text, [('合作', 'RELATIONSHIP', 'relationship_spec', operation)], follow=follow, shape='RELATION_LIST'), lambda c: dict(
        payload_type='RELATION_LIST', relationship_edits=[dict(operation=operation, direction=direction,
            binding_handle=binding(c, '合作', 'RELATIONSHIP')['binding_handle'], evidence_mention_ids=['m0'])])


def payload(result): return result.plan['logical_plan']['payload']


@pytest.mark.asyncio
async def test_detail_uses_governed_default_without_metric_or_field_question(catalog):
    steps = [detail()]; engine, transport = planner(catalog, steps); result = (await turns(engine, steps))[0]
    projection = payload(result)['projection_spec']
    assert projection['mode'] == 'SEMANTIC_DEFAULT'
    assert [i['ref']['display_name'] for i in projection['items']] == ['医院名称']
    assert projection['default_display_policy_id'].startswith('catalog-display:')
    assert result.plan['backend_contract']['mode'] == 'SHADOW_ONLY' and len(transport.calls) == 2


@pytest.mark.asyncio
async def test_replacing_subject_rebinds_default_projection(catalog):
    steps = [detail(), detail('商品', 'REPLACE', True)]; engine, _ = planner(catalog, steps)
    first, second = await turns(engine, steps)
    assert first.plan['logical_plan']['task_id'] == second.plan['logical_plan']['task_id']
    assert [i['ref']['display_name'] for i in payload(second)['projection_spec']['items']] == ['商品名称']
    assert payload(first)['projection_spec']['default_display_policy_id'] != payload(second)['projection_spec']['default_display_policy_id']


@pytest.mark.asyncio
async def test_explicit_projection_is_not_replaced_by_default(catalog):
    # City appears under two entities; select the offered owner, never list position.
    first = detail(explicit=True)
    def draft(c):
        c = deepcopy(c)
        c['catalog_candidates'] = [v for v in c['catalog_candidates'] if v.get('owner_entity_code') != 'clinic']
        return first[2](c)
    steps = [(first[0], first[1], draft)]; engine, _ = planner(catalog, steps)
    result = (await turns(engine, steps))[0]
    assert payload(result)['projection_spec']['mode'] == 'EXPLICIT'
    assert [i['ref']['display_name'] for i in payload(result)['projection_spec']['items']] == ['城市']


@pytest.mark.asyncio
@pytest.mark.parametrize('direction,source,target,card', [('FORWARD', '医院', '商品', 'ONE_TO_MANY'),
    ('REVERSE', '商品', '医院', 'MANY_TO_ONE')])
async def test_declared_relationship_direction_and_target_projection(catalog, direction, source, target, card):
    steps = [relation(direction)]; engine, _ = planner(catalog, steps); result = (await turns(engine, steps))[0]; p = payload(result)
    assert p['source_entity']['display_name'] == source and p['target_entity']['display_name'] == target
    assert p['relationship_spec']['cardinality'] == card
    assert [i['ref']['display_name'] for i in p['projection_spec']['items']] == [target + '名称']
    assert result.plan['result_contract']['expected_cardinality']['kind'] == 'DISTINCT_TARGETS'


@pytest.mark.asyncio
async def test_reversing_relation_changes_default_fields_without_inheriting_old_target(catalog):
    steps = [relation(), relation('REVERSE', 'REPLACE', True)]; engine, _ = planner(catalog, steps)
    first, second = await turns(engine, steps)
    assert first.plan['logical_plan']['task_id'] == second.plan['logical_plan']['task_id']
    assert payload(second)['target_entity']['display_name'] == '医院'
    assert payload(second)['projection_spec']['items'][0]['ref']['display_name'] == '医院名称'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [('missing_main', 'CATALOG_DEFAULT_DISPLAY_MISSING'),
    ('mapping', 'CATALOG_DEFAULT_DISPLAY_CONFLICT'), ('wrong_flag_type', 'CATALOG_DEFAULT_DISPLAY_MISSING')])
async def test_default_governance_gaps_are_system_reasons_not_field_questions(fault, code):
    def change(doc):
        attr = doc['entities'][0]['attributes'][-1]
        if fault == 'missing_main': attr.pop('is_main_attribute')
        if fault == 'wrong_flag_type': attr['is_main_attribute'] = 'true'
        if fault == 'mapping': attr['field_mapping'] = ''
    catalog = make_catalog(change); steps = [detail()]; engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match=code): await turns(engine, steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [('cardinality', 'CATALOG_RELATION_CARDINALITY_MISSING'),
    ('join', 'CATALOG_RELATION_JOIN_EVIDENCE_MISSING'), ('target', 'CATALOG_RELATION_ENDPOINT_UNRESOLVED')])
async def test_relation_gaps_are_not_repaired_by_model_guesses(fault, code):
    def change(doc):
        rel = doc['entities'][0]['relations'][0]
        if fault == 'cardinality': rel['relation_type'] = 'UNKNOWN'
        if fault == 'join': rel.pop('join_key')
        if fault == 'target': rel['target_entity'] = 'foreign'
    catalog = make_catalog(change); steps = [relation()]; engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match=code): await turns(engine, steps)


@pytest.mark.asyncio
async def test_pending_subject_answer_completes_default_projection(catalog):
    from app.semantic_v2.pending_recognition import RecognizedClarification
    text = '列出机构'
    def draft(c):
        return dict(payload_type='DETAIL_ROWS', ambiguities=[dict(mention_id='m0', slot_path='subject', operation='SET',
            candidate_handles=[v['binding_handle'] for v in c['catalog_candidates'] if v['catalog_type'] == 'ENTITY' and '机构' in v['aliases']])])
    step = text, parse(text, [('机构', 'SUBJECT_ENTITY', 'subject', 'SET')]), draft
    engine, _ = planner(catalog, [step]); asked = await engine.run(request(question=text, message_id='ask'), IDENTITY)
    assert isinstance(asked, RecognizedClarification)
    answer = '医院', parse('医院', (), follow=True), {}
    engine, _ = planner(catalog, [answer]); result = await engine.run(request(question='医院', message_id='answer'), IDENTITY,
        state=asked.next_state, pending=asked.pending_state)
    assert payload(result)['projection_spec']['items'][0]['ref']['display_name'] == '医院名称'
    assert payload(result)['source_entity']['display_name'] == '医院'


@pytest.mark.asyncio
async def test_explicit_clear_does_not_silently_restore_catalog_defaults(catalog):
    text = '去掉所有展示字段'
    step = text, parse(text, [(text, 'PROJECTION_FIELD', 'projection_spec', 'CLEAR')], follow=True), dict(
        payload_type='INHERIT', edits=[edit('projection_spec', None, 'CLEAR')])
    engine, _ = planner(catalog, [detail(), step])
    with pytest.raises(ValueError, match='V2_PROJECTION_EXPLICITLY_CLEARED'): await turns(engine, [detail(), step])


def session(catalog): return ScopedPlanSession(request(), IDENTITY, catalog[0])


def bound(current, name, kind, role):
    candidate = next(c for c in current.candidates(kind) if c['display_name'] == name)
    return current.bind(candidate['candidate_id'], role)


@pytest.mark.parametrize('fault', ['policy', 'wrong_entity', 'missing_field'])
def test_compile_rechecks_default_policy_claim_from_trusted_state(catalog, fault):
    from app.semantic_v2.models import DetailRowsPayload, ProjectionSpec
    current = session(catalog); entity = bound(current, '医院', CatalogType.ENTITY, 'SOURCE_ENTITY')
    projection = default_projection(current, entity).model_dump(mode='json')
    if fault == 'policy': projection['default_display_policy_id'] = 'model-generated'
    if fault == 'missing_field': projection['items'] = []
    if fault == 'wrong_entity': entity = bound(current, '商品', CatalogType.ENTITY, 'SOURCE_ENTITY')
    if fault == 'missing_field':
        with pytest.raises(ValueError, match='detail rows require'): DetailRowsPayload(source_entity=entity, projection_spec=projection)
    else:
        code = 'CATALOG_QUERY_ATTRIBUTE_OUTSIDE_DECLARED_ENTITIES' if fault == 'wrong_entity' else 'CATALOG_DEFAULT_DISPLAY_POLICY_MISMATCH'
        with pytest.raises(ValueError, match=code):
            compile_plan(current, DetailRowsPayload(source_entity=entity, projection_spec=ProjectionSpec.model_validate(projection)))


@pytest.mark.parametrize('fault', ['cardinality', 'endpoints', 'missing_relation'])
def test_compiler_rejects_self_declared_relationship_evidence(catalog, fault):
    from app.semantic_v2.models import RelationListPayload, RelationshipSpec
    current = session(catalog); ref = bound(current, '合作', CatalogType.RELATION, 'RELATIONSHIP')
    spec = relationship(current, ref); value = spec.model_dump()
    if fault == 'cardinality': value['cardinality'] = 'ONE_TO_ONE'
    source = spec.target_ref if fault == 'endpoints' else spec.source_ref
    payload = RelationListPayload(source_entity=source, target_entity=spec.target_ref,
        relationship_spec=None if fault == 'missing_relation' else RelationshipSpec.model_validate(value))
    with pytest.raises(ValueError, match='CATALOG_RELATIONSHIP_'):
        compile_plan(current, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [('authority', 'V2_MODEL_OUTPUT_INVALID'),
    ('whole', 'V2_MODEL_OUTPUT_INVALID'), ('operation', 'V2_SLOT_OPERATION_CONFLICT'),
    ('handle', 'V2_BINDING_HANDLE_NOT_OFFERED'), ('role', 'V2_RELATION_BINDING_ROLE_CONFLICT')])
async def test_relation_model_output_cannot_supply_catalog_authority(catalog, fault, code):
    original = relation()
    def corrupt(c):
        d = original[2](c)
        if fault == 'authority': d['relationship_edits'][0]['cardinality'] = 'ONE_TO_ONE'
        if fault == 'whole': d.pop('relationship_edits'); d['edits'] = [edit('relationship_spec', None)]
        if fault == 'operation': d['relationship_edits'][0]['operation'] = 'REPLACE'
        if fault == 'handle': d['relationship_edits'][0]['binding_handle'] = 'foreign'
        if fault == 'role': d['relationship_edits'][0]['binding_handle'] = next(v['binding_handle'] for v in c['catalog_candidates'] if v['role'] == 'RELATION_TARGET')
        return d
    parsed = deepcopy(original[1])
    if fault == 'role': parsed['mentions'][0]['candidate_roles'].append('RELATION_TARGET')
    step = original[0], parsed, corrupt; engine, _ = planner(catalog, [step])
    with pytest.raises(ValueError, match=code): await turns(engine, [step])


@pytest.mark.parametrize('kind', ['DETAIL_ROWS', 'RELATION_LIST'])
def test_query_does_not_import_attribute_from_unrelated_entity(catalog, kind):
    from app.semantic_v2.models import DetailRowsPayload, RelationListPayload, ProjectionSpec, ProjectionItem
    current = session(catalog)
    entity = bound(current, '医院', CatalogType.ENTITY, 'SOURCE_ENTITY')
    candidate = next(c for c in current.candidates(CatalogType.ATTRIBUTE)
        if current._rows[c['candidate_id']].metadata.get('parent') == 'clinic')
    ref = current.bind(candidate['candidate_id'], 'PROJECTION_FIELD')
    projection = ProjectionSpec(items=[ProjectionItem(output_field_id='wrong', ref=ref, role='PROJECTION_FIELD', position=0)])
    if kind == 'RELATION_LIST':
        spec = relationship(current, bound(current, '合作', CatalogType.RELATION, 'RELATIONSHIP'))
        p = RelationListPayload(source_entity=entity, target_entity=spec.target_ref, relationship_spec=spec, projection_spec=projection)
    else:
        p = DetailRowsPayload(source_entity=entity, projection_spec=projection)
    with pytest.raises(ValueError, match='CATALOG_QUERY_ATTRIBUTE_OUTSIDE_DECLARED_ENTITIES'): compile_plan(current, p)


@pytest.mark.asyncio
async def test_multiple_governed_display_attributes_are_complete_and_stable():
    def multiple(doc):
        doc['entities'][0]['attributes'][0]['is_main_attribute'] = True
        doc['entities'][0]['attributes'].reverse()
    catalog = make_catalog(multiple); steps = [detail()]; engine, _ = planner(catalog, steps)
    result = (await turns(engine, steps))[0]
    assert [i['ref']['canonical_code'] for i in payload(result)['projection_spec']['items']] == ['city', 'name']


def test_partial_default_projection_cannot_claim_full_policy():
    from app.semantic_v2.models import DetailRowsPayload, ProjectionSpec
    def multiple(doc): doc['entities'][0]['attributes'][0]['is_main_attribute'] = True
    current = session(make_catalog(multiple)); entity = bound(current, '医院', CatalogType.ENTITY, 'SOURCE_ENTITY')
    data = default_projection(current, entity).model_dump(mode='json'); data['items'] = data['items'][:1]
    with pytest.raises(ValueError, match='CATALOG_DEFAULT_DISPLAY_POLICY_MISMATCH'):
        compile_plan(current, DetailRowsPayload(source_entity=entity, projection_spec=ProjectionSpec.model_validate(data)))


@pytest.mark.asyncio
async def test_subject_cannot_contradict_declared_relationship_direction(catalog):
    original = relation('REVERSE')
    parsed = deepcopy(original[1]); subject = parse(original[0], [('医院', 'SUBJECT_ENTITY', 'subject', 'SET')])
    mention = subject['mentions'][0]; mention['mention_id'] = 'm1'; parsed['mentions'].append(mention)
    parsed['operation_markers'].append(dict(mention_id='m1', operation_hint='SET', slot_name='subject'))
    parsed['explicit_slot_mentions']['subject'] = ['m1']
    def wrong(c):
        d = original[2](c); d['edits'] = [edit('subject', binding(c, '医院', 'SUBJECT_ENTITY', 'm1', 'ENTITY'), ids=('m1',))]; return d
    step = original[0], parsed, wrong; engine, _ = planner(catalog, [step])
    with pytest.raises(ValueError, match='V2_RELATION_SUBJECT_CONFLICT'): await turns(engine, [step])


def test_enum_parent_is_not_mislabeled_as_an_entity_owner(catalog):
    from app.semantic_v2.pipeline import CurrentTurnParser, CurrentTurnSemanticParse
    from app.semantic_v2.recognition import RawTurnPlanner
    parsed = parse('甲城', [('甲城', 'FILTER_VALUE', None, None)])
    parsed['mentions'][0]['source_turn_id'] = 'message'
    parsed = CurrentTurnParser.parse(text='甲城', turn_id='message', text_ref='message', parsed=CurrentTurnSemanticParse.model_validate(parsed))
    _, candidates = RawTurnPlanner._candidates(session(catalog), parsed)
    assert candidates and all('owner_entity_code' not in c for c in candidates)


def test_catalog_completion_preserves_task_reset_without_old_metric_inheritance(catalog):
    from app.semantic_v2.catalog_plans import complete_catalog_defaults
    from app.semantic_v2.models import SlotOperation, TaskSemanticState
    from app.semantic_v2.slot_reducer import TaskPatch
    current = session(catalog)
    old_metric = bound(current, '销售额', CatalogType.METRIC, 'MEASURE')
    entity = bound(current, '医院', CatalogType.ENTITY, 'SOURCE_ENTITY')
    patch = TaskPatch(reset=True, base_task_version=2, sets=[SlotOperation(operation_id='new-subject',
        slot_path='subject', operation='SET', new_value=entity.model_dump(mode='json'), source='CURRENT_EXPLICIT',
        reason_code='NEW_TASK', base_task_version=2, presence='PRESENT')])
    completed, reduced = complete_catalog_defaults(current, 'DETAIL_ROWS', TaskSemanticState(metrics=[old_metric]), patch)
    assert completed.reset and not reduced.semantics.metrics
    assert reduced.semantics.projection_spec.items[0].ref.display_name == '医院名称'
