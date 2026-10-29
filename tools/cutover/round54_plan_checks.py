"""Independent declared-axis checks for accepted healthy-slice observations.

No expected values are inferred from model output. This is not Whole Plan Gold.
"""
from datetime import datetime, timedelta, timezone


def check_plan(capture, *, metrics, regions, year, relation, target, clear=False):
    if capture['error'] or not capture['result'] or not capture['result']['plan']:
        raise ValueError('PLAN_NOT_OBSERVED')
    result = capture['result']; plan = result['plan']['logical_plan']; payload = plan['payload']
    task = result['next_state']['payload']['tasks'][plan['task_id']]
    semantic = next(v['semantics'] for v in task['versions'] if v['version'] == task['active_version'])
    assert result['resolution']['dialogue_act'] in (relation if isinstance(relation, tuple) else (relation,))
    assert plan['task_id'] == target == result['resolution']['target_task_id']
    assert result['plan_state']['payload'] == plan
    assert [m['canonical_code'] for m in semantic['metrics']] == metrics
    assert payload['measures'] == semantic['metrics'] and payload['group_by'] == semantic['dimensions'] == []
    assert payload['filters'] == semantic['filter_expression']
    predicate = semantic['filter_expression']
    if not regions:
        assert predicate is None
    else:
        assert predicate['node_type'] == 'PREDICATE' and predicate['operator'] == 'EQ'
        assert predicate['field_ref']['canonical_code'] == 'province_name'
        assert [predicate['value']['ref']['display_name']] == regions
    assert payload['time'] == semantic['time_spec']
    time = semantic['time_spec']; zone = timezone(timedelta(hours=8))
    assert datetime.fromisoformat(time['range']['start']).astimezone(zone) == datetime(year, 1, 1, tzinfo=zone)
    assert datetime.fromisoformat(time['range']['end_exclusive']).astimezone(zone) == datetime(year + 1, 1, 1, tzinfo=zone)
    assert time['anchor']['canonical_code'] == 'created_date' and time['grain'] == 'NONE'
    if clear: assert 'filter_expression' in task['clear_barriers']
    ir = [e['output'] for e in capture['events'] if e['stage'] == 'SemanticQueryIR' and e['output'] is not None]
    assert ir and ir[-1] == payload
    for artifact in (result['next_state'], result['plan_state']):
        scope = artifact['context']['authorized_scope']
        assert scope['semantic_model_id'] == 81 and scope['business_domain_ids'] == [205]
        assert scope['database_id'] is None and scope['knowledge_base_names'] == []
    assert capture['state_unchanged'] is True
    return {'status': 'PASS_ON_DECLARED_AXES', 'whole_plan_gold': False}
