"""Verify committed evidence arithmetic and hashes; no network or model calls."""
from pathlib import Path
from hashlib import sha256
import json

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def read(name):return json.loads((HERE/name).read_text(encoding='utf8'))
def main():
    validation=read('validation_receipt.json')
    for path,expected in validation['source_hashes'].items():
        assert sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected,path
    for name,expected in validation['evidence_hashes'].items():
        assert sha256((HERE/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected,name
    test=read('test_delta.json');live=read('live_turn_inventory.json');perf=read('performance_receipt.json')
    assert test['current_unique_passed']==test['baseline_unique_passed']+test['new_test_count']
    assert not test['old_pass_new_fail'] and not test['collection_errors']
    assert all(v=='passed' for v in test['node_results'].values())
    assert live['unique_new_turns']==live['pass_on_declared_axes']+live['fail_safe_reject']
    assert perf['model_calls']==perf['provider_smoke_calls']+perf['semantic_model_calls']<=24
    assert read('safety_and_gates.json')['INTERNAL_DEMO_SEMANTIC_READY']=='NO'
    assert read('safety_and_gates.json')['READY_FOR_INTERNAL_DEMO_E2E_SMOKE']=='NO'
    assert not read('root_cause_receipt.json')['oracle']['strict_candidate_fixed_complete']
    assert read('live_chain_matrix.json')['fresh_primary']['formal_scores_not_replaced']
    print('ROUND55_EVIDENCE_VERIFIED; PARTIAL; INTERNAL_DEMO_NO; E2E_NO')
if __name__=='__main__':main()
