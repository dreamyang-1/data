"""Raw recognition, reducer and current catalog proofs for relation occurrences."""
from copy import deepcopy

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.enums import CatalogType
from app.semantic_v2.catalog_paths import relationship_path, validate_path
from app.semantic_v2.catalog_plans import validate_catalog_payload
from app.semantic_v2.recognition import SemanticTaskDraft
from test_v2_catalog_plans import make_catalog, session, bound, payload
from test_v2_raw_turn_recognition import binding, edit, parse, planner, turns


def graph(doc):
    hospital, product = doc['entities'][:2]
    order = dict(entity_id=215, entity_code='order', entity_name='订单', business_domain=205,
        attributes=[dict(attribute_id=1310, attr_code='name', attr_name='订单名称', is_main_attribute=True,
            field_mapping='orders.name')], relations=[])
    doc['entities'].append(order)
    def edge(code, name, target, source, card='1:N'):
        return dict(relation_code=code, relation_name=name, target_entity=target, relation_type=card,
            join_key=dict(source_field=source + '.id', target_field=target + '.owner_id'))
    hospital['relations'].extend([edge('orders', '下单', 'order', 'hospital'),
        edge('parent', '上级', 'hospital', 'hospital', 'N:1')])
    order['relations'] = [edge('items', '明细', 'product', 'order')]
    product['relations'] = [edge('supplied', '供货', 'hospital', 'product', 'N:1')]


@pytest.fixture
def catalog(): return make_catalog(graph)


def path_step(edges=(('下单', 'FORWARD'), ('明细', 'FORWARD')), *, field=None,
              alias=None, project=False, follow=False, operation='SET'):
    text = '沿' + '再'.join(n for n, _ in edges) + '列出结果'
    specs = [(n, 'RELATIONSHIP', 'relationship_spec', operation) for n, _ in edges]
    if field:
        text += '展示' + field if project else '条件' + field + '等于甲'
        specs.append((field, 'PROJECTION_FIELD' if project else 'FILTER_FIELD',
            'projection_spec' if project else 'filter_expression', 'SET'))
    def draft(c):
        c = deepcopy(c)
        c['catalog_candidates'] = [v for v in c['catalog_candidates'] if v.get('owner_entity_code') != 'clinic']
        result = dict(payload_type='RELATION_LIST', relationship_edits=[dict(operation=operation,
            hops=[dict(binding_handle=binding(c, name, 'RELATIONSHIP', 'm' + str(i))['binding_handle'], direction=d)
                for i, (name, d) in enumerate(edges)], evidence_mention_ids=['m' + str(i) for i in range(len(edges))])])
        if field:
            mid = 'm' + str(len(edges)); role = 'PROJECTION_FIELD' if project else 'FILTER_FIELD'
            ref = binding(c, field, role, mid, 'ATTRIBUTE')
            value = (dict(items=[dict(output_field_id='selected', ref=ref, role=role, position=0,
                    **({'entity_alias': alias} if alias is not None else {}))]) if project else
                dict(node_type='ALIASED_PREDICATE' if alias is not None else 'PREDICATE', field_ref=ref,
                    operator='EQ', value=dict(value_type='STRING', value='甲'), source='USER_EXPLICIT',
                    mention_ids=[mid], scope='CURRENT_TASK', **({'entity_alias': alias} if alias is not None else {})))
            result['edits'] = [edit('projection_spec' if project else 'filter_expression', value, ids=(mid,))]
        return result
    return text, parse(text, specs, shape='RELATION_LIST', follow=follow), draft


@pytest.mark.asyncio
@pytest.mark.parametrize('edges,names,cards', [
    ((('下单', 'FORWARD'), ('明细', 'FORWARD')), ['医院', '订单', '商品'], ['ONE_TO_MANY'] * 2),
    ((('明细', 'REVERSE'), ('下单', 'REVERSE')), ['商品', '订单', '医院'], ['MANY_TO_ONE'] * 2),
    ((('上级', 'REVERSE'),), ['医院', '医院'], ['ONE_TO_MANY'])])
async def test_raw_directed_paths_bind_nodes_defaults_and_result_occurrence(catalog, edges, names, cards):
    steps = [path_step(edges)]; engine, transport = planner(catalog, steps)
    result = (await turns(engine, steps))[0]; p = payload(result); path = p['relationship_spec']
    assert [n['entity_ref']['display_name'] for n in path['nodes']] == names
    assert [h['cardinality'] for h in path['hops']] == cards
    assert len({n['entity_alias'] for n in path['nodes']}) == len(names)
    alias = path['nodes'][-1]['entity_alias']
    assert p['projection_spec']['items'][0]['entity_alias'] == alias
    assert {o['entity_alias'] for o in result.plan['result_contract']['required_outputs']} == {alias}
    assert result.plan['backend_contract']['mode'] == 'SHADOW_ONLY' and len(transport.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('field,alias', [('医院名称', 'node:0'), ('订单名称', 'node:1'), ('商品名称', 'node:2')])
@pytest.mark.parametrize('project', [False, True])
async def test_raw_filter_and_projection_own_exact_occurrence(catalog, field, alias, project):
    steps = [path_step(field=field, alias=alias, project=project)]; engine, _ = planner(catalog, steps)
    result = (await turns(engine, steps))[0]; p = payload(result)
    expected = p['relationship_spec']['nodes'][int(alias[-1])]['entity_alias']
    actual = p['projection_spec']['items'][0] if project else p['filters']
    assert actual['entity_alias'] == expected
    if project:
        assert result.plan['result_contract']['required_outputs'][0]['entity_alias'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('edges,field,alias,reason', [
    ((('下单','FORWARD'), ('合作','FORWARD')), None, None, 'PATH_DISCONNECTED'),
    ((('上级','FORWARD'),), '医院名称', None, 'OCCURRENCE_UNRESOLVED'),
    ((('下单','FORWARD'), ('明细','FORWARD')), '医院名称', 'node:1', 'OCCURRENCE_MISMATCH'),
    ((('下单','FORWARD'),), '医院名称', 'node:8', 'ALIAS_NOT_IN_CURRENT_PATH'),
    ((('下单','FORWARD'),), '医院名称', 'invented', 'ALIAS_NOT_IN_CURRENT_PATH')])
async def test_raw_invalid_path_or_field_is_bounded(catalog, edges, field, alias, reason):
    steps = [path_step(edges, field=field, alias=alias)]; engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match=reason): await turns(engine, steps)


@pytest.mark.asyncio
async def test_unique_unaliased_field_remains_resolvable(catalog):
    steps = [path_step(field='订单名称')]; engine, _ = planner(catalog, steps)
    assert payload((await turns(engine, steps))[0])['filters']['field_ref']['display_name'] == '订单名称'


@pytest.mark.asyncio
async def test_self_relation_requires_occurrences_even_for_legacy_single_edit(catalog):
    text = '沿上级列出医院'
    step = (text, parse(text, [('上级', 'RELATIONSHIP', 'relationship_spec', 'SET')]), lambda c: dict(
        payload_type='RELATION_LIST', relationship_edits=[dict(operation='SET', evidence_mention_ids=['m0'],
            binding_handle=binding(c, '上级', 'RELATIONSHIP')['binding_handle'])]))
    engine, _ = planner(catalog, [step])
    with pytest.raises(ValueError, match='SELF_RELATION_REQUIRES_PATH'): await turns(engine, [step])


def built_path(current, names=('下单', '明细')):
    return relationship_path(current, [(bound(current, name, CatalogType.RELATION, 'RELATIONSHIP'), 'FORWARD') for name in names])


@pytest.mark.parametrize('fault', ['alias', 'cardinality', 'direction', 'endpoint'])
def test_compile_rederives_path_not_just_ref_receipts(catalog, fault):
    current = session(catalog); path = built_path(current).model_dump(mode='json')
    if fault == 'alias':
        path['nodes'][0]['entity_alias'] = path['hops'][0]['source_alias'] = 'forged'
    if fault == 'cardinality': path['hops'][0]['cardinality'] = 'MANY_TO_MANY'
    if fault == 'direction': path['hops'][0]['direction'] = 'REVERSE'
    if fault == 'endpoint': path['nodes'][-1]['entity_ref'] = path['nodes'][1]['entity_ref']
    with pytest.raises(ValueError, match='PATH_MISMATCH|PATH_DISCONNECTED'):
        validate_path(current, m.RelationshipPathSpec.model_validate(path))


def test_alias_prefix_stability_does_not_rebind_replacement(catalog):
    current = session(catalog)
    prefix = built_path(current, ('下单',)); extended = built_path(current)
    replacement = built_path(current, ('合作',))
    assert [n.entity_alias for n in prefix.nodes] == [n.entity_alias for n in extended.nodes[:2]]
    assert replacement.nodes[-1].entity_alias != extended.nodes[-1].entity_alias


@pytest.mark.parametrize('bad', [dict(binding_handle='x'), dict(direction='REVERSE'), dict(operation='CLEAR')])
def test_model_cannot_mix_path_and_single_edge(bad):
    with pytest.raises(ValueError): SemanticTaskDraft.model_validate(dict(payload_type='RELATION_LIST',
        relationship_edits=[dict(dict(operation='SET', evidence_mention_ids=['m0'], hops=[dict(binding_handle='x')]), **bad)]))


def test_frozen_plan_rejects_path_and_aliased_variants(catalog):
    from app.semantic_v2.catalog_paths import path_projection
    from app.semantic_v2.catalog_plans import default_projection
    current = session(catalog); path = built_path(current)
    projection = path_projection(default_projection(current, path.target_ref), path)
    for value in [path, projection, m.AliasedOutputFieldRequirement(output_field_id='x', logical_role='TARGET_ENTITY', entity_alias='x')]:
        with pytest.raises(ValueError, match='requires? scoped 0.2.2'): m.require_bounded_legacy_time(value)


@pytest.mark.asyncio
async def test_changed_path_rejects_retained_filter_alias(catalog):
    steps = [path_step(field='商品名称', alias='node:2'),
        path_step((('合作','FORWARD'),), follow=True, operation='REPLACE')]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='OCCURRENCE_MISMATCH'): await turns(engine, steps)


@pytest.mark.asyncio
async def test_path_append_keeps_source_filter_and_rebinds_display(catalog):
    steps = [path_step((('下单','FORWARD'),), field='医院名称', alias='node:0'),
        path_step(follow=True, operation='REPLACE')]
    engine, _ = planner(catalog, steps); a, b = await turns(engine, steps)
    assert payload(a)['filters']['entity_alias'] == payload(b)['filters']['entity_alias']
    assert payload(b)['projection_spec']['items'][0]['ref']['display_name'] == '商品名称'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['ADD', 'REPLACE', 'REMOVE', 'CLEAR'])
async def test_followup_filter_membership_preserves_self_occurrence(catalog, operation):
    first = path_step((('上级','FORWARD'),), field='医院名称', alias='node:1')
    text = operation + '条件甲'
    def draft(c):
        target = c['tasks'][0]['filter_targets'][0]['target_handle']
        value = None if operation in {'REMOVE', 'CLEAR'} else dict(value_type='STRING', value='乙')
        return dict(payload_type='INHERIT', filter_edits=[dict(operation=operation, target_handle=target,
            value=value, evidence_mention_ids=['m0'])])
    step = text, parse(text, [(text, 'FILTER_FIELD', 'filter_expression', operation)], follow=True), draft
    later = path_step((('上级', 'FORWARD'),), operation='REPLACE', follow=True)
    engine, _ = planner(catalog, [first, step, later]); a, b, c = await turns(engine, [first, step, later])
    assert payload(c)['filters'] == payload(b)['filters']
    if operation in {'REMOVE', 'CLEAR'}:
        assert payload(b)['filters'] is None
    else:
        assert payload(b)['filters']['entity_alias'] == payload(a)['filters']['entity_alias']
        assert payload(b)['filters']['operator'] == ('IN' if operation == 'ADD' else 'EQ')


@pytest.mark.asyncio
async def test_alias_changes_semantics_and_survives_nested_serialization(catalog):
    from app.semantic_v2.slot_reducer import semantic_fingerprint
    results = []
    for alias in ['node:0', 'node:1']:
        steps = [path_step((('上级', 'FORWARD'),), field='医院名称', alias=alias)]
        engine, _ = planner(catalog, steps); results.append((await turns(engine, steps))[0])
    left, right = [m.RelationListPayload.model_validate(payload(r)) for r in results]
    assert semantic_fingerprint(left) != semantic_fingerprint(right)
    tree = m.BooleanFilterGroup(operator='OR', children=[left.filters, m.BooleanFilterGroup(operator='NOT', children=[right.filters])])
    encoded = tree.model_dump(mode='json')
    assert encoded['children'][0]['entity_alias'] != encoded['children'][1]['children'][0]['entity_alias']
    assert m.BooleanFilterGroup.model_validate(encoded).model_dump(mode='json') == encoded


@pytest.mark.asyncio
@pytest.mark.parametrize('new_scope', [dict(semantic_model_id=82), dict(business_domain_ids=[]), dict(database_id=8)])
async def test_path_state_cannot_restore_under_changed_request_scope(catalog, new_scope):
    from test_v2_raw_turn_recognition import IDENTITY, request, publish
    first = path_step(); engine, _ = planner(catalog, [first]); saved = (await turns(engine, [first]))[0]
    # Ensure rejection is the restore boundary, not an absent catalog for the new request.
    publish(catalog[0], model=82, domains=[205]); publish(catalog[0], model=81, domains=[])
    text = '继续'; step = text, parse(text, (), follow=True), dict(payload_type='INHERIT')
    engine, _ = planner(catalog, [step])
    with pytest.raises(ValueError, match='SCOPED_STATE_REUSE_REJECTED'):
        await engine.run(request(question=text, message_id='changed', **new_scope), IDENTITY,
            state=saved.next_state, plans=(saved.plan_state,))


@pytest.mark.asyncio
async def test_add_new_condition_to_another_self_occurrence(catalog):
    first = path_step((('上级', 'FORWARD'),), field='医院名称', alias='node:1')
    text = '增加医院名称条件'
    def draft(c):
        c = deepcopy(c); c['catalog_candidates'] = [v for v in c['catalog_candidates'] if v.get('owner_entity_code') != 'clinic']
        return dict(payload_type='INHERIT', filter_edits=[dict(operation='ADD', evidence_mention_ids=['m0'],
            value=dict(node_type='ALIASED_PREDICATE', field_ref=binding(c, '医院名称', 'FILTER_FIELD', kind='ATTRIBUTE'),
                entity_alias='node:0', operator='EQ', value=dict(value_type='STRING', value='乙'),
                source='USER_EXPLICIT', mention_ids=['m0'], scope='CURRENT_TASK'))])
    step = text, parse(text, [('医院名称', 'FILTER_FIELD', 'filter_expression', 'ADD')], follow=True), draft
    engine, _ = planner(catalog, [first, step]); a, b = await turns(engine, [first, step])
    children = payload(b)['filters']['children']
    assert children[0] == payload(a)['filters']
    assert children[0]['entity_alias'] != children[1]['entity_alias']


@pytest.mark.asyncio
async def test_final_result_rejects_output_id_that_hides_target_occurrence(catalog):
    from types import SimpleNamespace
    from app.semantic_v2.result_contract import ResultContractCompiler
    steps = [path_step((('上级', 'FORWARD'),), field='医院名称', alias='node:0', project=True)]
    engine, _ = planner(catalog, steps); result = (await turns(engine, steps))[0]
    data = payload(result); target_id = result.plan['result_contract']['required_outputs'][-1]['output_field_id']
    data['projection_spec']['items'][0]['output_field_id'] = target_id
    plan = SimpleNamespace(payload=m.RelationListPayload.model_validate(data), semantic_fingerprint='test',
        snapshot_requirement=SimpleNamespace(data_snapshot_id=None))
    with pytest.raises(ValueError, match='OCCURRENCE_ID_COLLISION'): ResultContractCompiler.compile(plan)


def test_unscoped_alias_and_unowned_path_fields_reject_at_compiler(catalog):
    from app.semantic_v2.catalog_paths import path_projection
    from app.semantic_v2.catalog_plans import default_projection
    current = session(catalog); path = built_path(current)
    projection = path_projection(default_projection(current, path.target_ref), path)
    with pytest.raises(ValueError, match='ALIAS_REQUIRES_PATH'):
        validate_catalog_payload(current, m.DetailRowsPayload(source_entity=path.target_ref, projection_spec=projection))
    attr = bound(current, '销售额', CatalogType.METRIC, 'MEASURE')
    p = m.RelationListPayload(source_entity=path.source_ref, target_entity=path.target_ref, relationship_spec=path,
        projection_spec=m.ProjectionSpec(items=[m.AliasedProjectionItem(output_field_id='x', ref=attr, role='MEASURE',
            entity_alias=path.nodes[0].entity_alias, position=0)]))
    with pytest.raises(ValueError, match='FIELD_OWNERSHIP_UNSUPPORTED'): validate_catalog_payload(current, p)


def test_current_path_schemas_match_source_and_frozen_contract_stays_separate():
    import json
    from pathlib import Path
    from app.semantic_v2.recognition import value_schema
    from app.semantic_v2.pipeline import AuthorizedLogicalPlan
    from app.semantic_v2.schema import draft_2020_12_schema
    root = Path(__file__).resolve().parents[1] / 'specs/semantic_v2'
    for name, expected in [('semantic_task_draft_v6.schema.json', draft_2020_12_schema(SemanticTaskDraft)),
        ('current_recognition_value_v4.schema.json', value_schema()),
        ('authorized_logical_plan_v0_2_2.schema.json', draft_2020_12_schema(AuthorizedLogicalPlan))]:
        assert json.loads((root / name).read_text(encoding='utf-8')) == expected
    assert 'RelationshipPathSpec' not in (root / '0.2.1/LogicalPlan.schema.json').read_text(encoding='utf-8')
