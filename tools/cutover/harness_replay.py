"""Replay policy and hard external-call denial for frozen private artifacts."""
from contextlib import contextmanager,ExitStack
from copy import deepcopy
from unittest.mock import patch
import socket

import httpx
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_contract import canonical

EQUALITY_POLICY={
    'BYTE_EQUAL_REQUIRED':['authorized_scope','catalog_pin','captured_model_inputs','prompt','schema','source_observations'],
    'SEMANTIC_EQUAL_REQUIRED':['TaskSemanticState.metrics','TaskSemanticState.dimensions','LogicalPlan.permission_proofs'],
    'NONDETERMINISTIC_ALLOWED':['diagnostic.trace_id','diagnostic.wall_time_ms'],
    'never_ignored':['task_id','task_version','state_version','as_of','snapshot_id','dataset_truncation','output_projection_order'],
}


def _semantic_result(value):
    out=deepcopy(value)
    # Envelope hashes remain validated by the runtime replay. Normalize only
    # the explicitly set-valued contract fields for semantic comparison.
    if out is None:return None
    if isinstance(out.get('diagnostic'),dict):
        for key in ('trace_id','wall_time_ms'):out['diagnostic'].pop(key,None)
        if not out['diagnostic']:out.pop('diagnostic')
    state=out.get('next_state',{}).get('payload',{})
    for task in state.get('tasks',{}).values():
        for version in task['versions']:
            semantics=version['semantics']
            for key in ('metrics','dimensions'):
                if key in semantics:semantics[key]=canonical(semantics[key],unordered=True)
    plan=(out.get('plan') or {}).get('logical_plan',{})
    if 'permission_proofs' in plan:plan['permission_proofs']=canonical(plan['permission_proofs'],unordered=True)
    # Scope/capture hashes are compared independently; all scoped envelope
    # payload digests must first pass ScopedArtifact validation below.
    for key in ('next_state','plan_state','pending_state'):
        if out.get(key):out[key].pop('payload_digest',None)
    return out


def compare_results(expected,actual):
    from app.semantic_v2.authorized_contract import ScopedArtifact
    for result in (expected,actual):
        if result:
            for key in ('next_state','plan_state','pending_state'):
                if result.get(key):ScopedArtifact.model_validate(result[key])
    return {'byte_equal':digest(expected)==digest(actual),
            'semantic_equal':digest(_semantic_result(expected))==digest(_semantic_result(actual)),
            'policy_hash':digest(EQUALITY_POLICY)}


@contextmanager
def deny_external_calls(*, allowed_mock_transport=None):
    """Enter after the event loop exists. No model escape ContextVar is honored."""
    counters={'model_attempts':0,'database_attempts':0,'network_attempts':0,'redis_attempts':0}
    def denied(kind):
        def fail(*a,**kw):
            counters[kind]+=1
            raise RuntimeError('FROZEN_REPLAY_EXTERNAL_CALL_DENIED')
        return fail
    original_send=httpx.AsyncClient.send
    if allowed_mock_transport is not None and type(allowed_mock_transport) is not httpx.MockTransport:
        raise ValueError('ONLY_EXACT_MOCK_TRANSPORT_CAN_REPLAY')
    async def model_denied(client,*a,**kw):
        if allowed_mock_transport is not None and client._transport is allowed_mock_transport:
            return await original_send(client,*a,**kw)
        return denied('model_attempts')()
    with ExitStack() as stack:
        for name in ('connect','connect_ex'):
            stack.enter_context(patch.object(socket.socket,name,denied('network_attempts')))
        stack.enter_context(patch.object(socket,'create_connection',denied('network_attempts')))
        stack.enter_context(patch.object(httpx.AsyncClient,'send',model_denied))
        import pymysql
        stack.enter_context(patch.object(pymysql,'connect',denied('database_attempts')))
        import redis
        stack.enter_context(patch.object(redis.Redis,'execute_command',denied('redis_attempts')))
        yield counters


async def replay_verified(*args,**kwargs):
    from tools.cutover.run_raw_transition_benchmark import replay_turn
    capture=args[0]
    if 'label_provenance' in args[1]:
        from tools.cutover.harness_contract import validate_labels
        def validate_cases(rows,catalog):
            for row in rows:
                validate_labels(row)
                if row['scope']!=catalog['scope'] or row['catalog_ref']!=catalog['artifact_hash']:
                    raise ValueError('HARNESS_REPLAY_CASE_SCOPE_MISMATCH')
        kwargs['case_validator']=validate_cases
    before=digest(capture)
    # Use the public runtime wrapper as well: its ValidationError mapping is
    # part of the behavior being measured, not an evaluator normalization.
    kwargs['runtime_entry']=True
    with deny_external_calls() as counters:
        receipt,result=await replay_turn(*args,**kwargs)
    assert digest(capture)==before
    comparison=compare_results(capture.get('result'),result.model_dump(mode='json') if result else None)
    return {**receipt,'external_call_attempts':counters,'equality':comparison,
            'production_state_mutations':0,'storage':'FRESH_MEMORY_PUBLICATION_AND_LOCAL_STATE'},result
