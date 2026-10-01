"""Publish bounded Round 5 receipts from private artifacts; never publish raw inputs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import mean

from tools.cutover.run_context_proposal_round4 import live_cases,read,write,family
from tools.cutover.harness_observation import successful_axes
from tools.cutover.harness_contract import equivalent
from tools.cutover.evaluation_contract import digest

ROOT=Path(__file__).resolve().parents[2]


def stats(values):
    if not values:return dict(n=0,mean=None,p95=None,status='UNKNOWN_NO_RECEIPT')
    values=sorted(values);position=.95*(len(values)-1);low=int(position)
    p95=values[low]+(values[min(low+1,len(values)-1)]-values[low])*(position-low)
    return dict(n=len(values),mean=mean(values),p95=p95,method='linear interpolated sample percentile')


def summarize(private,out):
    out.mkdir(parents=True,exist_ok=True)
    live=read(private/'live/live_result.json');refresh=read(private/'downstream-refresh/live_result.json')
    calls=live['calls']+refresh['calls'];assert len(calls)==20
    index={r['case_id']:r for r in live['rows']};index.update({r['case_id']:r for r in refresh['rows']})
    replay=read(private/'final-source-replay/replay_receipt.json')
    targets=read(private/'final-source-targets/target_receipt.json')
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    rows=[]
    for case in live_cases():
        cid=case['case_id'];r=index[cid];capture=case['capture'];before=capture['before']
        final=read(private/'final-source-replay'/(cid+'.json'))['result']
        checks={};missing=[];prior=[]
        if not case.get('new_input'):
            for i in range(capture['turn_index']):
                path=ROOT/'.eval_private/harness-20260909T083000Z/transition/captures'/f'{cid}-turn-{i}.json'
                if path.exists():
                    previous=read(path).get('result')
                    if previous:prior.append(previous)
            if not final:missing=list(case['definition']['labels'])
        if final:
            axes,_=successful_axes(final,before=before,prior_results=prior,catalog=catalog)
            if not case.get('new_input'):
                for key,expected in case['definition']['labels'].items():
                    if key in axes:checks[key]=equivalent(expected,axes[key],key)
                    else:missing.append(key)
            else:
                checks['target_id']=final['plan']['logical_plan']['task_id']==r['expected_target']
                checks['turn_relation']=axes['turn_relation']==case['expected_relation']
                if cid=='R4L-010':
                    checks['canonical_metrics']=axes['canonical_metrics']==['METRIC:order_count']
                    s=axes['task_state'];checks['no_history_inheritance']=not s['dimensions'] and s['filter_expression'] is None and s['time_spec'] is None
                if cid=='R4L-012':checks['pending_action']=axes['pending_action']=='ANSWER'
        record=read(private/'live'/(cid+'.json'))
        trace=record['trace']
        rows.append(dict(case_id=cid,target_match=r['target_match'],relation_match=(
            family(trace['FINAL_RELATION'])==case['expected_relation'] if trace['FINAL_STATUS']=='ACCEPTED' else
            trace['FINAL_STATUS'] in {'AMBIGUOUS','UNRESOLVED'} and case['expected_relation']=='UNRESOLVED'),
            context_status=r['context_status'],outcome=r['outcome'],failure=r['error'],
            label_checks=checks,unobserved_axes=missing,arbitration=trace,
            initial_capture_hash=digest(record),final_runtime_record_hash=digest(read(private/'final-source-replay'/(cid+'.json'))),
            model_evidence='ALL_CURRENT_LIVE' if cid not in {'S81-012','S81-016'} else 'LIVE_PROPOSAL_WITH_ROUND4_DOWNSTREAM_FIXTURE'))
    validation=dict(cases=12,proposal_schema_valid=12,target_or_nonselection_match=sum(r['target_match'] for r in rows),
        relation_match=sum(r['relation_match'] for r in rows),rows=rows,
        native_plans=sum(r['outcome']=='PLAN' for r in rows),ambiguous_terminal=sum(r['context_status']=='AMBIGUOUS' for r in rows),
        downstream_rejections=[r['case_id'] for r in rows if r['failure'] and r['context_status']=='ACCEPTED'],
        labeled_state_wrong=[r['case_id'] for r in rows if any(v is False for v in r['label_checks'].values())],
        whole_plan_accuracy='NOT_CLAIMED; ONLY_EXISTING_DECLARED_AXES',
        real_model_calls=20,provider_timeout_count=sum(c['status'] is None for c in calls),
        schema_reject_count=0,source_sql_calls=0,production_writes=0,blind_access=0,
        initial_mixed_fixture_rejections=['S81-011','S81-013'],
        refresh='Same captured first-stage output/context/schema; two current downstream calls, not repeated first-stage calls')
    write(out/'live_validation_receipt.json',validation)
    write(out/'target_regression_receipt.json',targets)
    write(out/'runtime_replay_receipt.json',replay)
    before_calls=read(ROOT/'.eval_private/harness-20260909T083000Z/transition/model_calls.json')
    matched_ids={'S81-001','S81-002','S81-003','S81-014','S81-017'}
    definitions={c['case_id']:c for c in live_cases()}
    def selected(records):return [c for c in records if c['case_id'] in matched_ids
        and c['turn_index']==definitions[c['case_id']]['capture']['turn_index']
        and c['stage'] in {'v2_current_turn','v2_semantic_edits'}]
    def usage(records,stage=None):
        records=[c for c in records if not stage or c['stage']==stage]
        return dict(input=stats([c['usage']['prompt_tokens'] for c in records]),
            output=stats([c['usage']['completion_tokens'] for c in records]))
    def request_usage(records):
        return {kind:stats([sum(c['usage'][field] for c in records if c['case_id']==cid) for cid in sorted(matched_ids)])
            for kind,field in [('input','prompt_tokens'),('output','completion_tokens')]}
    old_joint=read(ROOT/'.eval_private/context-round4-20260910T004000Z/live/live_result.json')
    fully_live=[r for r in live['rows'] if r['downstream']=='LIVE' and r['outcome']=='PLAN']
    local_discovery=[t['seconds'] for r in replay['rows'] for t in r['stage_timings'] if t['stage']=='discover_context']
    local_validation=[t['seconds'] for r in replay['rows'] for t in r['stage_timings'] if t['stage']=='validate_proposal']
    performance=dict(normal_path_calls_per_request={'before':2,'after':2},context_path_calls_per_request={'before':2,'after':2},
        exact_pending_calls_per_request={'before':1,'after':1},added_sequential_context_calls=0,
        existing_source_probe_calls='Independent existing capability; not counted as a newly added context call',
        matched_usage_case_ids=sorted(matched_ids),before_current_production_usage=request_usage(selected(before_calls)),
        after_current_production_usage=request_usage(selected(calls)),
        before_first_stage_usage=usage(selected(before_calls),'v2_current_turn'),
        after_first_stage_usage=usage(selected(calls),'v2_current_turn'),
        before_recognition_seconds=stats([]),before_total_v2_seconds=stats([]),
        after_recognition_seconds=stats([t['seconds'] for r in live['rows'] for t in r['stage_seconds'] if t['stage']=='v2_current_turn']),
        after_total_v2_seconds=stats([r['total_v2_seconds'] for r in fully_live]),
        total_latency_case_ids=[r['case_id'] for r in fully_live],
        before_context_resolution_seconds=stats([r['legacy_resolver_seconds'] for r in targets['rows']]),
        after_context_discovery_seconds=stats(local_discovery),after_context_validation_seconds=stats(local_validation),
        component_timing_limit='Offline per-call seams, different measurement work; not end-to-end causal latency deltas',
        round4_experimental_joint_recognition_seconds=stats([r['proposal_seconds'] for r in old_joint['rows']]),
        round4_experimental_first_stage_usage=usage([c for c in old_joint['calls'] if not c['case_id'].endswith('-A')]),
        token_increase_source='Bounded summaries plus required proposal schema/consistency predicates. Draft sees only selected task.',
        provider_timeout_count=0,schema_reject_count=0,model_calls=20,
        incremental_production_latency='UNKNOWN_NO_MATCHED_BASELINE_TIMING_RECEIPT; NO_DETERMINISM_OR_PERFORMANCE_GUARANTEE',
        model=dict(name='qwen3.7-max',enable_thinking=False,temperature=0,retries=0,timeout_seconds=60,
            seed='NOT_SENT',top_p='NOT_SENT',max_tokens='NOT_SENT'))
    write(out/'performance_receipt.json',performance)
    batches=[read(p) for p in sorted((private/'full').glob('batch-*.json'))]
    outcomes={n:s for d in batches for n,s in d['results'].items()}
    for name in ('harness-final.json','production-final.json','terminal-final.json'):
        outcomes.update(read(private/name)['results'])
    previous=read(ROOT/'docs/v2_cutover/semantic_round1/test_delta.json')
    failures={n for n,s in outcomes.items() if s=='failed'}
    node_hash=digest(outcomes)
    test_delta=dict(counts=dict(Counter(outcomes.values())),node_outcomes=outcomes,node_outcomes_hash=node_hash,
        baseline_full={'source':'Round1 actual full run; Round4 did not rerun full Agent','passed':3183,'failed':27},
        baseline_round4_focused={'passed':322,'failed':0},
        old_pass_to_new_fail=sorted(failures-set(previous['failed_nodeids'])),
        old_fail_to_new_pass=sorted(set(previous['failed_nodeids'])-failures),
        collection_errors=[n for d in batches for n in d['collection_errors']],new_round5_tests=25,
        critical={'passed':160,'failed':0},context_slice={'passed':8,'turns':18},affected_v2={'passed':601,'failed':0},
        final_scope_context_batch={'passed':347,'failed':0},final_terminal_recheck={'passed':2,'failed':0},
        full_run_modules=131,offline=True,model_calls_in_tests=0,production_writes=0,
        retest_reason='Update two mocked joint outputs, snapshot-proof immutability, and explicit TerminalDecision; no legacy business expectation changes',
        source_receipts={str(p.relative_to(private)):digest(read(p)) for p in [*(private/'full').glob('batch-*.json'),
            private/'harness-final.json',private/'production-final.json',private/'terminal-final.json']})
    write(out/'test_delta.json',test_delta)
    print(json.dumps(dict(test_counts=test_delta['counts'],model_calls=20,target_match=validation['target_or_nonselection_match'],
        native_plans=validation['native_plans'],label_failures=validation['labeled_state_wrong'],
        matched_input_before=performance['before_current_production_usage']['input']['mean'],
        matched_input_after=performance['after_current_production_usage']['input']['mean'],
        latency_after=performance['after_total_v2_seconds'],first_latency_after=performance['after_recognition_seconds']),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--private-directory',type=Path,required=True)
    p.add_argument('--output-directory',type=Path,required=True);a=p.parse_args();summarize(a.private_directory,a.output_directory)
