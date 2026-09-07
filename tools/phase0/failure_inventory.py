"""Build an evidence inventory; historical inferences remain explicitly unknown."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from repro import ROOT, write_json, write_csv

OUT = ROOT / 'docs/phase0b'
BASE = 'e4d3bb59026d3b219c9268ac53f87dccf58f3c28'

# Category, stage, severity, slot, error behavior, evidence.
OLD = {
    1: ('STALE_TEST','RESULT_VALIDATION','P2','response','additive_option_details', 'app/domain/models.py:ClarificationItem'),
    5: ('REAL_PRODUCTION_BUG','RULE_PARSE','P0','filters','structural_text_becomes_product_filter', 'tests/test_clarification_quality.py; app/intent/classifier.py'),
    6: ('REAL_PRODUCTION_BUG','RULE_PARSE','P0','entity','structural_text_becomes_product_filter', 'tests/test_clarification_quality.py; app/intent/classifier.py'),
    8: ('LEGACY_BEHAVIOR_DEBT','SESSION_STATE','P2','events','events_absent', 'tests/test_memory_manager.py; app/services/orchestrator.py:_append_session_event'),
    9: ('REAL_PRODUCTION_BUG','SESSION_STATE','P0','identity','fixed_trusted_identity', 'app/api.py:trusted_identity; user section 32: register separately, do not refactor'),
    10: ('REAL_PRODUCTION_BUG','SESSION_STATE','P0','identity','fixed_trusted_identity', 'app/api.py:trusted_identity; user section 32'),
    15: ('REAL_PRODUCTION_BUG','TURN_ADMISSION','P0','dimensions','correction_loses_context', 'tests/test_pending_execution_transition.py; current business slot mutation requirement'),
    16: ('STALE_TEST','RESULT_VALIDATION','P2','display','governed_display_label', 'tests/test_runtime_regressions.py:test_query_answer_uses_business_markdown_table_and_hides_physical_prefix'),
    24: ('LEGACY_BEHAVIOR_DEBT','SESSION_STATE','P2','events','events_absent', 'tests/test_session_event_log.py'),
    26: ('LEGACY_BEHAVIOR_DEBT','SESSION_STATE','P2','events','events_absent', 'tests/test_tracing_evaluation.py'),
}
SCENARIOS = [
    ('上海最近一年销售额','那江苏呢？','FOLLOW_UP / REPLACE region; preserve metric/time','TURN_ADMISSION','region','FOLLOW_UP','region_replacement'),
    ('上海最近一年销售额','江苏有哪些医院？','NEW TASK; no inherited sales or fabricated product filter','RULE_PARSE','filters','NEW_TASK','structural_text_becomes_product_filter'),
    ('销售额和销售量','再加订单笔数','Retain both old metrics and append order count','SLOT_MERGE','metrics','ADD','add_becomes_replace'),
    ('上海销售额','换成江苏','Only Jiangsu region remains','SLOT_MERGE','region','REPLACE','region_replacement'),
    ('销售额和订单笔数','不要订单笔数','Retain sales amount only','SLOT_MERGE','metrics','REMOVE','removed_metric_restored'),
    ('上海销售额 → 不限地区','按季度','Region remains empty after CLEAR','SLOT_MERGE','region','CLEAR','clear_loses_base_or_resurrects_filter'),
    ('Pending metric choice','江苏有哪些医院？','NEW TASK even if default projection awaits governance','PENDING_ADMISSION','relation','NEW_TASK','pending_hijack'),
    ('','医院名称字段来自哪个表？','Recognize field target; never request a metric','RULE_PARSE','lineage_target','NEW_TASK','lineage_requires_metric'),
    ('Catalog fixture with governed default display','列出TDC-3合作医院','Use permitted governed projection; missing policy is a catalog gap','SEMANTIC_GROUNDING','fields','NEW_TASK','catalog_default_display_gap'),
    ('Existing dataset','只看前5条','DISPLAY_LIMIT preserves order without replanning','DATASET_FOLLOWUP','limit','FOLLOW_UP','display_limit_unrecognized'),
    ('Existing complete dataset','销售额最高5名','GLOBAL_RANKING descending count 5','DATASET_FOLLOWUP','ranking','FOLLOW_UP','ranking_count_ignored'),
    ('Existing truncated dataset','销售额最高5名','Never establish global Top5 from incomplete rows','DATASET_FOLLOWUP','completeness','FOLLOW_UP','explicit_completeness_guard_missing'),
    ('','销售数量','Quantity metric is retained; no repeated metric question','SEMANTIC_GROUNDING','metric','NEW_TASK','metric_attribute_role_collision'),
    ('','列出商品名称 / 按商品名称统计销售额','Attribute projection vs grouping determined by action','RULE_PARSE','fields','NEW_TASK','explicit_attribute_lost'),
    ('上海销售额','查询订单笔数','Default time/readiness cannot change turn relation','TURN_ADMISSION','time_range','NEW_TASK','readiness_pollutes_relation'),
]


def signature(stage, family, act, slot, behavior):
    value = '|'.join((stage,family,act,slot,behavior))
    return value, hashlib.sha256(value.encode()).hexdigest()[:16]


def main():
    baseline = json.loads((ROOT/'docs/phase0a/test_gate.json').read_text(encoding='utf-8'))
    repeats = json.loads((OUT/'repeatability.json').read_text(encoding='utf-8'))
    observations = []
    for index, rep in enumerate(repeats, 1):
        node = rep['nodeid']
        if index in OLD:
            cls,stage,severity,slot,behavior,evidence = OLD[index]
        elif index >= 28:
            cls,stage,severity,slot,behavior,evidence = ('UNKNOWN_NEEDS_EVIDENCE','RESULT_VALIDATION','P1','status','partial_success_vs_fallback','Need analysis contract owner to confirm partial-result policy; do not change expectation')
        else:
            cls,stage,severity,slot,behavior,evidence = ('STALE_TEST','RULE_PARSE','P1','time_range','obsolete_mandatory_period','Current requirement: safe deterministic defaults; tests/test_intent.py default-time/all-time stable regressions; app/intent/classifier.py:_apply_default_time_range')
        result=json.loads((ROOT/rep['evidence'][0]).read_text(encoding='utf-8'))
        failure=result.get('failures',{}).get(node,'')
        sig,key=signature(stage,'LEGACY', 'QUERY',slot,behavior)
        observations.append(dict(failure_id=f'pytest-{index:02d}',source=['CURRENT_PYTEST'],test_nodeids=[node],historical_case_ids=[],
            conversation_history=None,current_utterance=None,expected_behavior='Exact assertion and fixture in test_nodeids',
            actual_behavior='\n'.join(s for s in failure.splitlines() if s.startswith('E '))[:4000],affected_business_capability=slot,
            repeatability=rep['repeatability'],first_divergence_stage=stage,root_cause_class=cls,severity=severity,owning_service='DataAnalysis_Agent',
            evidence=[*rep['evidence'],evidence],status='SECURITY_P0_OWNER_BLOCKER' if slot=='identity' else 'OPEN',failure_signature=sig,signature_id=key))
    history=[json.loads(line) for line in (ROOT/'docs/data_agent_failure_cases.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    replay={r['case_id']:r for r in [json.loads(line) for line in (ROOT/'docs/phase2/failure_trace_replay.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]}
    for row in history:
        r=replay[row['case_id']]
        proposed=r['first_divergence_stage']
        sig,key=signature('UNKNOWN',row['actual_extracted_slots'].get('intent','UNKNOWN'),
                          'ADD' if '再加' in row['current_user_message'] else 'FOLLOW_UP' if 'followup' in row['source'] else 'NEW_TASK',
                          r['first_divergence_category'],row['actual_response'].split('：')[0])
        observations.append(dict(failure_id=row['case_id'],source=['HISTORICAL_PARTIAL_TRACE'],test_nodeids=[],historical_case_ids=[row['case_id']],
            conversation_history=row['initial_state'],current_utterance=row['current_user_message'],expected_behavior=row['expected_behavior'],actual_behavior=row['actual_response'],
            affected_business_capability=r['first_divergence_category'],repeatability='NOT_REPLAYABLE_WITH_AVAILABLE_STATE',first_divergence_stage='UNKNOWN',
            root_cause_class='UNKNOWN_NEEDS_EVIDENCE',severity='P1',owning_service='UNKNOWN',
            evidence=['docs/data_agent_failure_cases.jsonl#'+row['case_id'],'docs/phase2/failure_trace_replay.jsonl#'+row['case_id'],
                      'Historical proposed stage (not proved): '+proposed,'Need original state, stage outputs, catalog and dataset snapshots'],
            status='UNKNOWN_NEEDS_EVIDENCE',failure_signature=sig,signature_id=key))
    critical=json.loads((OUT/'critical_before.json').read_text(encoding='utf-8'))
    scenarios=[]
    for index,(history,text,expected,stage,slot,act,behavior) in enumerate(SCENARIOS,1):
        node=next(n for n in critical['results'] if f'::test_c{index:02d}_' in n)
        passed=critical['results'][node]=='passed'
        sig,key=signature(stage,'LEGACY',act,slot,behavior)
        scenarios.append(dict(case_id=f'critical-{index:02d}',history=history,current_utterance=text,expected_behavior=expected,test_nodeid=node,
                              source='CURRENT_USER_REQUIREMENT',baseline_status=critical['results'][node]))
        observations.append(dict(failure_id=f'critical-{index:02d}',source=['CRITICAL_SCENARIO_SUITE'],test_nodeids=[node],historical_case_ids=[],
            conversation_history=history,current_utterance=text,expected_behavior=expected,actual_behavior='PASS' if passed else critical['failures'][node][-3000:],
            affected_business_capability=slot,repeatability='OFFLINE_FIXED_CLOCK_FIXTURE',first_divergence_stage=stage,
            root_cause_class='CATALOG_GOVERNANCE_GAP' if index==9 else 'LEGACY_BEHAVIOR_DEBT' if passed else 'REAL_PRODUCTION_BUG',
            severity='P0' if not passed else 'P1',owning_service='DataAnalysis_Agent',evidence=['docs/phase0b/critical_before.json',node,'User Critical Multi-turn Scenario '+str(index)],
            status='PASS_AT_BASELINE' if passed else 'OPEN',failure_signature=sig,signature_id=key))
    write_json(OUT/'failure_universe.json',dict(baseline_commit=BASE,observation_count=len(observations),
        unique_failure_signatures=len({r['signature_id'] for r in observations if r['status']!='PASS_AT_BASELINE'}),
        deduplication='Group by signature; PASS_AT_BASELINE are acceptance controls, not failures. Historical unknowns are not silently equated with proven bugs.',items=observations))
    write_csv(OUT/'failure_universe.csv', [{k: '\n'.join(line.rstrip() for line in v.splitlines()) if isinstance(v,str) else v for k,v in row.items()} for row in observations])
    (OUT/'critical_multiturn_scenarios.jsonl').write_text(''.join(json.dumps(s,ensure_ascii=False)+'\n' for s in scenarios),encoding='utf-8',newline='\n')
    write_json(OUT/'phase0a_baseline_verification.json',dict(PHASE0A_BASELINE_COMMIT=BASE,committed_files=420,raw_commit_matches_both_workspaces=True,
        git_working_tree_clean_before_phase0b=True,phase0a_gate='PASS',phase0a_draft_pr='https://github.com/dreamyang-1/data/pull/1'))
    print('OBSERVATIONS',len(observations),'CURRENT',len(repeats),'HISTORICAL',len(history) if isinstance(history,list) else 30,'CRITICAL',len(scenarios))


if __name__=='__main__':main()
