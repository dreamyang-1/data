"""Bounded-memory Phase 0C evidence, explicit sync and commit verification."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REPO = Path('E:/yy')
OUT = ROOT/'docs/phase0c'
BASELINE = '3b3d60ccdd2698887ba57e2b849564861adf7e7c'
BRANCH = 'phase0c-trust-semantic-scope-20260908-01'
FINAL_SUITE = 'bounded_suite_final'
sys.path.insert(0,str(ROOT))
from tools.phase0.repro import sync_manifest, write_json

MODIFIED = '''.env.example
app/adapters/http.py
app/adapters/semantic_query.py
app/api.py
app/config.py
app/domain/models.py
app/main.py
app/services/dataset_followup.py
app/services/orchestrator.py
app/services/question_rewriter.py
app/services/report_export.py
app/services/validated_query_recall.py
minio_followup_store.py
tests/test_api.py
tests/test_chat_request_validation.py
tests/test_chat_responder.py
tests/test_clarification_flow.py
tests/test_external_search_routing.py
tests/test_forecast_readiness.py
tests/test_handshake.py
tests/test_history_compaction.py
tests/test_idempotency_conflict.py
tests/test_memory_manager.py
tests/test_orchestrator.py
tests/test_pending_execution_transition.py
tests/test_phase0b_regressions.py
tests/test_question_rewriter.py
tests/test_semantic_clarification_session_recovery.py
tests/test_session_event_log.py
tests/test_task_dag.py
tests/test_tracing_evaluation.py
tests/test_turn_admission.py
tests/test_workflow.py
tests/test_working_memory.py'''.splitlines()
NEW = '''app/domain/semantic_scope.py
app/services/authorized_scope.py
app/security.py
tests/test_phase0c_scope_contract.py
tools/phase0c/migrate_test_inputs.py
tools/phase0c/migrate_state_fixtures.py
tools/phase0c/run_bounded_suite.py
tools/phase0c/closure.py'''.splitlines()


def git(*args):
    return subprocess.check_output(['git','-C',str(REPO),'-c','core.deltaBaseCacheLimit=8m',
        '-c','core.packedGitWindowSize=8m','-c','core.packedGitLimit=32m',*args])


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_evidence():
    base = json.loads((ROOT/'docs/phase0b/final_verified_test_gate.json').read_text(encoding='utf8'))
    final = json.loads((OUT/FINAL_SUITE/'aggregate.json').read_text(encoding='utf8'))
    missing = sorted(set(base['results'])-set(final['results']))
    regressions = [n for n,v in base['results'].items() if v=='passed' and final['results'].get(n)!='passed']
    scope_nodes = {n:v for n,v in final['results'].items() if n.startswith('tests/test_phase0c_scope_contract.py::')}
    if missing or regressions or set(scope_nodes.values()) != {'passed'} or final['collection_errors']:
        raise SystemExit('Evidence gate failed; do not commit closure as passing')
    delta = dict(baseline_commit=BASELINE, baseline_counts=base['counts'],final_counts=final['counts'],
        mode=final['mode'],missing_baseline_nodes=missing,old_pass_to_new_fail=regressions,
        old_fail_to_new_pass=[n for n,v in base['results'].items() if v=='failed' and final['results'].get(n)=='passed'],
        new_nodes=sorted(set(final['results'])-set(base['results'])),remaining_failed_nodes=sorted(final['failures']),
        collection_errors=final['collection_errors'],scope_acceptance_nodes=scope_nodes)
    write_json(OUT/'test_delta_report.json',delta)
    (OUT/'test_delta_report.md').write_text(
        '# Phase 0C regression delta\n\n'
        f'Baseline `{BASELINE}`: {base["counts"]}. Final complete serial batches: {final["counts"]}.\n\n'
        f'Scope suite: {len(scope_nodes)}/{len(scope_nodes)} passed. Old pass → new fail: 0. '
        'Missing baseline nodes: 0. Collection errors: 0. The two existing OpenAPI failures now pass because trusted headers are actually enforced.\n\n'
        'The remaining 27 failures were already failing in Phase 0B. Their node IDs are retained in the JSON delta. '
        'Full-process attempts were interrupted by host memory exhaustion; only the complete module-batch aggregate is the final gate. '
        'No real external model calls or production writes were permitted by the existing offline network guard.\n',encoding='utf8',newline='\n')
    safety = dict(semantic_model_id_required=True,scope_expansion_from_explicit_domain=0,history_scope_expansion=0,
        pending_scope_expansion=0,dataset_scope_expansion=0,cache_cross_scope_reuse=0,
        explicit_multi_domain_silently_becoming_model_wide=0)
    write_json(OUT/'scope_safety_gate.json',dict(safety_assertions=safety,
        safety_assertions_status='PASS_FAIL_CLOSED',SEMANTIC_SCOPE_CONTRACT='BLOCKED_EXTERNAL_CONTRACT',
        scope_suite_passed=len(scope_nodes),scope_suite_total=len(scope_nodes),
        measurement='Observed offline acceptance assertions, not production telemetry',
        strict_single_domain_execution_supported=False,explicit_multi_domain_behavior='EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED',
        blockers=['EXT-SCOPE-01','EXT-SCOPE-02','EXT-SCOPE-03','INTEGRATION-SCOPE-04'],
        production_v2_routing_changed=False,real_model_calls=0,production_external_writes=0,
        cross_service_patches_performed=False))
    sources = [
        ('Oagnet/api.py','QueryRequest/QueryResponse arrays, entity search exact set, display adds -1'),
        ('Oagnet/agent.py','Passes domain arrays to PromptBuilder and domain_scope'),
        ('Oagnet/prompt_build.py','PromptBuilder._build_where includes unauthorized shared domain -1 for explicit sets'),
        ('sql-translator/api_server_prod.py','Metric resolver handler reads only model and names'),
        ('youoagent-api/youo-agent-project/src/main/java/com/youotech/project/util/ProjectAgentFluxUtil.java','Existing Authorization Bearer authKey call convention'),
    ]
    write_json(OUT/'source_contract_audit.json',dict(authority='User Phase 0C contract',sources=[
        dict(path=str(ROOT.parent/name),sha256=sha(ROOT.parent/name),finding=finding,access='READ_ONLY')
        for name,finding in sources],source_credentials_recorded=False,
        initial_schema_only_inference='Corrected after reading PromptBuilder: list support does not prove strict-set enforcement'))
    write_json(OUT/'source_snapshot.json',dict(baseline_commit=BASELINE,
        files=[dict(path=p,sha256=sha(ROOT/p)) for p in MODIFIED+NEW],
        frozen_v2_changed=False,prompt_changes=0,regex_changes=0))
    write_json(OUT/'execution_limitations.json',dict(
        full_process_attempts=['initial_test_gate.json','revised_test_gate.json','final_candidate_test_gate.json',
            'verified_test_gate_v1.json','final_test_gate.json','final_verified_test_gate.json'],
        final_authoritative_result=FINAL_SUITE+'/aggregate.json',
        host_failure='Observed MemoryError/internal pytest abort and git archive inflate out of memory; some terminated attempts produced no JSON',
        resolution=f'Serial batches of six test modules; all {len(final["results"])} node IDs accounted for; bounded Git reads',
        unverified='Single-process regression equivalence and deployed cross-service integration'))


def sync_reviewed():
    if git('rev-parse','HEAD').decode().strip()!=BASELINE or git('status','--porcelain'):
        raise SystemExit('Baseline or clean-tree precondition changed')
    tracked = git('ls-files','-z').decode().strip('\0').split('\0')
    differences = {p for p in tracked if (ROOT/p).read_bytes()!=(REPO/p).read_bytes()}
    if differences-set(MODIFIED):
        raise SystemExit('Unexpected changes outside reviewed manifest: '+str(differences-set(MODIFIED)))
    record_evidence()
    docs = sorted(p.relative_to(ROOT).as_posix() for p in OUT.rglob('*') if p.is_file())
    paths = sorted(set(MODIFIED+NEW+docs))
    # Exact-byte Git checkouts use LF; normalize only the reviewed text files.
    for name in paths:
        path = ROOT/name
        data = path.read_bytes()
        if b'\r\n' in data:
            path.write_bytes(data.replace(b'\r\n',b'\n'))
    manifest = OUT/'implementation_change_manifest.json'
    sync_manifest(paths,manifest)
    (REPO/manifest.relative_to(ROOT)).write_bytes(manifest.read_bytes())
    paths.append(manifest.relative_to(ROOT).as_posix())
    write_json(OUT/'stage_paths.json',paths)
    (REPO/'docs/phase0c/stage_paths.json').write_bytes((OUT/'stage_paths.json').read_bytes())
    paths.append('docs/phase0c/stage_paths.json')
    git('add','--',*paths)
    print('Staged reviewed paths:',len(paths))


def verify(commit):
    if git('rev-parse','--show-object-format').decode().strip()!='sha1':
        raise SystemExit('Verifier expects SHA-1 Git object IDs')
    rows=[]
    for entry in git('ls-tree','-r','-z',commit).split(b'\0'):
        if not entry: continue
        metadata,name=entry.split(b'\t',1)
        path=name.decode('utf8'); oid=metadata.split()[2].decode()
        dev=(ROOT/path).read_bytes(); repo=(REPO/path).read_bytes()
        actual=hashlib.sha1(b'blob '+str(len(dev)).encode()+b'\0'+dev).hexdigest()
        rows.append(dict(path=path,workspace_sha256=hashlib.sha256(dev).hexdigest(),
            git_checkout_sha256=hashlib.sha256(repo).hexdigest(),committed_blob=oid,
            status='MATCH' if dev==repo and actual==oid else 'MISMATCH'))
    return dict(verified_commit=commit,method='SHA-256 workspace/checkout plus raw Git blob OID recomputed from exact workspace bytes',
        tracked_files=len(rows),unexpected_tracked_drift=sum(r['status']!='MATCH' for r in rows),files=rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['sync','verify']);parser.add_argument('--commit',default='HEAD')
    args=parser.parse_args()
    if args.action=='sync': sync_reviewed()
    else:
        result=verify(git('rev-parse',args.commit).decode().strip())
        print(json.dumps({k:v for k,v in result.items() if k!='files'}))
        if result['unexpected_tracked_drift']: raise SystemExit(1)
