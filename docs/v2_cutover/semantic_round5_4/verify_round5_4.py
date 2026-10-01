"""Read-only public evidence/hash verification. No tests, models or network."""
from pathlib import Path
from hashlib import sha256
import json

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
read=lambda p:json.loads(p.read_text(encoding='utf8'))


def main():
    manifest=read(HERE/'round5_4_manifest.json')
    for path,expected in manifest['source_hashes'].items():
        assert sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected,path
    test=read(HERE/'test_delta.json');live=read(HERE/'live_coverage.json')
    assert test['final_unique_passed']==test['baseline_selected_passed']+test['new_tests']==604
    assert test['final_failed']==test['collection_errors']==0 and not test['old_pass_new_fail']
    assert sum(live['counts'].values())==live['declared_cases']==11
    assert live['declared_turns']==25 and live['actual_new_valid_turns']==4
    assert live['counts']=={'PASS':2,'FAIL':1,'NOT_RUN':8}
    assert live['valid_model_calls']+live['invalid_configuration_requests']==manifest['model_calls']==12
    assert manifest['gates']['READY_FOR_V2_READ_ONLY_E2E_SMOKE']=='NO'
    assert not any(manifest[k] for k in ('context_changed','grounding_architecture_changed','reducer_changed',
        'validator_relaxed','blind_accessed','business_sql_executed','production_writes','catalog_writes',
        'index_rebuild','V1_changed','port8088_changed'))
    print('ROUND54_PUBLIC_EVIDENCE_VERIFIED; STAGE_PARTIAL; SMOKE_NO')


if __name__=='__main__':main()
