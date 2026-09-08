"""Raw scoped V2 edit integration; deterministic HTTP fixtures, never model accuracy."""
from copy import deepcopy

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.pipeline import validate_bound_ref_scope_and_permission
from test_v2_raw_turn_recognition import (IDENTITY, NOW, authority, binding, edit,
    metric_step, parse, planner, publish, request, reseal, system, turns)


@pytest.fixture
def catalog():
    service, store, registry, redis, _, overrides = system()
    source = authority(); doc = source['documents'][0]
    doc['metrics'].append(dict(metric_code='quantity', metric_name='销售数量', business_domain=205, formula='SUM(quantity)'))
    for identifier, code, name in [(1206, 'product', '商品'), (1207, 'order_date', '订单日期')]:
        doc['entities'][0]['attributes'].append(dict(attribute_id=identifier, attr_code=code,
            attr_name=name, field_mapping='hospitals.' + code))
        source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=identifier, field_name=code, table_id=1))
    doc['dimensions'].append(dict(dim_code='order_date', dim_name='订单日期',
        bind_entities=[dict(entity='205', attr='1207', businessDomain='205')]))
    overrides[(81, (205,))] = reseal(source); publish(service)
    return service, store, registry, redis, overrides


def scalar(value):
    return dict(value_type='STRING', value=value)


def predicate(c, name, value, mention='m0'):
    return dict(node_type='PREDICATE', field_ref=binding(c, name, 'FILTER_FIELD', mention, 'ATTRIBUTE'),
        operator='EQ', value=scalar(value), source='USER_EXPLICIT', scope='CURRENT_TASK')


def initial(*, nested=False, time=False):
    text = '上海商品甲2025年按月销售额' if time else '上海商品甲销售额'
    specs = [('上海', 'FILTER_FIELD', 'filter_expression', 'SET'), ('商品甲', 'FILTER_FIELD', 'filter_expression', 'SET'),
        ('销售额', 'MEASURE', 'metrics', 'SET')]
    if time: specs.append(('2025年按月', 'TIME_FIELD', 'time_spec', 'SET'))
    def draft(c):
        region = predicate(c, '城市', '上海')
        if nested:
            region = dict(node_type='BOOLEAN_GROUP', operator='OR', children=[region,
                dict(node_type='BOOLEAN_GROUP', operator='NOT', children=[predicate(c, '城市', '北京')])])
        filters = dict(node_type='BOOLEAN_GROUP', operator='AND', children=[region, predicate(c, '商品', '甲', 'm1')])
        edits = [edit('metrics', [binding(c, '销售额', 'MEASURE', 'm2')], ids=('m2',)),
            edit('filter_expression', filters, ids=('m0', 'm1'))]
        if time:
            edits.append(edit('time_spec', dict(anchor=binding(c, '订单日期', 'TIME_FIELD', 'm3', 'DIMENSION'),
                range=dict(start='2025-01-01T00:00:00+08:00', end_exclusive='2026-01-01T00:00:00+08:00'),
                grain='MONTH', timezone='Asia/Shanghai', source='USER_EXPLICIT', as_of=NOW.isoformat()), ids=('m3',)))
        return dict(payload_type='TIME_SERIES' if time else 'SCALAR_AGGREGATE', edits=edits)
    return text, parse(text, specs), draft


def target(c, name='城市', value=None, *, task_index=-1):
    return next(t['target_handle'] for t in c['tasks'][task_index]['filter_targets']
        if t['expression'].get('field_ref', {}).get('name') == name
        and (value is None or t['expression']['value'].get('value') == value))


def filter_step(text, operation, value=None, name='城市', *, follow=True, transform=None):
    def draft(c):
        result = dict(payload_type='INHERIT', filter_edits=[dict(operation=operation,
            target_handle=target(c, name), evidence_mention_ids=['m0'], value=value)])
        return transform(c, result) if transform else result
    return text, parse(text, [(text, 'FILTER_FIELD', 'filter_expression', operation)], follow=follow), draft


def temporal_step(text, component, operation, value=None, *, kind='INHERIT'):
    return text, parse(text, [(text, 'TIME_FIELD', 'time_spec', operation)], follow=True), dict(
        payload_type=kind, temporal_edits=[dict(component=component, operation=operation, value=value, evidence_mention_ids=['m0'])])


def payload(result):
    return result.plan['logical_plan']['payload']


def leaves(node):
    if node is None: return []
    return [node] if node['node_type'] == 'PREDICATE' else [v for child in node['children'] for v in leaves(child)]


@pytest.mark.asyncio
async def test_filter_add_replace_remove_clear_preserve_other_conditions_across_turns(catalog):
    steps = [initial(time=True), filter_step('地区再加北京', 'ADD', scalar('北京')),
        filter_step('地区換成江苏', 'REPLACE', scalar('江苏')),
        filter_step('地区再加浙江', 'ADD', scalar('浙江')),
        filter_step('不要浙江', 'REMOVE', scalar('浙江')), filter_step('不限地区', 'CLEAR'),
        temporal_step('按季度', 'GRAIN', 'REPLACE', 'QUARTER')]
    engine, _ = planner(catalog, steps); results = await turns(engine, steps)
    assert leaves(payload(results[1])['filters'])[0]['value']['values'] == [scalar('上海'), scalar('北京')]
    assert leaves(payload(results[2])['filters'])[0]['value'] == scalar('江苏')
    assert leaves(payload(results[4])['filters'])[0]['value'] == scalar('江苏')
    for result in results[5:]:
        assert [(v['field_ref']['display_name'], v['value']) for v in leaves(payload(result)['filters'])] == [('商品', scalar('甲'))]
    assert payload(results[-1])['time']['grain'] == 'QUARTER'
    assert payload(results[-1])['time']['range'] == payload(results[0])['time']['range']
    assert [r.edit_trace[0].operation for r in results[1:6]] == ['ADD', 'REPLACE', 'ADD', 'REMOVE', 'CLEAR']
    assert all(r.edit_trace[0].lowered_operation == 'REPLACE' for r in results[1:6])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,value', [('ADD', scalar('江苏')), ('REPLACE', scalar('江苏')),
    ('REMOVE', None), ('CLEAR', None)])
async def test_filter_operation_contrast_retains_boolean_branch_and_siblings(catalog, operation, value):
    steps = [initial(nested=True), filter_step('修改上海条件', operation, value)]
    engine, _ = planner(catalog, steps); before, after = await turns(engine, steps)
    original = payload(before)['filters']; changed = payload(after)['filters']
    assert original['operator'] == changed['operator'] == 'AND'
    assert original['children'][1] == changed['children'][1]
    assert changed['children'][0]['operator'] == 'OR'
    assert changed['children'][0]['children'][-1] == original['children'][0]['children'][-1]
    assert changed['children'][0]['children'][-1]['operator'] == 'NOT'
    assert len(changed['children'][0]['children']) == (1 if operation in {'REMOVE', 'CLEAR'} else 2)
    if operation == 'ADD': assert len(changed['children'][0]['children'][0]['value']['values']) == 2
    if operation == 'REPLACE': assert changed['children'][0]['children'][0]['value'] == scalar('江苏')


@pytest.mark.asyncio
async def test_add_new_condition_conjoins_whole_expression(catalog):
    text = '商品限定乙'
    step = text, parse(text, [(text, 'FILTER_FIELD', 'filter_expression', 'ADD')], follow=True), lambda c: dict(
        payload_type='INHERIT', filter_edits=[dict(operation='ADD', evidence_mention_ids=['m0'], value=predicate(c, '商品', '乙'))])
    steps = [initial(nested=True), step]; engine, _ = planner(catalog, steps); before, after = await turns(engine, steps)
    assert payload(after)['filters']['children'][0] == payload(before)['filters']
    assert payload(after)['filters']['children'][1]['value'] == scalar('乙')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['REMOVE', 'CLEAR'])
async def test_remove_entire_tree_forms_barrier_and_does_not_revive(catalog, operation):
    def root(c, d):
        d['filter_edits'][0]['target_handle'] = next(t['target_handle'] for t in c['tasks'][0]['filter_targets'] if t['path'] == [])
        return d
    steps = [initial(), filter_step('取消全部筛选条件', operation, transform=root), metric_step('再加销售数量', '销售数量', 'ADD', True)]
    engine, _ = planner(catalog, steps); results = await turns(engine, steps)
    for r in results[1:]:
        assert payload(r)['filters'] is None
        assert 'filter_expression' in next(iter(r.next_state.payload['tasks'].values()))['clear_barriers']


@pytest.mark.asyncio
async def test_remove_last_membership_value_deletes_only_that_predicate(catalog):
    steps = [initial(), filter_step('不要上海', 'REMOVE', scalar('上海'))]
    engine, _ = planner(catalog, steps); result = (await turns(engine, steps))[-1]
    assert len(leaves(payload(result)['filters'])) == 1
    assert leaves(payload(result)['filters'])[0]['field_ref']['display_name'] == '商品'


@pytest.mark.asyncio
async def test_partial_dates_grain_clear_and_restore_are_independent(catalog):
    dates = dict(start='2024-01-01T00:00:00+08:00', end_exclusive='2025-01-01T00:00:00+08:00')
    steps = [initial(time=True), temporal_step('改为2024年', 'RANGE', 'REPLACE', dates),
        temporal_step('按季度', 'GRAIN', 'REPLACE', 'QUARTER'), temporal_step('不限时间', 'RANGE', 'CLEAR'),
        metric_step('再加销售数量', '销售数量', 'ADD', True), temporal_step('按年', 'GRAIN', 'REPLACE', 'YEAR'),
        temporal_step('改为2024年', 'RANGE', 'SET', dates)]
    engine, _ = planner(catalog, steps); results = await turns(engine, steps)
    assert payload(results[1])['time']['grain'] == 'MONTH'
    assert m.TimeRange.model_validate(payload(results[2])['time']['range']) == m.TimeRange.model_validate(dates)
    for r in results[3:6]:
        assert payload(r)['time']['range'] is None
        assert payload(r)['time']['source'] == 'USER_EXPLICIT_UNBOUNDED'
        assert payload(r)['filters'] == payload(results[0])['filters']
    assert payload(results[3])['time']['grain'] == 'QUARTER'
    assert payload(results[-1])['time']['grain'] == 'YEAR'
    assert m.TimeRange.model_validate(payload(results[-1])['time']['range']) == m.TimeRange.model_validate(dates)
    assert payload(results[-1])['time']['source'] == 'USER_EXPLICIT'
    assert len({payload(r)['time']['anchor']['canonical_id'] for r in results}) == 1


@pytest.mark.asyncio
async def test_clear_grain_preserves_range_and_uses_scalar_shape(catalog):
    steps = [initial(time=True), temporal_step('不按月份分组', 'GRAIN', 'CLEAR', kind='SCALAR_AGGREGATE')]
    engine, _ = planner(catalog, steps); before, after = await turns(engine, steps)
    assert payload(after)['time']['range'] == payload(before)['time']['range']
    assert payload(after)['time']['grain'] == 'NONE'


@pytest.mark.asyncio
async def test_time_anchor_uses_current_binding_and_retains_period(catalog):
    text = '改按订单日期'
    step = text, parse(text, [(text, 'TIME_FIELD', 'time_spec', 'REPLACE')], follow=True), lambda c: dict(
        payload_type='INHERIT', temporal_edits=[dict(component='ANCHOR', operation='REPLACE',
            evidence_mention_ids=['m0'], value=binding(c, '订单日期', 'TIME_FIELD', kind='ATTRIBUTE'))])
    steps = [initial(time=True), step]; engine, _ = planner(catalog, steps); before, after = await turns(engine, steps)
    assert payload(after)['time']['anchor']['catalog_type'] == 'ATTRIBUTE'
    assert payload(after)['time']['range'] == payload(before)['time']['range']
    assert payload(after)['time']['grain'] == 'MONTH'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [
    ('foreign', 'V2_FILTER_TARGET_NOT_CURRENT_TASK'), ('new_task', 'V2_FILTER_TARGET_NOT_CURRENT_TASK'),
    ('operation', 'V2_SLOT_OPERATION_CONFLICT'), ('evidence', 'V2_EDIT_EVIDENCE_NOT_CURRENT'),
    ('missing', 'V2_FILTER_REMOVE_VALUE_NOT_PRESENT'), ('overlap', 'V2_FILTER_OVERLAPPING_EDITS'),
    ('root_conflict', 'V2_STRUCTURED_EDIT_CONFLICT'), ('authority', 'V2_MODEL_AUTHORITY_FIELD_FORBIDDEN'),
    ('clear_value', 'V2_FILTER_VALUE_EDIT_INVALID'), ('type', 'V2_CONTRACT_VALIDATION_FAILURE')])
async def test_invalid_filter_edits_fail_before_plan_or_state(catalog, fault, code):
    def corrupt(c, d):
        e = d['filter_edits'][0]
        if fault == 'foreign': e['target_handle'] = 'filter:unoffered'
        if fault == 'operation': e['operation'] = 'REPLACE'
        if fault == 'evidence': e['evidence_mention_ids'] = ['prior-turn']
        if fault == 'missing': e['operation'] = 'REMOVE'; e['value'] = scalar('不存在')
        if fault == 'overlap': d['filter_edits'].append(deepcopy(e))
        if fault == 'root_conflict': d['edits'] = [edit('filter_expression', None, 'CLEAR')]
        if fault == 'authority': e['value'] = dict(value_type='ENTITY_REF', ref=dict(canonical_id='invented'))
        if fault == 'clear_value': e['operation'] = 'CLEAR'
        if fault == 'type': e['value'] = dict(value_type='NUMBER', value=3)
        return d
    op = 'REMOVE' if fault == 'missing' else 'CLEAR' if fault == 'clear_value' else 'ADD'
    steps = [initial(), filter_step('修改地区', op, scalar('江苏'), follow=fault != 'new_task', transform=corrupt)]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match=code): await turns(engine, steps)


@pytest.mark.asyncio
async def test_stale_filter_handle_rejected_after_task_version_changes(catalog):
    cached = {}
    def save(c, d): cached['handle'] = d['filter_edits'][0]['target_handle']; return d
    def replay(c, d): d['filter_edits'][0]['target_handle'] = cached['handle']; return d
    steps = [initial(), filter_step('再加北京', 'ADD', scalar('北京'), transform=save),
        filter_step('换江苏', 'REPLACE', scalar('江苏'), transform=replay)]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_FILTER_TARGET_NOT_CURRENT_TASK'): await turns(engine, steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('component,operation,value,code', [
    ('ANCHOR', 'CLEAR', None, 'V2_TEMPORAL_CLEAR_INVALID'),
    ('RANGE', 'CLEAR', {}, 'V2_TEMPORAL_CLEAR_INVALID'),
    ('GRAIN', 'REPLACE', 'DECADE', 'V2_CONTRACT_VALIDATION_FAILURE'),
    ('RANGE', 'REPLACE', dict(start='2025-01-01', end_exclusive='2026-01-01'), 'V2_CONTRACT_VALIDATION_FAILURE'),
    ('RANGE', 'SET', None, 'V2_CONTRACT_VALIDATION_FAILURE')])
async def test_invalid_temporal_edits_fail_closed(catalog, component, operation, value, code):
    steps = [initial(time=True), temporal_step('修改时间', component, operation, value)]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match=code): await turns(engine, steps)


@pytest.mark.asyncio
async def test_unbounded_time_cannot_be_reintroduced_into_legacy_plan(catalog):
    steps = [initial(time=True), temporal_step('不限时间', 'RANGE', 'CLEAR')]
    engine, _ = planner(catalog, steps); result = (await turns(engine, steps))[-1]
    typed = m.TimeSeriesPayload.model_validate(payload(result))
    from tools.phase25_1.fixtures import permission, snapshot
    with pytest.raises(ValueError, match='unbounded time requires scoped 0.2.2'):
        validate_bound_ref_scope_and_permission(typed, snapshot(), permission(), ())
    with pytest.raises(ValueError, match='unbounded time requires scoped 0.2.2'):
        m.require_bounded_legacy_time(typed)
    forged = typed.time.model_dump(); forged['source'] = 'USER_EXPLICIT'
    with pytest.raises(ValueError, match='unbounded time requires explicit'):
        m.TimeSpec.model_validate(forged)


@pytest.mark.asyncio
async def test_structured_state_context_has_labels_but_no_authority_identifiers(catalog):
    steps = [initial(time=True), filter_step('换江苏', 'REPLACE', scalar('江苏'))]
    engine, transport = planner(catalog, steps); await turns(engine, steps)
    import json
    context = json.loads(transport.calls[-1]['messages'][1]['content'])
    history = json.dumps(context['tasks'])
    assert 'filter_targets' in history
    assert m.TimeRange.model_validate(context['tasks'][0]['time']['range']).start.astimezone(NOW.tzinfo).year == 2025
    assert all(key not in history for key in ['canonical_id', 'semantic_model_id', 'business_domain_ids', 'catalog_version'])


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_task', [False, True])
async def test_historical_edits_use_selected_task_subtrees_only(catalog, wrong_task):
    text = '回到销售额问题，地区换江苏'
    def draft(c):
        return dict(payload_type='INHERIT', historical_task_handle=c['tasks'][0]['task_handle'],
            filter_edits=[dict(operation='REPLACE', evidence_mention_ids=['m0'], value=scalar('江苏'),
                target_handle=target(c, task_index=1 if wrong_task else 0))])
    step = text, parse(text, [('江苏', 'FILTER_FIELD', 'filter_expression', 'REPLACE')], history=True), draft
    steps = [initial(), initial(), step]; engine, _ = planner(catalog, steps)
    if wrong_task:
        with pytest.raises(ValueError, match='V2_FILTER_TARGET_NOT_CURRENT_TASK'): await turns(engine, steps)
    else:
        first, second, last = await turns(engine, steps)
        assert last.plan['logical_plan']['task_id'] == first.plan['logical_plan']['task_id']
        assert last.plan['logical_plan']['task_id'] != second.plan['logical_plan']['task_id']
        assert leaves(payload(last)['filters'])[0]['value'] == scalar('江苏')


@pytest.mark.asyncio
async def test_disjoint_removes_apply_against_original_paths_without_index_shift(catalog):
    def both(c, d):
        d['filter_edits'].append(dict(operation='REMOVE', evidence_mention_ids=['m0'], target_handle=target(c, '商品')))
        return d
    steps = [initial(), filter_step('删除地区和商品条件', 'REMOVE', transform=both)]
    engine, _ = planner(catalog, steps); results = await turns(engine, steps)
    assert payload(results[-1])['filters'] is None
    assert len(results[-1].edit_trace) == 2


@pytest.mark.asyncio
async def test_ancestor_and_child_edits_are_not_ordered_programs(catalog):
    def overlap(c, d):
        d['filter_edits'].append(dict(operation='CLEAR', evidence_mention_ids=['m0'],
            target_handle=next(t['target_handle'] for t in c['tasks'][0]['filter_targets'] if not t['path'])))
        return d
    steps = [initial(), filter_step('清除地区条件', 'CLEAR', transform=overlap)]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_FILTER_OVERLAPPING_EDITS'): await turns(engine, steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('slot', ['time_spec', 'filter_expression'])
async def test_whole_slot_cannot_bypass_local_edit_contract(catalog, slot):
    first = initial(time=True)
    def whole(c):
        d = first[2](c)
        return dict(payload_type='INHERIT', edits=[e for e in d['edits'] if e['slot_path'] == slot])
    parsed = deepcopy(first[1]); parsed['reference_signals'] = ['ELLIPSIS']
    parsed['operation_markers'] = [x for x in parsed['operation_markers'] if x['slot_name'] == slot]
    parsed['explicit_slot_mentions'] = {k: v for k, v in parsed['explicit_slot_mentions'].items() if k == slot}
    steps = [first, (first[0], parsed, whole)]; engine, _ = planner(catalog, steps)
    code = 'V2_TEMPORAL_COMPONENT_EDIT_REQUIRED' if slot == 'time_spec' else 'V2_FILTER_SUBTREE_EDIT_REQUIRED'
    with pytest.raises(ValueError, match=code): await turns(engine, steps)


@pytest.mark.asyncio
async def test_granular_change_invalidates_old_dataset(catalog):
    from test_v2_raw_turn_recognition import envelope_with_payload
    first = initial(); second = filter_step('改为江苏', 'REPLACE', scalar('江苏'))
    engine, _ = planner(catalog, [first, second])
    before = await engine.run(request(question=first[0], message_id='turn0'), IDENTITY)
    raw = deepcopy(before.next_state.model_dump(mode='json')['payload']); task = next(iter(raw['tasks'].values()))
    raw['datasets']['dataset1'] = dict(dataset_id='dataset1', task_id=task['task_id'], task_version=1, status='VALID')
    after = await engine.run(request(question=second[0], message_id='turn1'), IDENTITY,
        state=envelope_with_payload(before.next_state, raw), plans=[before.plan_state])
    assert after.next_state.payload['datasets']['dataset1']['status'] == 'INVALIDATED'
    assert after.next_state.payload['datasets']['dataset1']['invalidation_reason'] == 'SEMANTIC_CHANGE'


@pytest.mark.asyncio
async def test_time_edit_does_not_get_base_from_previous_new_task(catalog):
    step = temporal_step('按季度', 'GRAIN', 'REPLACE', 'QUARTER')
    step[1]['reference_signals'] = []
    steps = [initial(time=True), step]; engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_TEMPORAL_BASE_REQUIRED'): await turns(engine, steps)


@pytest.mark.asyncio
async def test_time_comparison_dependency_is_not_silently_recomputed(catalog):
    first = initial(time=True)
    def comparison(c):
        d = first[2](c)
        d['edits'][-1]['value']['comparison'] = dict(comparison_type='YOY',
            comparison_range=dict(start='2024-01-01T00:00:00+08:00', end_exclusive='2025-01-01T00:00:00+08:00'))
        return d
    steps = [(first[0], first[1], comparison), temporal_step('不限时间', 'RANGE', 'CLEAR')]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_TEMPORAL_COMPARISON_DEPENDENCY'): await turns(engine, steps)


@pytest.mark.asyncio
async def test_membership_add_does_not_reinterpret_negative_predicate(catalog):
    first = initial()
    def negative(c):
        d = first[2](c); d['edits'][-1]['value']['children'][0]['operator'] = 'NE'; return d
    steps = [(first[0], first[1], negative), filter_step('再加北京', 'ADD', scalar('北京'))]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_FILTER_MEMBERSHIP_OPERATOR_UNSUPPORTED'): await turns(engine, steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('follow', [False, True])
async def test_scalar_plan_cannot_silently_discard_explicit_time_grouping(catalog, follow):
    first = initial(time=True)
    if follow:
        steps = [first, temporal_step('不限时间', 'RANGE', 'CLEAR', kind='SCALAR_AGGREGATE')]
    else:
        def scalar_shape(c):
            d = first[2](c); d['payload_type'] = 'SCALAR_AGGREGATE'; return d
        steps = [(first[0], first[1], scalar_shape)]
    engine, _ = planner(catalog, steps)
    with pytest.raises(ValueError, match='V2_TIME_GRAIN_SCALAR_CONFLICT'): await turns(engine, steps)
