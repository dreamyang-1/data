"""Explicit Phase 0B documentation synchronization and raw-commit verification."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))
from repro import ROOT, REPO, git, sha, tracked, write_json, sync_manifest, commit_files
from failure_inventory import BASE

OUT=ROOT/'docs/phase0b'
DOCS=[
    'failure_universe.csv','failure_universe.json','failure_universe_baseline.json',
    'failure_triage_matrix.csv','failure_triage_matrix.json','p0_failure_root_cause_report.md',
    'critical_multiturn_scenarios.jsonl','legacy_multiturn_regression_matrix.csv',
    'temporal_failure_matrix.csv','semantic_grounding_failure_matrix.csv',
    'external_service_fix_candidates.md','enterprise_security_blockers.md',
    'gold_candidate_inventory.jsonl','test_delta_report.md','closure_counts.json',
    'scope_freeze_verification.json','rollback_manifest.json','phase0b_closure_report.md',
    'git_commit_manifest.json',
]
TOOLS=['tools/phase0/close_evidence.py','tools/phase0/finalize_repository.py']
VERIFICATION='docs/phase0b/workspace_git_hash_verification.json'
SYNC='docs/phase0b/closure_sync_manifest.json'
FINAL_RESOLVER='git log -1 --format=%H -- docs/phase0b/phase0b_closure_report.md'


def prepare():
    assert not git('status','--porcelain').strip(), 'Preserve unrelated Git work'
    counts=json.loads((OUT/'closure_counts.json').read_text(encoding='utf-8'))
    assert counts['old_pass_to_new_fail']==counts['collection_errors']==0
    assert counts['critical_scenarios']==counts['critical_passed']==15
    behavior=git('rev-parse','HEAD').decode().strip()
    assert behavior.startswith('82e1a48')
    report=f'''# Phase 0B Legacy Multi-turn P0 Closure

Status: **CONDITIONALLY_READY_FOR_PHASE_0C**. Critical deterministic acceptance passes; identity, catalog display publication, nonmetric lineage and incomplete historical evidence remain explicit owner/evidence blockers. No production readiness claim is made.

Development workspace: `E:/YouoAgent/DataAnalysis_Agent`.
Git repository: `E:/yy`.
Feature branch: `phase0b-legacy-multiturn-p0-closure-20260907-01`.
Baseline / PHASE0A_BASELINE_COMMIT: `{BASE}`.
Evidence commit: `42baf3c`.
Behavior commit: `{behavior}`.
Final closure commit: resolve with `{FINAL_RESOLVER}`. A commit cannot contain its own SHA; this exact tracked report binds the final commit and the postcommit verifier checks every committed blob. There are three Phase 0B commits: evidence, coherent Legacy guard repair, closure documentation.
GitHub head: `https://github.com/dreamyang-1/data/tree/phase0b-legacy-multiturn-p0-closure-20260907-01`.
Draft PR: https://github.com/dreamyang-1/data/pull/2, base `phase0a-repro-closure-20260907-01`; Phase 0A PR is https://github.com/dreamyang-1/data/pull/1. No automatic merge.

## Phase 0A prerequisite

Phase 0A Gate passed before this branch existed: 420 raw committed files matched both workspaces, unexpected tracked source drift 0, Git clean, tests 1696/32 with zero collection errors. Source behavior was unchanged in Phase 0A; four preexisting Python drifts were reconciled exactly. The ignored runtime collector was transiently copied before untracking due to a filename encoding error; it never entered the commit. The original Git placeholder was restored, dev runtime retained, and explicit deny guards prevent repetition. See phase0a_evidence_corrections.json; no published history was rewritten.

## Failure universe and classification

81 observations = 32 original pytest failures + 30 historical partial-trace cases + 15 critical scenarios + 4 source-audit findings. Three baseline critical passes are acceptance controls. **35 unique failure signatures**, grouped by first divergence, intent family, dialogue act, affected slot and normalized error behavior. Historical inference is never treated as a proven current implementation defect.

| Classification | Unique signatures | Observation details |
| --- | ---: | --- |
| REAL_PRODUCTION_BUG | 17 | 5 old failed nodes, 11 critical failures, 3 clarification findings; shared roots deduplicated |
| STALE_TEST | 4 | 15 old failed nodes; expectations retained |
| UNKNOWN_NEEDS_EVIDENCE | 11 | 9 old failed nodes + all 30 partial historical cases |
| LEGACY_BEHAVIOR_DEBT | 1 | 3 old event/memory trace failures, P2; pass controls excluded |
| EXTERNAL_SERVICE_CONTRACT_BUG | 1 | typed nonmetric lineage contract absent |
| CATALOG_GOVERNANCE_GAP | 1 | default display publication not established |
| ENVIRONMENT_DEPENDENCY / FLAKY | 0 / 0 | no such cause inferred without evidence |

All 32 current failures were independently repeated twice: 64 process runs, **32 deterministic / 0 flaky / 0 environment-dependent**, no third run needed. Historical traces are not fully replayable because original state, catalog, model and result artifacts are missing; each inventory row specifies the evidence needed. Four provisional stale decisions were reverted to UNKNOWN after evidence review.

Verified real P0 before: **17**; fixed: **16**; remaining: **1**, SECURITY_P0 with an explicit identity-platform owner. In addition, one catalog and one external lineage capability blocker remain. Historical P0 candidates are UNKNOWN, not silently counted as fixed. No unresolved verified in-scope Legacy P0 is left without a named blocker.

## Behavior outcomes

| Capability | Result and practical boundary |
| --- | --- |
| TurnAdmission P0 | PASS: missing execution slots alone cannot imply reference ambiguity; no Gate rewrite |
| Pending hijack | PASS: complete new request wins; typed answers and candidate options may resume; unrelated text cannot bind automatically; root DAG included |
| Wrong inheritance | PASS: new hospital question does not inherit old sales; region follow-up preserves metric/time |
| ADD | PASS: retain old metrics; append new metric; idempotent contrast and real orchestration tested |
| REPLACE | PASS: replace region/metric, remove old value; contrasting ADD retains values |
| REMOVE | PASS: remove named metric only, retained values remain; audit operation is REMOVE |
| CLEAR | PASS: region inheritance barrier survives next grain turn; explicit new region reopens scope |
| Lineage | Readiness PASS for metric/field/column/table/entity/dataset; actual nonmetric execution is EXT-01 blocked |
| Detail/list | Explicit projection PASS; versioned permitted catalog default fixture PASS; actual missing catalog policy is CAT-01 blocked |
| Temporal | Fixed clock; explicit-time, default-policy and turn-relation causes separated. Forecast/report default-policy conflicts remain UNKNOWN |
| Semantic grounding | Quantity metric preserved; explicit name projection/grouping separated; bare name asks operation. Live scores, role permissions and remaining collisions need governed catalog evidence |
| Dataset follow-up | DISPLAY_LIMIT, LOCAL_SORT, GLOBAL_RANKING, PROJECTION and DRILLDOWN distinguished; existing local operations avoid full replanning, incomplete rows cannot establish global rank. Unknown operations are not labeled drilldown |
| History/task state | Existing scoped task references and completed-query context are retained; no event store/CAS/memory redesign; historical 30-case snapshots remain unavailable |
| Clarification | Ordinary/root-composite/cached final responses carry reason codes; real ambiguity, default, repair and already-asked gates enforced; unshown questions are not marked asked |

Trace coverage observed in the whole offline suite: **23/23 public clarification responses with reason trace**, untraced 0, unsafe ASK 0. This is measured test coverage, not a guarantee over unseen live catalog states. Trace contains no raw business question, SQL or dataset rows; it uses bounded codes, task/pending references and opaque candidate digests.

Critical P0 suite: **15/15 PASS, 100%**. Additional regression/contrast/metamorphic tests: **49/49 PASS**. All four slot operations have single-turn, multi-turn and contrast evidence. Catalog defaults use a controlled authorized fixture; nonmetric lineage tests verify readiness and explicit capability blocking, not nonexistent service support.

## Test and scope evidence

Baseline: **1696 passed / 32 failed**.
Final: **1763 passed / 29 failed**.
Collection errors: **0**.
Old pass → new fail: **0**; old fail → pass: **3**.
New regression tests: **64**.
Two old passing fixtures changed under documented current business contracts; original failing expectations were retained. See stale_test_decisions.md for old/actual/authority/decision evidence.

Prompt modifications: **0**.
Regex modifications: **4 bounded families / 5 sites**, with positive and contrast evidence in regex_change_register.md.
Production V2 routing changed: **NO**, V2 remains **SHADOW_ONLY**; protected files have baseline-identical hashes.
Cross-service patches performed: **NO**.
Real external model calls: **0**.
Production external writes: **0**.
No new dependencies installed. Business date uses the existing monkeypatch seam, 2026-09-07. Offline network guard blocks DNS/connect and disables .env/runtime collection. Final node outcomes and trace counts: final_verified_test_gate.json.

## Gate and next owner decisions

Critical suite 100%, old-pass regression 0, collection errors 0, failure inventory fully classified or explicit UNKNOWN evidence need, no V2 routing/model/write scope violations. Verified in-scope repairs are complete. Closure remains **CONDITIONALLY_READY_FOR_PHASE_0C** because live catalog/lineage contracts and identity boundary are unresolved, and historical cases are partial evidence. Gold candidates are PARTIAL_REQUIRES_HUMAN_REVIEW; no model marks them COMPLETE.

Highest blocker: **SECURITY_P0 — app/api.py returns fixed default-tenant/default-user**. Separately, the Catalog owner must publish permitted default display policies, sql-translator owner must agree typed lineage, and analysis/business owners must resolve forecast/report/partial-result semantics and recover historical replay artifacts. Identity changes are explicitly deferred under the requested scope.

Repository closure uses an explicit sync manifest, rejects credentials/runtime files and unrelated dirty changes, stages named paths only, and checks raw committed SHA-256 bytes against both workspaces. Final verification command: `python tools/phase0/finalize_repository.py verify`. Intermediate test output is local generated evidence, not formal source; no directory mirroring, force push, reset, clean or stash was used.
'''
    (OUT/'phase0b_closure_report.md').write_text(report,encoding='utf-8',newline='\n')
    paths=['docs/phase0b/'+p for p in DOCS]+TOOLS
    series=[dict(commit=line.split(' ',1)[0],title=line.split(' ',1)[1]) for line in git('log','--reverse','--format=%H %s',BASE+'..HEAD').decode().splitlines()]
    modified=git('diff','--name-only',BASE,'HEAD').decode().splitlines()
    manifest=dict(PHASE0A_BASELINE_COMMIT=BASE,branch=git('branch','--show-current').decode().strip(),prior_commits=series,
        final_commit_resolver=FINAL_RESOLVER,final_commit_title='phase0b: finalize legacy p0 closure evidence',draft_pr='https://github.com/dreamyang-1/data/pull/2',
        explicit_series_paths=sorted(set(modified+paths+[VERIFICATION,SYNC])),test_evidence='docs/phase0b/final_verified_test_gate.json',
        source_authority='Tested development workspace; explicit manifest sync after unrelated-edit checks',rollback='docs/phase0b/rollback_manifest.json',
        excludes='No .env, credentials, runtime collection, logs, caches or backup files',self_reference='Final SHA resolved by tracked closure-report commit; postcommit raw-blob verification checks all files including metadata.')
    write_json(OUT/'git_commit_manifest.json',manifest)
    sync_manifest(paths,ROOT/SYNC)
    (REPO/SYNC).write_bytes((ROOT/SYNC).read_bytes())
    all_paths=sorted(set(tracked()+paths+[SYNC]))
    rows=[dict(path=p,workspace_hash=sha(ROOT/p),git_hash=sha(REPO/p)) for p in all_paths]
    assert all(r['workspace_hash'] and r['workspace_hash']==r['git_hash'] for r in rows)
    local_artifacts=sorted(p.relative_to(ROOT).as_posix() for p in OUT.glob('*.json') if p.relative_to(ROOT).as_posix() not in {*all_paths,VERIFICATION})
    write_json(ROOT/VERIFICATION,dict(status='PASS',phase0a_baseline_commit=BASE,verified_payload_files=len(rows),unexpected_tracked_source_drift=0,
        verification_scope='Prepared final commit payload: both workspaces match; verification file excludes itself from embedded hashes, and postcommit command includes every raw blob.',
        final_commit_resolver=FINAL_RESOLVER,postcommit_verification='python tools/phase0/finalize_repository.py verify',
        expected_local_generated_artifacts=local_artifacts,files=rows))
    (REPO/VERIFICATION).write_bytes((ROOT/VERIFICATION).read_bytes())
    all_stage=paths+[SYNC,VERIFICATION]
    subprocess.run(['git','-C',str(REPO),'add','--',*all_stage],check=True)
    subprocess.run(['git','-C',str(REPO),'diff','--cached','--check'],check=True)
    # Recheck staged blobs, so Git line-ending conversions cannot escape the Gate.
    for p in all_stage:
        assert hashlib.sha256(git('show',':'+p)).hexdigest()==sha(ROOT/p),p
    print(json.dumps({'prepared_explicit_paths':len(all_stage),'staged_byte_verification':'PASS'}))


def commit():
    allowed={'docs/phase0b/'+p for p in DOCS}|set(TOOLS)|{VERIFICATION,SYNC}
    staged=set(git('diff','--cached','--name-only').decode().splitlines())
    assert staged and staged<=allowed
    subprocess.run(['git','-C',str(REPO),'diff','--cached','--check'],check=True)
    body='phase0b: finalize legacy p0 closure evidence\n\nReconcile 81 observations into 35 failure signatures, retain explicit historical and policy unknowns, and document catalog, external lineage and enterprise identity blockers. Record clarification/temporal/grounding evidence, human-review Gold candidates, rollback and explicit synchronization manifests. Preserve the published Phase0A history and disclose its runtime-copy/encoding correction.\n\nValidation: 1763 passed, 29 retained failures, 0 collection errors, 0 old-pass regressions. All 15 critical scenarios and 49 additional regressions pass. Trace coverage 23/23 observed public clarifications, zero untraced or unsafe asks. Raw staged bytes match both workspaces; no real model calls, production external writes or cross-service patches. Readiness: CONDITIONALLY_READY_FOR_PHASE_0C.\n'
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.txt',delete=False) as f:
        f.write(body); path=Path(f.name)
    try: subprocess.run(['git','-C',str(REPO),'commit','--file',str(path)],check=True)
    finally: path.unlink()
    verify()


def verify():
    blobs=commit_files('HEAD')
    differences=[p for p,b in blobs.items() if sha(ROOT/p)!=hashlib.sha256(b).hexdigest() or sha(REPO/p)!=hashlib.sha256(b).hexdigest()]
    assert not differences,differences
    assert not git('status','--porcelain').strip()
    receipt=dict(final_commit=git('rev-parse','HEAD').decode().strip(),raw_committed_files=len(blobs),
        committed_bytes_match_both_workspaces=True,unexpected_tracked_source_drift=0,git_working_tree_clean=True,hash_verification='PASS')
    # Receipt is local build output outside the project, avoiding self-SHA recursion.
    write_json(ROOT.parent/'phase0b_postcommit_receipt.json',receipt)
    print(json.dumps(receipt))


if __name__=='__main__':
    {'prepare':prepare,'commit':commit,'verify':verify}[sys.argv[1]]()
