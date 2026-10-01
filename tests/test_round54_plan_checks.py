from copy import deepcopy

import pytest

from tools.cutover.round54_plan_checks import check_plan


def capture():
    metric = {'canonical_code': 'order_count'}
    time = dict(anchor={'canonical_code': 'created_date'}, grain='NONE',
        range={'start': '2025-01-01T00:00:00+08:00', 'end_exclusive': '2026-01-01T00:00:00+08:00'})
    semantics = dict(metrics=[metric], dimensions=[], filter_expression=None, time_spec=time)
    payload = dict(measures=[metric], group_by=[], filters=None, time=time)
    plan = dict(task_id='task', payload=payload)
    context = {'authorized_scope': dict(semantic_model_id=81, business_domain_ids=[205], database_id=None, knowledge_base_names=[])}
    task = dict(active_version=2, versions=[dict(version=2, semantics=semantics)], clear_barriers=['filter_expression'])
    result = dict(plan={'logical_plan': deepcopy(plan)}, plan_state=dict(payload=deepcopy(plan), context=deepcopy(context)),
        next_state=dict(payload={'tasks': {'task': task}}, context=context),
        resolution=dict(dialogue_act='REMOVE', target_task_id='task'))
    return dict(error=None, result=result, state_unchanged=True, events=[dict(stage='SemanticQueryIR', output=deepcopy(payload))])


def check(item):
    return check_plan(item, metrics=['order_count'], regions=[], year=2025,
        relation=('REMOVE', 'CLEAR'), target='task', clear=True)


def test_complete_subtree_removal_checks_the_native_clear_barrier():
    assert check(capture())['status'] == 'PASS_ON_DECLARED_AXES'


@pytest.mark.parametrize('mutation', ['extra_metric', 'wrong_target', 'wrong_relation', 'lost_barrier',
    'wider_scope', 'wrong_year', 'extra_grouping', 'missing_ir', 'state_mutation'])
def test_injected_wrong_plan_or_missing_observation_cannot_pass(mutation):
    item = capture(); result = item['result']; task = result['next_state']['payload']['tasks']['task']
    semantic = task['versions'][0]['semantics']
    if mutation == 'extra_metric': semantic['metrics'].append({'canonical_code': 'extra'})
    if mutation == 'wrong_target': result['resolution']['target_task_id'] = 'other'
    if mutation == 'wrong_relation': result['resolution']['dialogue_act'] = 'NEW_TASK'
    if mutation == 'lost_barrier': task['clear_barriers'] = []
    if mutation == 'wider_scope': result['next_state']['context']['authorized_scope']['business_domain_ids'] = []
    if mutation == 'wrong_year': semantic['time_spec']['range']['start'] = '2024-01-01T00:00:00+08:00'
    if mutation == 'extra_grouping': semantic['dimensions'].append({'canonical_code': 'unexpected'})
    if mutation == 'missing_ir': item['events'] = []
    if mutation == 'state_mutation': item['state_unchanged'] = False
    with pytest.raises(AssertionError): check(item)
