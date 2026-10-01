"""Observe invariant violations in accepted native plans and state artifacts."""
from tools.cutover.evaluation_contract import digest

ACCEPTANCE_AXES=frozenset({'target_task','bindings',
    'canonical_metrics','canonical_dimensions','metric_surfaces','dimension_surfaces',
    'entity','region_values','filters','time','time_relation','time_grain','task_patch',
    'task_state','query_shape','semantic_query_ir','dry_plan','ranking','dataset_truncation',
    'pending_action','metric_clarification_required','clarification_decision'})


def _refs(value):
    if isinstance(value,dict):
        if {'canonical_id','catalog_type','semantic_role','semantic_model_id','catalog_version','business_domain_ids'}<=value.keys():
            return [value]
        return [r for v in value.values() for r in _refs(v)]
    if isinstance(value,list):return [r for v in value for r in _refs(v)]
    return []


def _read(value,path):
    for part in path.split('.'):
        if not isinstance(value,dict):return None
        value=value.get(part)
    return value


def state_safety(result,before):
    if not result.get('plan'):return {}
    plan=result['plan']['logical_plan'];scope=plan['permission_requirement']['authorized_scope']
    bound=_refs(plan['payload']);proofs=_refs(plan['permission_proofs'])
    proof_ids={digest(r) for r in proofs}
    # BoundSemanticRef uses Identifier strings; AuthorizedSemanticScope uses
    # strict positive integers. This is the runtime's documented representation
    # conversion, not permissive JSON coercion in the generic evaluator.
    expansion=any(r['semantic_model_id']!=str(scope['semantic_model_id']) or
        (scope['scope_mode']=='EXPLICIT_DOMAINS' and
         (not r['business_domain_ids'] or not set(r['business_domain_ids'])<={str(d) for d in scope['business_domain_ids']})) for r in bound)
    out={'scope_expansion':expansion,'catalog_id_fabrication':any(digest(r) not in proof_ids for r in bound)}
    task=result['next_state']['payload']['tasks'][plan['task_id']]
    semantic=next(v['semantics'] for v in task['versions'] if v['version']==task['active_version'])
    if task['clear_barriers']:
        from app.semantic_v2.models import TaskSemanticState
        defaults=TaskSemanticState().model_dump(mode='json')
        out['clear_resurrection']=any(digest(_read(semantic,path))!=digest(_read(defaults,path)) for path in task['clear_barriers'])
    return out


def removal_history_safety(results):
    empty_after_remove={};observed=False;violation=False
    for result in results:
        if not result.get('plan'):continue
        plan=result['plan']['logical_plan'];task_id=plan['task_id'];patch=result['resolution']['task_patch']
        task=result['next_state']['payload']['tasks'][task_id]
        semantic=next(v['semantics'] for v in task['versions'] if v['version']==task['active_version'])
        explicit={v['slot_path'] for phase in ('sets','adds','replacements') for v in patch.get(phase,[])}
        for (owner,path),_ in list(empty_after_remove.items()):
            if owner!=task_id:continue
            if path in explicit or patch.get('reset'):
                empty_after_remove.pop((owner,path));continue
            observed=True
            if semantic.get(path):violation=True
        for removal in patch.get('removes',[]):
            path=removal['slot_path']
            if path in {'metrics','dimensions'} and semantic.get(path)==[]:
                empty_after_remove[(task_id,path)]=True
    return {'remove_last_resurrection':violation} if observed else {}
