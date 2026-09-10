"""Versioned declared-axis checks; never change old recordings or runtime outputs.

MODIFY is a task relation. CLEAR is a separately proven slot operation. This
validator accepts the former only with evidence of the latter and its barrier.
It does not certify Whole Plan correctness, SQL execution or geographic grain.
"""
VERSION = 'healthy-slice-relation-operation-v2'


def task_semantics(artifact, target):
    task = artifact['payload']['tasks'][target]
    semantic = next(v['semantics'] for v in task['versions'] if v['version'] == task['active_version'])
    return task, semantic


def check_clear_transition(current, following):
    assert not current['error'] and not following['error']
    result = current['result']; later = following['result']
    resolution = result['resolution']; target = resolution['target_task_id']
    assert resolution['dialogue_act'] in {'MODIFY', 'CLEAR', 'REMOVE'}
    assert resolution['referential_completeness']['relation'] == 'CURRENT_TASK'
    patch = resolution['task_patch']
    assert not patch['reset']
    assert all(not patch[k] for k in ('sets', 'adds', 'removes', 'replacements', 'inherit_requests'))
    assert len(patch['clears']) == 1
    clear = patch['clears'][0]
    assert (clear['slot_path'], clear['operation'], clear['source'], clear['presence']) == (
        'filter_expression', 'CLEAR', 'CURRENT_EXPLICIT', 'EXPLICITLY_CLEARED')
    assert clear['evidence_mention_ids'] and clear['new_value'] is None
    old_task, old = task_semantics(current['before']['state'], target)
    new_task, new = task_semantics(result['next_state'], target)
    assert patch['base_task_version'] == clear['base_task_version'] == old_task['active_version']
    assert new_task['active_version'] == old_task['active_version'] + 1
    assert old['filter_expression'] is not None and new['filter_expression'] is None
    assert {k: v for k, v in old.items() if k != 'filter_expression'} == {
        k: v for k, v in new.items() if k != 'filter_expression'}
    assert following['before']['state'] == result['next_state']
    for cap, value in ((current, result), (following, later)):
        assert cap['state_unchanged'] is True
        assert value['resolution']['target_task_id'] == target
        plan = value['plan']['logical_plan']
        assert plan['task_id'] == target and plan == value['plan_state']['payload']
        task, semantic = task_semantics(value['next_state'], target)
        assert 'filter_expression' in task['clear_barriers']
        assert semantic['filter_expression'] is None and plan['payload']['filters'] is None
        assert semantic['metrics'] == new['metrics'] == plan['payload']['measures']
        ir = [e['output'] for e in cap['events'] if e['stage'] == 'SemanticQueryIR' and e['output'] is not None]
        assert ir and ir[-1] == plan['payload']
        for artifact in (value['next_state'], value['plan_state']):
            assert artifact['context'] == current['before']['state']['context']
    return {'status': 'PASS_ON_DECLARED_AXES', 'evaluator_version': VERSION,
            'relation_operation_separate': True, 'whole_plan_gold': False}
