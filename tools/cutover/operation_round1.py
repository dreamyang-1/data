"""Frozen operation-cluster causal diagnosis; never a production repair/evaluator.

Oracle variants alter one recorded parse artifact. Semantic drafts, candidates,
scope and clocks remain frozen. Oracle acceptance is not a Gold or model PASS.
"""
from contextlib import contextmanager
from copy import deepcopy
from inspect import unwrap
from unittest.mock import patch

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_contract import validate_labels
from tools.cutover.harness_observation import RuntimeObserver, json_value
from tools.cutover.harness_replay import deny_external_calls, replay_verified
from tools.cutover.run_raw_transition_benchmark import replay_turn


def parse_intervention(outputs, *, slot, variant):
    if variant not in {'MARKER_ONLY', 'DECLARED_SLOT_PROPOSITION'}:
        raise ValueError('UNREVIEWED_ORACLE_VARIANT')
    updated = deepcopy(outputs)
    stages = [i for i, value in enumerate(updated) if value['stage'] == 'v2_current_turn']
    if len(stages) != 1:
        raise ValueError('SINGLE_PARSE_ARTIFACT_REQUIRED')
    i = stages[0]
    parsed = updated[i]['output']
    selected = [m for m in parsed['operation_markers'] if m['slot_name'] == slot]
    if not selected:
        raise ValueError('ORACLE_TARGET_NOT_DECLARED')
    parsed['operation_markers'] = [m for m in parsed['operation_markers'] if m['slot_name'] != slot]
    if variant == 'DECLARED_SLOT_PROPOSITION':
        parsed['explicit_slot_mentions'].pop(slot, None)
    # No mentions/roles, relation signals, query shape, binding selection or draft
    # may change. The second declaration is the same slot proposition's map view.
    allowed = {'operation_markers', 'explicit_slot_mentions'}
    assert {k: v for k, v in parsed.items() if k not in allowed} == {
        k: v for k, v in outputs[i]['output'].items() if k not in allowed}
    assert all(value == outputs[j] for j, value in enumerate(updated) if j != i)
    return updated


@contextmanager
def rejecting_patch_observer():
    """Read bounded locals from the original rejecting frame; return/rethrow intact."""
    from app.semantic_v2.recognition import RawTurnPlanner
    original = RawTurnPlanner._patch
    original_code = unwrap(original).__code__
    observations = []
    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as exc:
            frame = exc.__traceback__
            while frame:
                if frame.tb_frame.f_code is original_code:
                    local = frame.tb_frame.f_locals
                    markers = local.get('markers', set())
                    used = local.get('used_markers', set())
                    observations.append({
                        'boundary': 'RawTurnPlanner._patch', 'line': frame.tb_lineno,
                        'reason': str(exc), 'markers': sorted(markers), 'used_markers': sorted(used),
                        'unconsumed_markers': sorted(markers - used),
                        'covered_mentions': sorted(local.get('covered_mentions', set())),
                        'operations_created_before_reject': json_value(local.get('operations', [])),
                        'base_task_version': local.get('base'),
                    })
                frame = frame.tb_next
            raise
    with patch.object(RawTurnPlanner, '_patch', staticmethod(observe)):
        yield observations


def validate_case(rows, catalog):
    for case in rows:
        if case.get('split') == 'BLIND_HOLDOUT':
            raise ValueError('BLIND_HOLDOUT_FORBIDDEN')
        validate_labels(case)
        if case['scope'] != catalog['scope'] or case['catalog_ref'] != catalog['artifact_hash']:
            raise ValueError('ORACLE_SCOPE_OR_CATALOG_MISMATCH')


async def diagnose(capture, case, catalog, raw, *, expected_capture_hash,
                   source_observations, source_observations_hash):
    validate_case([case], catalog)
    if digest(capture) != expected_capture_hash:
        raise ValueError('CAPTURE_HASH_MISMATCH')
    frozen = digest(capture)
    kwargs = dict(expected_capture_hash=expected_capture_hash, source_observations=source_observations,
                  source_observations_hash=source_observations_hash)
    # Instrumentation-off baseline first, then an observed baseline; compare the
    # real runtime's result/rejection with the original immutable capture.
    off, off_result = await replay_verified(capture, case, catalog, raw, **kwargs)
    assert off['failure'] and off['failure']['reason'] == capture['outcome']['reason'] == 'V2_EXPLICIT_OPERATION_DROPPED'
    assert off_result is None
    runs = []; private = []
    for variant in ['BASELINE_OBSERVED', 'MARKER_ONLY', 'DECLARED_SLOT_PROPOSITION']:
        oracle = None if variant == 'BASELINE_OBSERVED' else parse_intervention(
            capture['outputs'], slot='projection_spec', variant=variant)
        observer = RuntimeObserver()
        with observer:
            observer.begin_turn(case['case_id'], capture['turn_index'])
            with rejecting_patch_observer() as rejecting, deny_external_calls() as counters:
                receipt, result = await replay_turn(capture, case, catalog, raw, **kwargs,
                    oracle_outputs=oracle, case_validator=validate_case, runtime_entry=True)
        assert digest(capture) == frozen
        assert not any(counters.values())
        if variant == 'BASELINE_OBSERVED':
            assert receipt['failure'] == off['failure'] and result is None
            assert len(rejecting) == 1 and rejecting[0]['unconsumed_markers']
            assert {m[0] for m in rejecting[0]['unconsumed_markers']} == {'projection_spec'}
            assert rejecting[0]['operations_created_before_reject']
        if oracle is not None:
            assert [x['output_changed'] for x in receipt['model_stages']] == [True, False]
        traces = observer.turns[-1]['events']
        candidate = [e['output'] for e in traces if e['stage'] == 'CandidateSet']
        targets = [e['output'] for e in traces if e['stage'] == 'TurnResolutionInput' and e['output']]
        summary = dict(case_id=case['case_id'], turn_index=capture['turn_index'], variant=variant,
            status='ACCEPTED_DIAGNOSTIC_ONLY' if result is not None else 'SAFE_REJECT',
            receipt=receipt, rejecting_patch=rejecting, candidate_artifact_hash=digest(candidate),
            target_task=targets[0]['target_task_id'] if targets else None,
            reducer_called=any(e['stage']=='TaskSemanticState' for e in traces),
            external_call_attempts=counters, Gold_PASS=False, Whole_Plan_PASS=False,
            result_hash=digest(result.model_dump(mode='json')) if result else None)
        runs.append(summary)
        private.append(dict(variant=variant, traces=traces, result=json_value(result), oracle_outputs=oracle))
    assert len({r['candidate_artifact_hash'] for r in runs}) == 1
    assert len({r['target_task'] for r in runs}) == 1
    return {'case_id':case['case_id'],'source_capture_hash':frozen,'instrumentation_parity':'PASS',
            'runs':runs,'production_code_changes':0,'real_model_calls':0}, private
