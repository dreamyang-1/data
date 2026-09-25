"""Derive closure evidence from frozen offline results, without live services."""
from __future__ import annotations
import ast
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from repro import ROOT, REPO, git, sha, write_json, write_csv, commit_files
from failure_inventory import BASE, signature

OUT = ROOT / 'docs/phase0b'
FINAL_GATE = 'docs/phase0b/final_verified_test_gate.json'


def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def md(name, text):
    (OUT / name).write_text('\n'.join(line.rstrip() for line in text.strip().splitlines()) + '\n', encoding='utf-8', newline='\n')


def csv(name, rows):
    write_csv(OUT / name, [{k: '\n'.join(s.rstrip() for s in v.splitlines()) if isinstance(v, str) else v for k,v in r.items()} for r in rows])


def fixture_inputs(node):
    path, name = node.split('::', 1)
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    function = next(n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name == name.split('[')[0])
    values = []
    for n in sorted(ast.walk(function), key=lambda n:getattr(n,'lineno',0)):
        if isinstance(n,ast.Call):
            for kw in n.keywords:
                if kw.arg in {'question','original_question'} and isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str):
                    values.append(kw.value.value)
            if isinstance(n.func,ast.Attribute) and n.func.attr in {'classify','merge_clarification'}:
                values.extend(a.value for a in n.args if isinstance(a,ast.Constant) and isinstance(a.value,str) and any('\u4e00' <= c <= '\u9fff' for c in a.value))
    return list(dict.fromkeys(values))


def main():
    baseline = read('docs/phase0a/test_gate.json')
    final = read(FINAL_GATE)
    assert not final['collection_errors']
    regressed = [n for n,s in baseline['results'].items() if s=='passed' and final['results'].get(n)!='passed']
    assert not regressed, regressed
    original = git('show','42baf3c:docs/phase0b/failure_universe.json')
    (OUT/'failure_universe_baseline.json').write_bytes(original)
    universe = json.loads(original)
    items = universe['items']
    for r in items:
        r['baseline_status'] = r['status']
        r['baseline_root_cause_class'] = r['root_cause_class']
        r['final_test_outcomes'] = {n:final['results'].get(n,'NOT_COLLECTED') for n in r['test_nodeids']}
        r['evidence_need'] = None
        if 'CURRENT_PYTEST' in r['source']:
            index = int(r['failure_id'].split('-')[1])
            inputs = fixture_inputs(r['test_nodeids'][0])
            r['fixture_utterances'] = inputs
            r['conversation_history'] = inputs[:-1] if len(inputs)>1 else []
            r['current_utterance'] = inputs[-1] if inputs else None
            r['input_extraction_note'] = 'Literal fixture inputs in source order; parameterized/shared-helper state remains in referenced test source. Not a reconstructed production conversation.'
            if index in {7,13,14,25}:
                r['root_cause_class'] = 'UNKNOWN_NEEDS_EVIDENCE'
                r['status'] = 'UNKNOWN_NEEDS_EVIDENCE'
                r['evidence_need'] = {
                    7:'Owner must confirm whether an automatic historical window counts as supplied forecast history; parser output alone is not a business contract.',
                    13:'Pending-transition test starts from obsolete missing-period setup; terminal-call count needs isolated state replay before assigning the downstream cause.',
                    14:'Pending setup and backend ambiguity behavior interact; need an isolated governed candidate fixture before assigning the later negation cause.',
                    25:'Report owner must confirm whether every master-data facet shares transaction time or retains all-history scope.',
                }[index]
                if index in {13,14}: r['first_divergence_stage']='UNKNOWN'
            elif r['root_cause_class']=='STALE_TEST':
                r['status']='STALE_EXPECTATION_RETAINED'
            elif all(v=='passed' for v in r['final_test_outcomes'].values()):
                r['status']='FIXED'
            elif r['root_cause_class']=='UNKNOWN_NEEDS_EVIDENCE':
                r['status']='UNKNOWN_NEEDS_EVIDENCE'
                r['evidence_need']='Analysis contract owner must settle partial-success vs terminal-fallback policy with representative dataset evidence.'
            elif r['root_cause_class']=='LEGACY_BEHAVIOR_DEBT':
                r['status']='DEFERRED_P2'
            family = 'FORECAST' if index==7 else 'REPORT' if index in {*range(17,24),25} else 'ANALYSIS' if index>=28 else 'QUERY'
            behavior = r['failure_signature'].split('|')[-1]
            slot = 'query_subject' if index in {5,6} else r['affected_business_capability']
            if index==7: behavior='forecast_history_default_policy'
            if index==13: behavior='pending_terminal_call_count'
            if index==14: behavior='pending_metric_negation_fixture'
            if index==25: behavior='report_shared_period_policy'
            r['failure_signature'],r['signature_id']=signature(r['first_divergence_stage'],family,'QUERY',slot,behavior)
        elif 'HISTORICAL_PARTIAL_TRACE' in r['source']:
            # Deduplicate symptom families, never claim an inferred stage as a proved root cause.
            text=r['current_utterance']; response=r['actual_behavior']
            act='ADD' if '再加' in text else 'DISPLAY_LIMIT' if '前5' in text else 'REPLACE' if '改成' in text or '改按' in text else 'NEW_TASK'
            if act=='ADD': slot,behavior='metrics','metric_set_not_preserved'
            elif act=='DISPLAY_LIMIT': slot,behavior='dataset','limit_or_projection_not_preserved'
            elif act=='REPLACE': slot,behavior='time_grain','grain_not_reconciled'
            elif 'NEEDS_CLARIFICATION' in response: slot,behavior='clarification','unnecessary_question_candidate'
            elif '医院' in text: slot,behavior='fields','relationship_projection_alignment'
            else: slot,behavior='fields','analysis_projection_alignment'
            r['severity']='P0'
            r['severity_note']='Business P0 candidate; root cause and present-day reproducibility remain UNKNOWN.'
            r['failure_signature'],r['signature_id']=signature('UNKNOWN', 'RELATIONSHIP' if '医院' in text else 'ANALYSIS',act,slot,behavior)
            r['evidence_need']='Original scoped pending/task/dataset state, structured intent, governed catalog version/candidates, ASL/SQL and validated result. Historical proposed stages are inferences only.'
        else:
            index=int(r['failure_id'].split('-')[1])
            r['status']='PASS_AT_BASELINE' if r['baseline_status']=='PASS_AT_BASELINE' else 'CATALOG_OWNER_BLOCKER' if index==9 else 'FIXED'
            r['evidence'].append(FINAL_GATE)
            if index==9:
                r['owning_service']='Semantic Catalog owner'
                r['evidence_need']='Publish a versioned permitted default_display_attributes policy for hospital/entity lists; offline fixture proves consumption only.'
            if index==12:
                r['evidence'].append('Baseline orchestrator persisted no source_truncated provenance and could reuse explicit incomplete rows; baseline TypeError alone is an acceptance API gap, not the root-cause evidence.')
            if index==14:
                r['current_utterance']='商品名称 / 列出商品名称 / 按商品名称统计销售额'
                r['expected_behavior']='Bare surface asks list-vs-group operation; explicit projection/grouping preserves role and never invents a measure.'
    def observation(fid,cls,stage,slot,behavior,expected,evidence,status,service='DataAnalysis_Agent',nodes=None):
        sig,key=signature(stage,'LEGACY','QUERY',slot,behavior)
        return dict(failure_id=fid,source=['CLOSURE_SOURCE_AUDIT'],test_nodeids=nodes or [],historical_case_ids=[],conversation_history=None,current_utterance=None,
            expected_behavior=expected,actual_behavior='See baseline source and final regression evidence',affected_business_capability=slot,repeatability='OFFLINE_SOURCE_AND_REGRESSION',
            first_divergence_stage=stage,root_cause_class=cls,severity='P0',owning_service=service,evidence=evidence,status=status,failure_signature=sig,signature_id=key,
            baseline_status='OPEN',baseline_root_cause_class=cls,final_test_outcomes={n:final['results'].get(n) for n in nodes or []},evidence_need=None)
    nodes=list(final['results'])
    items.extend([
        observation('trace-01','REAL_PRODUCTION_BUG','INTENT_ASL_CONTRACT','clarification','missing_reason_trace','Every final clarification has a structured scoped reason',
            ['e4d3bb5:app/domain/models.py:AgentResponse lacked ClarificationDecisionTrace',FINAL_GATE],'FIXED',nodes=[n for n in nodes if '::test_final_clarification_has_scoped_reason_' in n]),
        observation('trace-02','REAL_PRODUCTION_BUG','PENDING_ADMISSION','clarification','repeated_pending_question','Do not re-ask the same unresolved Pending question',
            ['e4d3bb5:app/services/orchestrator.py:_request_clarification and _resume_task_plan unconditionally reissued questions',FINAL_GATE],'FIXED',nodes=[n for n in nodes if '::test_same_pending_question_' in n or '::test_root_dag_mapping_question_' in n]),
        observation('trace-03','REAL_PRODUCTION_BUG','INTENT_ASL_CONTRACT','clarification','backend_failure_becomes_user_question','Backend failure without concrete user ambiguity produces an explicit fallback',
            ['e4d3bb5:app/services/orchestrator.py:ASL_AMBIGUOUS/SQL_AMBIGUOUS flowed directly to clarification',FINAL_GATE],'FIXED',nodes=[n for n in nodes if '::test_backend_failure_cannot_' in n]),
        observation('external-01','EXTERNAL_SERVICE_CONTRACT_BUG','SQL_TRANSLATOR','lineage','metric_only_lineage_endpoint','Resolve permitted typed FIELD/COLUMN/TABLE/ENTITY/DATASET targets without pretending they are metrics',
            ['E:/YouoAgent/sql-translator/api_server_prod.py:194-196,264-270','E:/YouoAgent/sql-translator/sql_translator_prod.py:495','app/adapters/base.py:SemanticAdapter.lineage'],
            'EXTERNAL_OWNER_BLOCKER',service='sql-translator'),
    ])
    groups=defaultdict(list)
    for r in items:
        if r['status']!='PASS_AT_BASELINE': groups[r['signature_id']].append(r)
    group_rows=[]
    for key, rows in groups.items():
        assert len({r['root_cause_class'] for r in rows})==1
        group_rows.append(dict(signature_id=key,failure_signature=rows[0]['failure_signature'],failure_ids=[r['failure_id'] for r in rows],
            source=sorted({s for r in rows for s in r['source']}),test_nodeids=sorted({s for r in rows for s in r['test_nodeids']}),historical_case_ids=sorted({s for r in rows for s in r['historical_case_ids']}),
            first_divergence_stage=rows[0]['first_divergence_stage'],root_cause_class=rows[0]['root_cause_class'],severity=rows[0]['severity'],status=rows[0]['status'],
            evidence_need=[r['evidence_need'] for r in rows if r['evidence_need']],evidence=sorted({str(s) for r in rows for s in r['evidence']})))
    categories=Counter(r['root_cause_class'] for r in group_rows)
    real=[r for r in group_rows if r['root_cause_class']=='REAL_PRODUCTION_BUG']
    new=[n for n in final['results'] if n not in baseline['results']]
    critical=[n for n in final['results'] if n.startswith('tests/test_phase0b_critical.py::')]
    counts=dict(observations=len(items),source_observations=dict(CURRENT_PYTEST=32,HISTORICAL=30,CRITICAL=15,CLOSURE_AUDIT=4),
        acceptance_controls=3,unique_failure_signatures=len(groups),categories=dict(categories),real_production_bugs=len(real),
        p0_before=len(real),p0_fixed=sum(r['status']=='FIXED' for r in real),p0_remaining=sum(r['status']!='FIXED' for r in real),
        deterministic_current=32,flaky=0,environment=0,baseline_passed=baseline['counts']['passed'],baseline_failed=baseline['counts']['failed'],
        final_passed=final['counts']['passed'],final_failed=final['counts']['failed'],collection_errors=0,old_pass_to_new_fail=0,new_regression_tests=len(new),
        critical_scenarios=len(critical),critical_passed=sum(final['results'][n]=='passed' for n in critical),clarification_coverage=final['clarification_coverage'],
        readiness='CONDITIONALLY_READY_FOR_PHASE_0C',highest_blocker='SECURITY_P0: fixed trusted identity; catalog/default-display and typed lineage contracts also await owners')
    assert counts['critical_scenarios']==counts['critical_passed']==15
    assert final['clarification_coverage']['untraced']==final['clarification_coverage']['unsafe_asks']==0
    write_json(OUT/'closure_counts.json',counts)
    write_json(OUT/'failure_universe.json',dict(baseline_commit=BASE,**{k:counts[k] for k in ['observations','source_observations','acceptance_controls','unique_failure_signatures']},
        deduplication='Stage + intent family + dialogue act + affected slot + normalized error behavior. Unknown historical symptoms are grouped but never merged into verified implementation defects. Baseline classifications are preserved and reviewed; pass controls excluded.',items=items))
    # Additional extraction notes have sparse keys; CSV projects the required common schema.
    cols=['failure_id','source','test_nodeids','historical_case_ids','conversation_history','current_utterance','expected_behavior','actual_behavior','affected_business_capability','repeatability','first_divergence_stage','root_cause_class','severity','owning_service','evidence','status','failure_signature','signature_id','baseline_status','baseline_root_cause_class','final_test_outcomes','evidence_need']
    csv('failure_universe.csv',[{k:r.get(k) for k in cols} for r in items])
    write_json(OUT/'failure_triage_matrix.json',dict(counts=counts,signatures=group_rows))
    csv('failure_triage_matrix.csv',group_rows)
    scenarios=[]
    for r in items:
        if 'CRITICAL_SCENARIO_SUITE' in r['source']:
            scenarios.append(dict(case_id=r['failure_id'],history=r['conversation_history'],current_utterance=r['current_utterance'],expected_behavior=r['expected_behavior'],source='CURRENT_USER_REQUIREMENT',test_nodeid=r['test_nodeids'][0],baseline_status=r['baseline_status'],final_status='PASS',limitations=r['evidence_need']))
    (OUT/'critical_multiturn_scenarios.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in scenarios),encoding='utf-8',newline='\n')
    csv('legacy_multiturn_regression_matrix.csv',[dict(test_nodeid=n,source='CRITICAL' if n in critical else 'REGRESSION_CONTRAST_METAMORPHIC',status=final['results'][n],evidence=FINAL_GATE) for n in new])
    temporal=[]
    for r in items[:32]:
        if r['affected_business_capability']=='time_range' or any(x in r['test_nodeids'][0] for x in ['forecast','period','day_range','date_reply']):
            temporal.append(dict(failure_id=r['failure_id'],test_nodeids=r['test_nodeids'],temporal_class='STALE_TEST' if r['root_cause_class']=='STALE_TEST' else None,
                evidence_status='CONFIRMED_STALE' if r['root_cause_class']=='STALE_TEST' else 'UNKNOWN_POLICY_NOT_PROVEN_BUG',root_cause_class=r['root_cause_class'],clock='2026-09-07',repeatability=r['repeatability'],decision=r['status'],evidence=r['evidence']))
    temporal.extend([dict(failure_id='critical-15',test_nodeids=items[76]['test_nodeids'],temporal_class='TURN_RELATION_POLLUTION',evidence_status='VERIFIED_FIXED',root_cause_class='REAL_PRODUCTION_BUG',clock='2026-09-07',repeatability='OFFLINE',decision='FIXED',evidence=['User section 21',FINAL_GATE])])
    csv('temporal_failure_matrix.csv',temporal)
    collisions=read('docs/phase25/surface_collision_inventory.json')['collisions']
    semantic=[]
    for surface in ['销售数量','商品名称','医院','业务员','订单日期']:
        candidates=next((r['candidates'] for r in collisions if r['surface']==surface),[])
        semantic.append(dict(surface=surface,role_hypothesis='METRIC' if surface=='销售数量' else 'ATTRIBUTE / DIMENSION' if surface=='商品名称' else 'Catalog role depends on explicit operation',catalog_candidates=candidates,
            candidate_types=sorted({c['catalog_type'] for c in candidates}),selected_result='销售量 (deterministic alias; offline metric fixture)' if surface=='销售数量' else 'PROJECTION vs GROUPING; bare name asks operation' if surface=='商品名称' else 'UNRESOLVED_OFFLINE',
            confidence=None,margin=None,permission='Only existing authorized snapshot is consumed; live caller permissions not audited due fixed trusted identity',
            why_rejected='No rejection assumed; live grounding scores/candidate permissions unavailable',evidence='docs/phase25/surface_collision_inventory.json; tests/test_phase0b_critical.py; no live catalog read',status='REGRESSION_VERIFIED_WITH_FIXTURE' if surface in {'销售数量','商品名称'} else 'UNKNOWN_NEEDS_LIVE_GOVERNED_EVIDENCE'))
    csv('semantic_grounding_failure_matrix.csv',semantic)
    gold=[]
    for r in items:
        if r['status']=='PASS_AT_BASELINE' or r['root_cause_class'] in {'REAL_PRODUCTION_BUG','UNKNOWN_NEEDS_EVIDENCE','CATALOG_GOVERNANCE_GAP'}:
            gold.append(dict(case_id=r['failure_id'],history=r['conversation_history'],current_utterance=r['current_utterance'],source=r['source'],business_value=r['expected_behavior'],available_ground_truth=r['test_nodeids'] or ['partial historical assertion'],
                missing_labels=['Human business-owner confirmation','Governed catalog/version and permission labels','Full stage/state evidence' if not r['test_nodeids'] else 'Production representativeness'],candidate_status='PARTIAL_REQUIRES_HUMAN_REVIEW'))
    for n in new:
        if 'regressions.py' in n:
            gold.append(dict(case_id='contrast-'+hashlib.sha256(n.encode()).hexdigest()[:12],history=None,current_utterance=None,source=[n],business_value='Mutation contrast, scope isolation or clarification/dataset safety',available_ground_truth=['Passing deterministic assertion'],missing_labels=['Business-owner review','Extract fixture labels and catalog scope'],candidate_status='PARTIAL_REQUIRES_HUMAN_REVIEW'))
    (OUT/'gold_candidate_inventory.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in gold),encoding='utf-8',newline='\n')
    frozen=commit_files(BASE)
    protected=[p for p in frozen if p.startswith('app/semantic_v2/') or p in {'app/api.py','app/intent/structured.py'}]
    protected_rows=[dict(path=p,baseline_sha256=hashlib.sha256(frozen[p]).hexdigest(),workspace_sha256=sha(ROOT/p)) for p in protected]
    assert all(r['baseline_sha256']==r['workspace_sha256'] for r in protected_rows)
    write_json(OUT/'scope_freeze_verification.json',dict(V2='SHADOW_ONLY',production_v2_routing_changed=False,prompt_changed=False,cross_service_patches=False,protected_files=protected_rows))
    md('test_delta_report.md',f'''# Phase 0B test delta

Baseline `{BASE}`: **1696 passed / 32 failed / 0 collection errors**.
Final `{FINAL_GATE}`: **{counts['final_passed']} passed / {counts['final_failed']} failed / 0 collection errors**.
Old pass → new fail: **0**. Old fail → pass: **3** (pytest-05, pytest-06, pytest-15).
New regression tests: **{len(new)}** = 15 independent critical scenarios + {len(new)-15} contrasts, metamorphic and real Legacy orchestration checks; all pass.
All 32 original failed nodeids ran independently twice: **64 process runs, 32 DETERMINISTIC**, no third run required. No flaky or environmental failure was inferred merely from calendar time.

Two old passing fixtures were updated with explicit current-contract evidence; their nodeids and behavior assertions remain. See stale_test_decisions.md. Old failing expectations remain unchanged, including unresolved policy differences.
Business clock is fixed through the existing classifier.date monkeypatch seam at 2026-09-07. No dependency installation. Offline runner blocks DNS/connect operations, disables .env loading and runtime collection; real external model calls = 0, production external writes = 0.
Trace audit: `{json.dumps(final['clarification_coverage'])}`. This counts public clarification responses observed across the full offline suite (including idempotent cache paths), not live traffic or all possible catalog combinations.
Intermediate debugging runs are local generated artifacts; they are not the final Gate or the original failure universe.
''')
    md('clarification_trace_contract.md','''# ClarificationDecisionTrace / Legacy v1

Every final Legacy clarification passes `_ensure_clarification_trace`, including cache and root composite responses. `_request_clarification` evaluates the gate before writing Pending. Root DAG mapping uses the existing versioned Pending write, without introducing a new concurrency architecture.

Required fields: conversation_id, message_id, source_stage, reason_type, blocking_slot, expected_answer_type, candidate_ids, already_asked, base_task_reference, pending_reference, evidence_codes, is_user_ambiguity, system_repair_possible. Additional gate fields: safe_default_available and decision (ASK/SUPPRESS).

ASK requires a concrete missing business choice or at least two semantic choices, no safe default, no deterministic repair, and an unasked Pending key. Missing fields without a governed default is CATALOG_GOVERNANCE_GAP; backend ambiguity without options is SYSTEM_FAILURE. Supplied metric/lineage target cannot become a repeated missing metric. Duplicate Pending questions are suppressed while state remains available. Invalid shared-report time replies keep the original question without reissuing it. Root task mapping is asked once.

Reasons: MISSING_USER_SLOT, USER_SEMANTIC_AMBIGUITY, USER_REFERENCE_AMBIGUITY, CATALOG_GOVERNANCE_GAP, SYSTEM_FAILURE, REPEATED_QUESTION. Source stages use the actual guard boundary (INTENT_ASL_CONTRACT, OAGNET_ASL_GENERATION, SQL_TRANSLATOR, SESSION_STATE). These identify why the question was sent; the separate failure inventory identifies the earlier root-cause divergence.

Trace stores no raw utterance, business SQL, dataset rows or candidate display labels. Candidate labels are represented by opaque digests; task IDs are existing opaque IDs. Pending retains only hashed question keys in addition to its existing TTL-scoped request state. The final response fills real conversation/message IDs. No new long-term trace store is introduced. Diagnostic `trace-only` placeholders are never trusted execution identities.

Tests: final_clarification_has_scoped_reason, same_pending_question_is_suppressed, backend_failure_cannot_be_rephrased, real_candidate_choice_is_allowed, root_dag_mapping_question_is_not_repeated, clarification_history_is_isolated_by_trusted_scope, question_limit_does_not_mark_unshown_question_as_already_asked. Whole-suite public response coverage is recorded in final_verified_test_gate.json.
''')
    md('external_service_fix_candidates.md','''# External contract candidates — no cross-service patches

## EXT-01 / sql-translator / P0 capability blocker

Read-only source evidence: `E:/YouoAgent/sql-translator/api_server_prod.py:194-196` dispatches only `/v1/semantic/metrics/{metric_id}/lineage`; `_handle_metric_lineage` calls `catalog.lineage(metric_id, version)` at lines 264-270. `sql_translator_prod.py:495` defines metric-only lineage. DataAnalysis's existing SemanticAdapter likewise accepts a metric.
Expected contract: a scope/permission checked, versioned target of METRIC, FIELD, COLUMN, TABLE, ENTITY or DATASET resolves lineage. Actual: nonmetric routes and typed target contract are absent. Recommended external fix: agree and implement typed-target lineage in sql-translator and then align the adapter contract with contract tests. Do not encode a field as a fake metric.
Legacy now recognizes the target and reports the absent downstream capability without asking for a metric. This is not a claim that field lineage can execute today.

## CAT-01 / Semantic Catalog owner / P0 list capability blocker

The frozen `docs/phase2/semantic_catalog_inventory.json` and `docs/phase25/surface_collision_inventory.json` provide entities/attributes and collision evidence, but no governed `default_display_attributes` publication was established. Read-only Oagnet source search did not establish such a contract either.
Expected: entity-specific, immutable catalog version, allowed default projection. Actual: no evidenced policy to consume for the live hospital-list case. Recommended fix: publish governed defaults with role and permission scope; never hardcode hospital fields in Legacy. A controlled fixture proves that the new consumer uses only a matching entity/version and permitted attributes. Missing or invalid policies stay blocked.

Historical result projection/alignment failures only suggest Oagnet/SQL boundaries. They remain UNKNOWN until the original ASL, SQL and result contract evidence is recovered; they are not silently classified as proved external bugs.
Cross-service patches performed: **NO**.
''')
    md('enterprise_security_blockers.md','''# SECURITY_P0 — trusted identity boundary

`app/api.py:trusted_identity` returns fixed `default-tenant` / `default-user`; the public interface does not establish a caller-specific trusted tenant/user boundary. `pytest-09` and `pytest-10` are two assertions associated with this one boundary issue, not two independently repaired bugs. Existing role/header handling does not establish tenant isolation for real callers.

Owner: identity/API platform owner. Severity: SECURITY_P0. Status: OWNER_BLOCKER, intentionally unmodified under user section 32. Required next work: authenticated identity derivation and tenant/user/application/role trust contract; cross-tenant, unauthorized-candidate and spoofing regressions before production exposure. Passing in-memory scoped-state tests verifies the lower-layer keys only.

This phase does not rewrite production identity, introduce V2 production routing, event store, long-term memory or CAS architecture. Highest readiness is Phase 0C Gold preparation, and closure here is conditional on explicit blockers.
''')
    stale=[r for r in items[:32] if r['root_cause_class']=='STALE_TEST']
    sections=['# Stale-test decisions and retained uncertainty\n',
        'No old failing expectation was changed to reduce the failure count. Baseline categorization was reviewed: pytest-07/13/14/25 are downgraded from provisional STALE_TEST to UNKNOWN because current behavior alone does not prove the business contract. Their evidence needs remain in the inventory.\n',
        '## ST-0B-01 — readiness is not turn reference ambiguity\nOld: test_turn_admission.py::test_incomplete_unreferenced_turn_exposes_relation_clarification_state expected AMBIGUOUS_RELATION and needs_clarification=True solely because time was missing. Current requirement sections 18.15 and 21 explicitly separate referential completeness from execution readiness. New: STANDALONE_NEW_TOPIC, needs_clarification=False; no-inheritance assertions retained. This test was already passing before the contract correction; source change is disclosed rather than hidden in zero regression accounting.\n',
        '## ST-0B-02 — make the pending-time fixture actually pending\nOld: test_orchestrator.py::test_time_clarification_preserves_ranked_comparison_execution_contract entered normal handle with a stub requiring time, while the existing deterministic rule layer had already supplied a safe period. The new clarification gate correctly suppresses this artificial question. New fixture constructs a genuinely missing-time request and calls _request_clarification before the unchanged real handle(reply); application and filters are normalized by the existing rule classifier. Ranked comparison/time-preservation assertions are unchanged. Evidence: current requirements 20-21 and stable auditable-default tests.\n',
        '## Retained old failing tests\n']
    for r in stale:
        i=int(r['failure_id'].split('-')[1])
        contract='Phase 2.5.1 committed ClarificationItem additive option_details schema; existing model contract tests' if i==1 else 'Governed business display label 商品名称; stable business-table rendering regressions' if i==16 else 'Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range'
        sections.append(f"### {r['failure_id']}\nNode: `{r['test_nodeids'][0]}`\n\nOld expectation / actual assertion:\n```text\n{r['actual_behavior']}\n```\nAuthoritative support: {contract}. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.\n")
    md('stale_test_decisions.md','\n'.join(sections))
    md('p0_failure_root_cause_report.md',f'''# Verified P0 closure and evidence limits

Failure universe: {len(items)} observations (32 current failures, 30 historical partial traces, 15 critical scenarios, 4 closure audit findings). Three critical cases passed at baseline and are controls. Deduplication yields {len(groups)} failure signatures, not 62 independently assumed bugs. Counts in closure_counts.json are signature counts except explicitly labeled observations/nodeids.

Verified REAL_PRODUCTION_BUG signatures: {len(real)}; P0 fixed: {counts['p0_fixed']}; remaining: {counts['p0_remaining']} (fixed trusted identity, named security owner blocker). The external typed-lineage and catalog display gaps are separate P0 capability blockers. Historical 30 observations are P0 candidates with UNKNOWN root cause and missing replay artifacts; symptom deduplication does not declare them repaired by similar regression fixtures.

First divergences and minimal repairs:

- RULE_PARSE: pure temporal/grouping syntax and region relationship scope became product filters; explicit field projection disappeared; nonmetric lineage targets were forced through metric readiness. Preserve literal role and intent-specific target; no model prompt expansion.
- TURN_ADMISSION: missing execution slots polluted turn relation; correction and result-sort evidence failed to preserve the right context. Only reference/dependency evidence decides inheritance; new full requests retain their own frame.
- PENDING_ADMISSION: pending presence captured unrelated inputs; admitted candidate choices could still be replaced later. Complete new requests win, expected answer/candidate guards bind only compatible replies, and opaque candidate numbers follow existing option resolution.
- SLOT_MERGE: ADD/REMOVE and regional CLEAR lost values or allowed later inheritance. Keep the existing reducer, fix set mutation and persist a bounded region inheritance barrier. Audit operations now retain ADD/REMOVE/CLEAR semantics.
- DATASET_FOLLOWUP: display LIMIT and explicit ranking count were conflated; completeness was unavailable in persisted provenance. Preserve display order, distinguish local and global operations, retain source_truncated, and fail closed/requery when a global operation lacks a complete source. Projection and explicitly local sorting remain reusable.
- INTENT_ASL_CONTRACT / PENDING_ADMISSION: clarification had no structured reason and could repeat or delegate backend failure to users. Gate and trace cover ordinary, composite and cached final paths. No raw business text is added to long-term traces.

Current business evidence outranks old single test expectations. Four provisional stale classifications were explicitly reverted to UNKNOWN on insufficient evidence. No speculative parser repair is applied to ambiguous forecast/report period policy or PARTIAL_SUCCESS expectations.

Prompt modifications: **0**. Regex changes: **4 bounded families / 5 sites**: additive optional 上 (classifier + admission); anchored display-limit syntax; numeric extreme ranking count; pure time/grouping subject filter. See regex_change_register.md for positive/negative contrasts. Bare name and clear-region guards use finite existing vocabulary/literals, not additional regex.
Cross-service patches: **NO**. Production V2 routing changes: **NO**. Security refactor: **NO**. Full event store, new CAS design, long-term memory and V2 integration remain outside scope.
''')
    md('regex_change_register.md','''# Regex change register

No StructuredIntent prompt edits. Four behavior families, five production expression sites:

| Expression family | Scope / reason | Positive | Negative / contrast | Regression |
| --- | --- | --- | --- | --- |
| Existing additive verb pattern: 加上 → 加上? in classifier and admission | Existing ADD verb syntax accepts the same optional particle; no metric-specific keyword | 再加订单笔数 | 换成订单笔数 must REPLACE; 不要订单笔数 must REMOVE | c03/c05; single_merge_operation_contrasts; slot_audit |
| Anchored display-only prefix + 前N条 | A whole display request selects rows in existing order; metric ranking is excluded | 只看前5条 | 销售额最高5名 is ranking, not limit | c10/c11; dataset_operation_contrasts |
| Explicit extrema count 最高/最低/最大/最小 + N + 名/条/个 | Extract count where the existing extrema branch already chose a field/direction | 销售额最高5名 / 最低2名 | 销售额从低到高 remains full sort | c11; dataset_operation_contrasts |
| Full-match temporal/grouping-only subject using re.escape(existing dimensions) | Reject an invented product only when the entire candidate is recognized structural text and a parsed period exists | 查询2026年7月按地区拆分销售额 | 查询2026年7月医用导管的销售额 retains product | original pytest-05/06; temporal_structural_guard_does_not_discard_product_text |

No regex is added for a single product, hospital or catalog role. Region CLEAR phrases are a finite set of explicit operation commands; explicit subsequent region reopens scope. Bare `<known dimension>名称` is an exact vocabulary match, excludes other text, and asks the list/group operation before resolving a role.
''')
    write_json(OUT/'rollback_manifest.json',dict(baseline_commit=BASE,baseline_evidence_commit='42baf3c',scope='DataAnalysis_Agent only',
        strategy='Review git diff from PHASE0A_BASELINE_COMMIT, then use git revert on Phase0B commits in reverse order when authorized. No reset/clean/stash; no external service rollback needed.',
        phase0a_history_unchanged=True,phase0a_runtime_sync_deviation='See phase0a_evidence_corrections.json; original ignored Git placeholder restored, dev runtime retained',
        final_commit_resolver='git log -1 --format=%H -- docs/phase0b/phase0b_closure_report.md'))
    print(json.dumps(counts,ensure_ascii=True))


if __name__=='__main__':
    main()
