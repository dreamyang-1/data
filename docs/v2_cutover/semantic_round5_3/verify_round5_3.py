"""Read-only public evidence checks; no tests, models, private inputs or network."""
from pathlib import Path
from hashlib import sha256
from collections import Counter
import json,sys

ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from tools.cutover.evaluation_contract import digest
from tools.cutover.round53_healthy_cases import validate_case

def read(name):return json.loads((HERE/name).read_text(encoding='utf-8'))
def source(name):return ROOT/name if (ROOT/name).exists() else ROOT.parent/name
def norm(path):return sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()

def verify():
    m=read('round5_3_manifest.json')
    assert m['status']=='ROUND_5_3_GROUNDING_CLOSURE_PARTIAL'
    assert m['baseline']=='bf3e8dbb9adb3abe40a4a53f8475f27799cf3da9'
    for key in ('prompt_changes','context_changes','reducer_changes','v1_changes','ui_changes','api_sse_changes',
                'port8088_changes','model_changes','confidence_changes','production_regex_added','business_keyword_rules_added',
                'catalog_writes','index_rebuilds','business_sql_executed','production_writes','blind_access'):
        assert m[key]==0,key
    assert m['live_model_calls']==11 and m['invalid_live_input_calls']==3
    assert m['CONTEXT_FOLLOWUP_READY']=='NOT_READY' and not m['READY_FOR_V2_READ_ONLY_E2E_SMOKE']
    for path,expected in m['production_hashes'].items():assert norm(source(path))==expected,path
    t=read('test_delta.json')
    for service,total in [('Agent',563),('Oagnet',261)]:
        assert Counter(t[service]['results'].values())=={'passed':total}
        assert not t[service]['collection_errors'] and not t[service]['old_pass_to_new_fail']
    assert t['Agent']['new_tests']==37 and t['Oagnet']['new_tests']==13
    assert t['context_slice']=={'cases':8,'turns':18,'passed':8}
    assert t['critical']['passed']==160 and t['round3_target']['passed']==15
    live=read('live_coverage.json')
    assert sum(a['calls'] for a in live['attempts'])==11
    assert sum(a['input_valid'] for a in live['attempts'])==3
    assert sum(not a['input_valid'] for a in live['attempts'])==3
    assert all(a['first_divergence_stage']=='FIXTURE_GAP' for a in live['attempts'] if not a['input_valid'])
    assert Counter(c['status'] for c in live['original_cases'])=={'FAIL':2,'NOT_RUN':18}
    assert live['healthy_valid_live_turns']==0 and live['healthy_not_run']==10
    healthy=read('catalog_healthy_slice.json')
    assert healthy['definition_hash']==digest({k:v for k,v in healthy.items() if k!='definition_hash'})
    for case in healthy['cases']:validate_case(case)
    roots=read('root_closure.json')
    assert roots['grounding_oracle']['causal_root_confirmed']
    assert roots['grounding_oracle']['after_same_raw_capture_enabled_plan']
    assert roots['grounding_oracle']['same_raw_capture_disable_only_targeted_consumer']['reason']=='V2_SOURCE_VALUE_PROBE_CARDINALITY_EXCEEDED'
    assert read('catalog_time_contract.json')['classification']=='CATALOG_GAP'
    assert read('source_value_current_path.json')['targeted_bound']==8
    validation=read('validation_receipt.json')
    for file,h in validation['evidence_hashes'].items():assert norm(HERE/file)==h,file
    for file,h in validation['source_hashes'].items():assert norm(source(file))==h,file
    return dict(status=m['status'],evidence_integrity='PASS',Agent=563,Oagnet=261,live_model_calls=11,
        invalid_live_inputs=3,CONTEXT_FOLLOWUP_READY='NOT_READY',READY_FOR_V2_READ_ONLY_E2E_SMOKE=False)

if __name__=='__main__':print(json.dumps(verify()))
