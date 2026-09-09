"""Verify checkpoint evidence without model, database or production-state access."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def read(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def file_hash(path):
    # Repository text is LF; Windows development files may be CRLF.
    # Only source/document file hashes use this policy. Frozen input and
    # canonical JSON digests keep their original authority and hash algorithm.
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def main():
    cases = read('case_inventory.json')
    roots = read('root_cause_inventory.json')
    distribution = read('first_divergence_distribution.json')
    summary = read('checkpoint_summary.json')
    gate = read('semantic_freeze_gate.json')
    evidence = read('evidence_inventory.json')
    growth = read('rule_growth_audit.json')
    assert len(cases) == len({r['case_id'] for r in cases}) == 120
    assert len(roots) == len({r['root_cause_id'] for r in roots}) == 11
    assert Counter(r['status'] for r in cases) == {'PASS':79, 'FAIL':37, 'NOT_RUN':4}
    by_id = {r['root_cause_id']:r for r in roots}
    for case in cases:
        if case['status'] == 'PASS':
            assert case['root_cause_id'] is None and case['first_divergence_stage'] is None
        else:
            root = by_id[case['root_cause_id']]
            assert case['case_id'] in root['case_ids']
            assert case['first_divergence_stage'] == root['first_divergence_stage']
    assert sum(r['case_count'] for r in roots) == sum(r['case_count'] for r in distribution) == 41
    for row in distribution:
        expected = sorted(c['case_id'] for c in cases if c['first_divergence_stage'] == row['stage'])
        assert sorted(row['case_ids']) == expected and row['case_count'] == len(expected)
    assert len(distribution) == len({r['stage'] for r in distribution}) == 17
    with (HERE/'case_inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
        csv_cases = list(csv.DictReader(stream))
    assert len(csv_cases) == 120
    assert {r['case_id']:r['status'] for r in csv_cases} == {
        r['case_id']:r['status'] for r in cases}
    assert sum(r['kind']=='SEMANTIC' for r in roots)==8
    assert sum(r['case_count'] for r in roots if r['kind']=='SEMANTIC')==31
    assert sum(r['case_count'] for r in roots if r['kind']=='EVALUATION')==10
    assert summary['status']=='SEMANTIC_TRIAGE_COMPLETE' and gate['formal_model_benchmark_allowed'] is False
    assert evidence['total_receipt_rows']==94 and evidence['distinct_source_capture_hashes']==77
    assert growth['versions']['current']['keyword_count']==33
    assert growth['versions']['current']['prompt_rule_sentence_count']==92
    assert not growth['versions']['current']['semantic_regex_calls']
    assert gate['file_hash_policy'] == evidence['file_hash_policy'] == 'SHA-256 after CRLF-to-LF normalization; frozen input/canonical JSON hashes unchanged'
    for path, expected in gate['current_artifacts']['source_files'].items():
        assert file_hash(ROOT/path)==expected, path
    for entry in evidence['replay_sources'] + evidence['oracle_and_candidate_sources']:
        assert file_hash(ROOT/entry['path'])==entry['sha256'], entry['path']
    assert all(summary[k]==0 for k in ('production_code_changes','model_calls','source_SQL','production_writes'))
    print(json.dumps({'status':'CHECKPOINT_VERIFIED','cases':120,'primary_divergences':41,
        'root_groups':11,'model_benchmark_gate':'FAIL','network_calls':0,'production_writes':0}))


if __name__ == '__main__':
    main()
