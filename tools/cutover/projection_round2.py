"""Bounded parse-artifact diagnosis. Never a production projection policy."""
from copy import deepcopy

from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_observation import RuntimeObserver
from tools.cutover.harness_replay import deny_external_calls
from tools.cutover.operation_round1 import rejecting_patch_observer, validate_case
from tools.cutover.run_raw_transition_benchmark import replay_turn


def intervene(outputs, *, variant, mention_ids=(), boundary=None):
    if variant not in {'A_CLASSIFICATION','B_OBLIGATION','C_BOUNDARY'}:
        raise ValueError('UNKNOWN_ORACLE')
    result=deepcopy(outputs)
    stages=[i for i,r in enumerate(result) if r['stage']=='v2_current_turn']
    if len(stages)!=1:raise ValueError('SINGLE_PARSE_ARTIFACT_REQUIRED')
    index=stages[0];parsed=result[index]['output'];ids=set(mention_ids)
    if not ids or not ids <= {m['mention_id'] for m in parsed['mentions']}:
        raise ValueError('EXACT_EXISTING_MENTION_REQUIRED')
    if variant=='C_BOUNDARY':
        if len(ids)!=1 or not boundary or set(boundary)!={'surface','normalized_surface','start_char','end_char'}:
            raise ValueError('SINGLE_BOUNDARY_REQUIRED')
        next(m for m in parsed['mentions'] if m['mention_id'] in ids).update(boundary)
    else:
        parsed['operation_markers']=[m for m in parsed['operation_markers']
            if not (m['slot_name']=='projection_spec' and m['mention_id'] in ids)]
        parsed['explicit_slot_mentions']['projection_spec']=[m for m in parsed['explicit_slot_mentions'].get('projection_spec',[]) if m not in ids]
        if variant=='A_CLASSIFICATION':
            kept=[]
            for mention in parsed['mentions']:
                if mention['mention_id'] in ids:
                    mention['candidate_roles']=[r for r in mention['candidate_roles'] if r!='PROJECTION_FIELD']
                if mention['candidate_roles']:kept.append(mention)
            parsed['mentions']=kept
    CurrentTurnSemanticParse.model_validate(parsed)
    assert all(r==outputs[i] for i,r in enumerate(result) if i!=index)
    return result


async def observe_oracle(capture, case, catalog, raw, *, oracle_outputs, source_observations, source_observations_hash):
    frozen=digest(capture);observer=RuntimeObserver()
    with observer:
        observer.begin_turn(case['case_id'],capture['turn_index'])
        with rejecting_patch_observer() as rejecting, deny_external_calls() as counters:
            receipt,result=await replay_turn(capture,case,catalog,raw,expected_capture_hash=frozen,
                source_observations=source_observations,source_observations_hash=source_observations_hash,
                oracle_outputs=oracle_outputs,case_validator=validate_case,runtime_entry=True)
    assert digest(capture)==frozen and not any(counters.values())
    assert [s['output_changed'] for s in receipt['model_stages']]==[True,False]
    traces=observer.turns[-1]['events']
    value={'case_id':case['case_id'],'source_capture_hash':frozen,'receipt':receipt,
        'outcome':receipt['failure']['reason'] if receipt['failure'] else 'ACCEPTED_DIAGNOSTIC_ONLY',
        'rejecting_patch':rejecting,'candidate_artifact_hash':digest([e['output'] for e in traces if e['stage']=='CandidateSet']),
        'external_call_attempts':counters,'formal_gold_pass':False,'whole_plan_pass':False}
    return value,{'oracle_outputs':oracle_outputs,'traces':traces,'result':result.model_dump(mode='json') if result else None}
