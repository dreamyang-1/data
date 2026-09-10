"""Upgrade authored mock fixtures to joint output; never imported by production.

Fixture flags are test-author labels, not a classifier. Explicit proposal fixtures
take precedence. No raw user text is classified; Pending choices reuse its exact
option grammar. Frozen real-model captures require separate versioned migration.
"""
from types import SimpleNamespace
from app.semantic_v2.pending_recognition import selected_option


def fixture_proposal(parsed, context):
    supplied=parsed.get('context_proposal')
    if supplied is not None:return supplied(context) if callable(supplied) else supplied
    ctx=context['task_context'];rows=ctx['candidate_tasks'];active=ctx['active_task_id']
    status='ACCEPTED';relation='NEW_TASK';target=None;pending_id=None
    refs=parsed.get('reference_signals',[]);shift=parsed.get('topic_shift_signals',[])
    if not shift and 'HISTORICAL' in refs:
        relation='RETURN_TO_TOPIC';target=next((r for r in reversed(rows) if r['task_id']!=active),None)
    elif not shift and (refs or parsed.get('followup_signals')):
        relation='CONTINUE';target=next((r for r in rows if r['task_id']==active),None)
    else:
        markers=parsed.get('operation_markers',[])
        acts=set(parsed.get('dialogue_act_candidates',[]))
        if not shift and len(acts)==1 and acts<={'REMOVE','CLEAR'} and {m['operation_hint'] for m in markers}==acts:
            relation=next(m['operation_hint'] for m in markers if m['operation_hint'] in {'REMOVE','CLEAR'})
            target=next((r for r in rows if r['task_id']==active),None)
    pending=ctx['pending']
    if pending and not shift and 'NEW_TASK' not in parsed.get('dialogue_act_candidates',[]):
        p=SimpleNamespace(status='ACTIVE',active_blocker_id=pending['active_blocker_id'],blockers=[
            SimpleNamespace(**{**b,'options':[SimpleNamespace(option_id=o['option_id'],display_label=o['label']) for o in b['options']]})
            for b in pending['blockers']])
        if selected_option(p,context['question']):
            relation='ANSWER_CLARIFICATION';pending_id=pending['pending_id']
            target=next(r for r in rows if r['task_id']==pending['task_id'])
        elif relation!='NEW_TASK' or not parsed.get('dialogue_act_candidates'):
            status='UNRESOLVED';relation=None;target=None
    if relation not in (None,'NEW_TASK') and target is None:status='UNRESOLVED';relation=None
    return dict(status=status,relation=relation,target_task_id=target['task_id'] if target else None,
        state_version=ctx['state_version'],task_version=target['task_version'] if target else None,pending_id=pending_id)
