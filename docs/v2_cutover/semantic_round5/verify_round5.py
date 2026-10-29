"""Verify frozen public Round 5 receipts; no replay, tests or external calls."""
from collections import Counter
import ast
import hashlib
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


def verify():
    manifest = read('round5_manifest.json')
    assert manifest['stage'] == 'CONTEXT_PROPOSAL_PRODUCTION_WIRING_PARTIAL'
    assert manifest['baseline'] == '899b7b692f7ff06a8660fe99361ba39f8528b1f1'
    assert Counter(manifest['gates'].values()) == {'PASS': 16, 'PARTIAL': 1}
    assert manifest['gates']['PERFORMANCE_BEFORE_AFTER_ACCEPTANCE'] == 'PARTIAL'
    for path, expected in manifest['production_files'].items():
        assert normalized_hash(ROOT / path) == expected, path
    assert len(manifest['production_files']) == 4
    for name in ('prompt_literals_changed', 'added_semantic_regex',
                 'added_business_keyword_special_cases', 'added_confidence_thresholds',
                 'extra_sequential_llm_calls', 'production_writes', 'source_sql_calls',
                 'blind_access', 'new_promotions', 'private_semantic_debug_or_tuning'):
        assert manifest[name] == 0, name
    for name in ('reducer_changed', 'public_api_sse_changed', 'v1_changed', 'ui_changed',
                 'port_8088_changed', 'shadow_canary_benchmark_started',
                 'READY_FOR_USER_APPROVAL', 'next_8088_takeover_preparation'):
        assert manifest[name] is False, name
    assert manifest['context_candidate_cap'] == 4
    assert manifest['real_model_calls'] == 20
    assert manifest['scope'] == {'semantic_model_id': 81, 'business_domain_ids': [205]}
    assert manifest['CONTEXT_FOLLOWUP_READY'] == 'NOT_READY'
    assert manifest['schema_reject_root_cause']['classification'].startswith('C_')
    for node in ast.parse((ROOT / 'app/semantic_v2/recognition.py').read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in manifest['prompt_literal_hashes']:
                assert hashlib.sha256(node.value.value.encode()).hexdigest() == manifest['prompt_literal_hashes'][name]

    validation = read('validation_receipt.json')
    assert validation['status'] == 'PARTIAL'
    assert validation['source_hashes'] == manifest['production_files']
    for path, expected in validation['evidence_hashes'].items():
        assert normalized_hash(HERE / path) == expected, path

    live = read('live_validation_receipt.json')
    assert live['cases'] == live['proposal_schema_valid'] == live['target_or_nonselection_match'] == live['relation_match'] == 12
    assert live['native_plans'] == 10 and live['ambiguous_terminal'] == 1
    assert live['downstream_rejections'] == ['S81-016']
    assert not live['labeled_state_wrong']
    assert live['real_model_calls'] == 20
    assert live['provider_timeout_count'] == live['schema_reject_count'] == 0
    assert len({row['case_id'] for row in live['rows']}) == 12
    replay = read('runtime_replay_receipt.json')
    assert replay['cases'] == replay['passed'] == 12
    assert all(row['request_match'] and row['semantic_outcome_equal'] for row in replay['rows'])
    assert replay['model_calls'] == replay['sql_calls'] == replay['production_writes'] == 0
    targets = read('target_regression_receipt.json')
    assert targets['cases'] == targets['target_preserved'] == 15
    assert not targets['target_regressions'] and not targets['new_model_accuracy']
    assert not targets['strict_original_request_replay']
    for receipt in (replay, targets):
        hashes = {path.replace('\\', '/'): value for path, value in receipt['runtime_source_hashes'].items()}
        assert hashes == manifest['production_files']

    tests = read('test_delta.json')
    assert Counter(tests['node_outcomes'].values()) == tests['counts'] == {'passed': 3266, 'failed': 27}
    assert digest(tests['node_outcomes']) == tests['node_outcomes_hash']
    assert not tests['old_pass_to_new_fail'] and not tests['old_fail_to_new_pass']
    assert not tests['collection_errors']
    assert tests['critical'] == {'passed': 160, 'failed': 0}
    assert tests['context_slice'] == {'passed': 8, 'turns': 18}
    assert tests['new_round5_tests'] == 25

    performance = read('performance_receipt.json')
    assert performance['before_total_v2_seconds']['n'] == 0
    assert performance['before_recognition_seconds']['n'] == 0
    assert performance['after_total_v2_seconds']['n'] == 6
    assert performance['after_recognition_seconds']['n'] == 12
    assert performance['added_sequential_context_calls'] == 0
    assert performance['before_current_production_usage']['input']['mean'] == 16162
    assert performance['after_current_production_usage']['input']['mean'] == 17086
    return dict(evidence_integrity='PASS', stage=manifest['stage'],
                performance_gate='PARTIAL_MISSING_MATCHED_OLD_PRODUCTION_LATENCY',
                tests=tests['counts'], live_model_calls=20, new_external_calls=0,
                private_reads=0, replay_or_tests_executed=0,
                CONTEXT_FOLLOWUP_READY='NOT_READY', READY_FOR_USER_APPROVAL=False)


if __name__ == '__main__':
    print(json.dumps(verify()))
