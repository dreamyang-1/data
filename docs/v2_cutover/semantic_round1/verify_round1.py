"""Verify public Round 1 evidence; no private payload, model, network or writes."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tools.cutover.evaluation_contract import digest


def read(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def normalized_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def parent_verify(folder, filename):
    spec = importlib.util.spec_from_file_location(folder, HERE.parent / folder / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify()


def verify():
    harness = parent_verify('evaluation_harness', 'verify_closure.py')
    calibration = parent_verify('pre_semantic_calibration', 'verify_calibration.py')
    assert harness['status'] == calibration['status'] == 'PASS'
    manifest = read('round1_manifest.json')
    baseline = json.loads((HERE.parent / 'evaluation_harness/evaluation_baseline_manifest.json').read_text(encoding='utf-8'))
    for path, expected in manifest['diagnostic_files'].items():
        assert normalized_hash(ROOT / path) == expected, path
    assert digest(manifest['diagnostic_files']) == manifest['diagnostic_code_hash']
    assert manifest['frozen_pins'] == {k: baseline[k] for k in manifest['frozen_pins']}
    assert not any(manifest['changes'].values())
    assert manifest['real_model_calls'] == manifest['production_external_writes'] == manifest['blind_calls'] == 0

    inventory = read('operation_case_inventory.json')['cases']
    expected_ids = {'G81-' + n for n in ['001','003','008','009','010','012','058','059','060','061','066']} | {'PV81-010','PV81-016'}
    assert len(inventory) == 13 and {r['case_id'] for r in inventory} == expected_ids
    assert Counter(r['corpus'] for r in inventory) == {'PUBLIC_DEV': 11, 'DEV_PROMOTED_FROM_PRIVATE': 2}
    assert sum(bool(r['planned_history']) for r in inventory) == 5
    for row in inventory:
        assert row['failed_turn_index'] == 0 and row['turn_relation'] == 'NEW_TASK'
        assert row['target_task'] == 'task:turn-0'
        assert row['before_status'] == row['after_status'] == 'FAIL'
        assert row['risk'] == 'SAFE_REJECT' and row['fail_closed'] and not row['accepted']
        assert row['target_slot_family'] == 'projection_spec'
        assert row['task_patch_operation'] is None and row['reducer_delta'] == row['IR_operation_effect'] == 'NOT_REACHED'
        assert row['state_version_before'] == row['state_version_after'] == row['task_version_before'] == row['task_version_after'] == 0
        assert row['input_state_unchanged'] and not row['task_patch_published']
        assert row['validated_operations_before_reject'] and all(o['source'] == 'CURRENT_EXPLICIT' for o in row['validated_operations_before_reject'])
    roots = read('operation_root_subclusters.json')
    assert roots['distribution'] == dict(Counter(r['first_divergence_stage'] for r in inventory)) == {'SEMANTIC_ROLE':12, 'MENTION_BOUNDARY':1}
    assert roots['operation_operand_target'] == {'OPERATION_EVIDENCE_DROPPED':0,'OPERAND_UNRESOLVED':9,'TARGET_SLOT_UNRESOLVED':4,'TARGET_TASK_UNRESOLVED':0}
    assert roots['risk_distribution'] == {'CORRECT_ACCEPT':0,'SAFE_REJECT':13,'WRONG_CLARIFICATION':0,'UNSAFE_ACCEPT':0,'WRONG_SILENT_ACCEPT':0}
    assert roots['generic_operation_contract_defects_proven'] == 0 and roots['NO_GENERIC_OPERATION_DEFECT_PROVEN']
    assert [r['case_count'] for r in roots['subclusters']] == [4,8,1]
    assert len({cid for r in roots['subclusters'] for cid in r['case_ids']}) == 13
    oracle = read('oracle_receipts.json')
    assert not oracle['semantic_scores_changed'] and len(oracle['results']) == 13
    assert {r['case_id'] for r in oracle['results']} == expected_ids
    outcomes = Counter()
    capture_hashes = {}
    for corpus in ['public_dev', 'private_validation']:
        source = HERE.parent / 'evaluation_harness/final' / corpus / 'capture_manifest.json'
        capture_hashes.update({(r['case_id'],r['turn_index']):r['hash'] for r in json.loads(source.read_text(encoding='utf-8'))})
    for case in oracle['results']:
        assert case['instrumentation_parity'] == 'PASS'
        assert case['source_capture_hash'] == capture_hashes[(case['case_id'],0)]
        runs = case['runs']
        assert [r['variant'] for r in runs] == ['BASELINE_OBSERVED','MARKER_ONLY','DECLARED_SLOT_PROPOSITION']
        assert len({r['candidate_artifact_hash'] for r in runs}) == len({r['target_task'] for r in runs}) == 1
        assert runs[0]['receipt']['failure']['reason'] == 'V2_EXPLICIT_OPERATION_DROPPED'
        assert runs[1]['receipt']['failure']['reason'] == 'V2_EXPLICIT_SLOT_DROPPED'
        assert not runs[0]['reducer_called'] and len(runs[0]['rejecting_patch']) == 1
        reject = runs[0]['rejecting_patch'][0]
        assert {m[0] for m in reject['unconsumed_markers']} == {'projection_spec'}
        assert reject['operations_created_before_reject']
        for i, run in enumerate(runs):
            assert not run['Gold_PASS'] and not run['Whole_Plan_PASS']
            assert not any(run['external_call_attempts'].values())
            receipt = run['receipt']
            assert receipt['real_model_calls'] == receipt['source_SQL_executions'] == receipt['production_writes'] == 0
            assert [s['output_changed'] for s in receipt['model_stages']] == [i > 0, False]
        last = runs[-1]
        outcomes[last['receipt']['failure']['reason'] if last['receipt']['failure'] else last['status']] += 1
    assert outcomes == {'ACCEPTED_DIAGNOSTIC_ONLY':3,'CATALOG_RELATIONSHIP_REQUIRED':6,'V2_QUERY_SHAPE_CONFLICT':4}
    defaults = read('catalog_default_display_evidence.json')
    assert len(defaults['entities']) == 6 and not any(defaults['external_call_attempts'].values())
    assert all(r['mode'] == 'SEMANTIC_DEFAULT' and r['governed_attribute_codes'] for r in defaults['entities'])

    promotion = read('split_promotion_manifest.json')
    assert {r['case_id'] for r in promotion['promotions']} == {'PV81-010','PV81-016'}
    assert promotion['PRIVATE_VALIDATION_ORIGINAL'] == 24 and promotion['PRIVATE_VALIDATION_REMAINING'] == 22
    assert promotion['BLIND_HOLDOUT'] == 'SEALED_NOT_VIEWED_OR_RUN' and not promotion['blind_calls']
    private = read('private_remaining_evaluation.json')
    assert private['status_counts'] == {'PASS':0,'FAIL':22,'NOT_RUN':0,'BLOCKED':0}
    assert len(private['remaining_case_ids']) == 22 and not set(private['remaining_case_ids']) & {'PV81-010','PV81-016'}
    assert len(private['receipts']) == 35
    full = read('full_plan_observable_delta.json')
    previous_full = json.loads((HERE.parent / 'pre_semantic_calibration/full_plan_evaluation.json').read_text(encoding='utf-8'))
    assert full['scores'] == previous_full['results']
    assert full['FULL_PLAN_GOLD_COUNT'] == 16 and full['before'] == full['after'] == {'BLOCKED':16}
    assert full['Whole_Plan_PASS'] == full['new_pass'] == full['new_fail'] == 0
    assert len(full['scores']) == 16 and len(full['receipts']) == 22
    for row in full['scores']:
        assert row['axes']['expected_dry_plan_outcome'] == 'NOT_OBSERVED'
        assert all(v == 'PASS' for k,v in row['axes'].items() if k != 'expected_dry_plan_outcome')
        assert not row['whole_plan_runtime_success']
    for receipt in private['receipts'] + full['receipts']:
        assert receipt['strict_replay'] == 'PASS' and receipt['model_contexts_identical']
        assert receipt['state_and_plan_identical'] or receipt['same_safe_reject']
        assert receipt['model_calls'] == receipt['SQL_executions'] == receipt['production_writes'] == 0

    tests = read('test_delta.json')
    agent = tests['Agent']
    assert agent['baseline'] == {'passed':3160,'failed':27} and agent['final'] == {'passed':3183,'failed':27}
    assert len(agent['new_nodes']) == 23 and not any(agent[k] for k in ['old_pass_to_new_fail','old_fail_to_new_pass','missing_old_nodes','collection_errors'])
    assert len(tests['failed_nodeids']) == 27 and tests['Critical_160']['counts'] == {'passed':160}
    assert len(tests['Critical_160']['node_outcomes']) == 160 and set(tests['Critical_160']['node_outcomes'].values()) == {'passed'}
    assert tests['clarification_coverage'] == {'public_clarification_responses':89,'with_reason_trace':89}
    safety = read('operation_safety_matrix.json')
    assert safety['new_controlled_native_cases'] == sum(r['case_count'] for r in safety['rows']) == 21
    assert all(r['status'] == 'PASS' for r in safety['rows'])
    assert safety['live_model_operation_safety_gate'] == 'INCOMPLETE_UNCHANGED'
    negative = read('operation_negative_transfer.json')
    assert {r['operation']:r['before_failed'] for r in negative['rows']} == {'ADD':6,'REPLACE':0,'REMOVE':0,'CLEAR':0,'SET':7}
    assert all(r['before_failed'] == r['after_failed'] == r['reclassified'] and r['new_pass'] == r['new_fail'] == r['blocked'] == 0 for r in negative['rows'])
    excluded = {'validation_receipt.json','change_manifest.json','git_commit_manifest.json','rollback_manifest.json','workspace_git_hash_verification.json'}
    hashes = {p.name:normalized_hash(p) for p in HERE.iterdir() if p.is_file() and p.name not in excluded}
    return {'status':'PASS','stage_status':'SEMANTIC_ROOT_CLOSURE_ROUND_1_COMPLETE',
        'conclusion':'NO_GENERIC_OPERATION_DEFECT_PROVEN','investigated_cases':13,
        'evidence_files':len(hashes),'evidence_hash':digest(hashes),
        'parent_harness_status':harness['status'],'parent_calibration_status':calibration['status'],
        'production_changes':0,'model_calls':0,'private_or_blind_payload_reads':0,'production_writes':0}


if __name__ == '__main__':
    print(json.dumps(verify()))
