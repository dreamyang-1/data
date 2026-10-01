"""Score recorded public relation diagnostics without any new model calls.

Schema/transport rejections never earn semantic credit for a null target.
Native state labels are scored only where the existing frozen evaluator has an
observation; unavailable axes and complete-plan truth remain unavailable.
"""
from copy import deepcopy
import json
from pathlib import Path

from tools.cutover.run_context_proposal_round4 import live_cases,read,family
from tools.cutover.harness_observation import successful_axes
from tools.cutover.harness_contract import equivalent
from tools.cutover.evaluation_contract import digest
from app.semantic_v2.pipeline import CurrentTurnSemanticParse,CurrentTurnParser,TurnResolver
from app.semantic_v2.recognition_repairs import repair_model_parse
from app.semantic_v2.state_machine import ConversationState
from app.semantic_v2.models import SemanticResolutionContract
from app.semantic_v2.slot_reducer import TaskPatch

ROOT=Path(__file__).resolve().parents[2]


def ratio(n,N):return {'n':n,'N':N}


def metrics(rows):
    summaries={}
    for arm in ('A_CURRENT_HYBRID','B_MODEL_PROPOSAL_PLUS_HARD_CONSTRAINTS','C_RULE_ONLY_DIAGNOSTIC'):
        valid=[r for r in rows if not r['arms'][arm].get('error')]
        exact=[r for r in valid if r['expected_target'] is not None]
        follows=[r for r in rows if r['expected_relation']=='FOLLOW_UP']
        historical=[r for r in rows if r['expected_relation']=='RETURN_TO_TOPIC']
        predicted_new=[r for r in valid if r['arms'][arm]['relation']=='NEW_TASK']
        summaries[arm]=dict(attempts=len(rows),valid_decision_outputs=len(valid),
            schema_or_runtime_rejected=len(rows)-len(valid),
            target_task_exact_match=ratio(sum(r['arms'][arm]['target']==r['expected_target'] for r in exact),len(exact)),
            relation_semantic_match=ratio(sum(r['arms'][arm]['relation']==r['expected_relation'] for r in valid),len(valid)),
            follow_up_recall=ratio(sum(not r['arms'][arm].get('error') and r['arms'][arm]['relation']=='FOLLOW_UP' for r in follows),len(follows)),
            new_task_precision=ratio(sum(r['expected_relation']=='NEW_TASK' for r in predicted_new),len(predicted_new)),
            historical_return=ratio(sum(not r['arms'][arm].get('error') and r['arms'][arm]['relation']=='RETURN_TO_TOPIC'
                and r['arms'][arm]['target']==r['expected_target'] for r in historical),len(historical)),
            wrong_inheritance={'n':None,'N':0,'status':'NO_STATE_IN_RELATION_ONLY_ARM'},
            unnecessary_clarification={'n':None,'N':0,'status':'NO_USER_QUESTION_OUTPUT_IN_RELATION_ONLY_ARM'})
    return summaries


def summarize(private):
    live=read(private/'live/live_result.json');rows=deepcopy(live['rows']);corrections=[]
    definitions={c['case_id']:c for c in live_cases()}
    # Fix only the observer wiring for A's new independent-request contrast.
    # Same recorded output; native repair always precedes span validation.
    r=next(r for r in rows if r['case_id']=='R4L-010');case=definitions[r['case_id']]
    record=read(private/'live/R4L-010-A-capture.json');capture=case['capture']
    parsed=CurrentTurnSemanticParse.model_validate(record['outputs'][0]['output'])
    parsed,repairs=repair_model_parse(parsed,text=capture['question'],turn_id='turn-2')
    native=CurrentTurnParser.parse(text=capture['question'],turn_id='turn-2',text_ref='turn-2',parsed=parsed)
    decision=TurnResolver.resolve(native,state=ConversationState.model_validate(capture['before']['state']['payload']),
        task_patch=TaskPatch(base_task_version=0),semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))
    corrected=dict(relation=family(decision.dialogue_act),target=decision.target_task_id,source='SAME_RECORDED_OUTPUT_NATIVE_REPAIR_THEN_RESOLVER')
    corrections.append(dict(case_id=r['case_id'],arm='A_CURRENT_HYBRID',before=r['arms']['A_CURRENT_HYBRID'],after=corrected,
        repairs=repairs,record_hash=digest(record),new_model_calls=0,reason='DIAGNOSTIC_ADAPTER_OMITTED_EXISTING_REPAIR'))
    r['arms']['A_CURRENT_HYBRID']=corrected
    native=read(private/'native-final/native_result.json');reviews=[]
    catalog=read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    for item in native['rows']:
        cid=item['case_id'];case=definitions[cid];record_path=private/'native-final'/(cid+'.json')
        if not record_path.exists():
            reviews.append(dict(case_id=cid,status=item['status'],label_checks={},unobserved_axes=['ALL_NATIVE_STATE_AXES']));continue
        record=read(record_path);result=record['result'];capture=case['capture'];before=capture['before']
        public=not case.get('new_input');prior=[]
        if public:
            for i in range(capture['turn_index']):
                p=ROOT/'.eval_private/harness-20260909T083000Z/transition/captures'/f'{cid}-turn-{i}.json'
                if p.is_file():
                    value=read(p).get('result')
                    if value:prior.append(value)
        checks={};missing=list(case['definition']['labels']) if public and not result else [];state_check=None;unnecessary=None
        if result:
            axes,_=successful_axes(result,before=before,prior_results=prior,catalog=catalog)
            if public:
                labels=case['definition']['labels']
                for k,v in labels.items():
                    if k in axes:checks[k]=equivalent(v,axes[k],k)
                    else:missing.append(k)
            else:
                expected=next(r for r in rows if r['case_id']==cid)
                checks['target_id']=result['plan']['logical_plan']['task_id']==expected['expected_target']
                checks['turn_relation']=axes['turn_relation']==expected['expected_relation']
                if cid=='R4L-010':
                    checks['canonical_metrics']=axes['canonical_metrics']==['METRIC:order_count']
                    s=axes['task_state'];checks['no_history_inheritance']=not s['dimensions'] and s['filter_expression'] is None and s['time_spec'] is None
                if cid=='R4L-012':checks['pending_action']=axes['pending_action']=='ANSWER'
            relevant={'metric_surfaces','dimension_surfaces','canonical_metrics','canonical_dimensions','region_values',
                      'metric_operations','dimension_operations','no_history_inheritance','pending_action'}
            checked=[v for k,v in checks.items() if k in relevant]
            state_check=all(checked) if checked else None
            unnecessary=result['plan'] is None
        old_status=('ACCEPTED' if capture.get('result') else 'REJECTED') if public else 'NO_PRIOR_RUNTIME_BASELINE'
        reviews.append(dict(case_id=cid,status=item['status'],baseline_status=old_status,
            failure=item.get('failure'),label_checks=checks,unobserved_axes=missing,
            labeled_state_axes_correct=state_check,unnecessary_clarification=unnecessary,
            record_hash=digest(record),native_state_written_only_to_returned_artifact=True))
    seconds=sorted(r['proposal_seconds'] for r in rows)
    summary=dict(experiment='ROUND4_CONTEXT_PROPOSAL_DIAGNOSTIC',formal_gold_rescored=False,
        original_live_schema_version='context-proposal-experiment-v1',current_schema_export_version='context-proposal-experiment-v1.1',
        current_schema_export_live_tested=False,
        corpus='PUBLIC_DEV_TRANSITION_9_PLUS_NEW_PUBLIC_CONTRAST_3',case_count=12,
        arms=metrics(rows),rows=rows,observer_corrections=corrections,
        latency=dict(unit='SECONDS_FIRST_PROPOSAL_CALL',count=len(seconds),minimum=min(seconds),
            median=(seconds[5]+seconds[6])/2,maximum=max(seconds),
            incremental_latency='UNKNOWN_NO_SAME_CONDITIONS_PAIRED_BASELINE; TOKENIZER_NOT_PINNED'),
        live_first_stage_calls=len(live['calls']),live_downstream_calls=len(native['calls']),
        real_model_calls=len(live['calls'])+len(native['calls']),
        native_reviews=reviews,
        native_status_counts={s:sum(r['status']==s for r in reviews) for s in {r['status'] for r in reviews}},
        native_old_accept_to_reject=[r['case_id'] for r in reviews if r.get('baseline_status')=='ACCEPTED' and r['status']!='ACCEPTED_DIAGNOSTIC_ONLY'],
        native_labeled_state_wrong=[r['case_id'] for r in reviews if r.get('labeled_state_axes_correct') is False],
        source_sql_calls=0,production_state_writes=0,private_remaining_access=0,blind_access=0,
        comparison_limits=['A_9_FROZEN_NOT_CONTEMPORANEOUS', 'NEW_CONTRAST_A_NO_SECOND_STAGE_HISTORICAL_SELECTOR',
            'C_RELATION_ONLY_NOT_WHOLE_V1', 'B_REUSES_NATIVE_STATE_BUT_CHANGED_PARSE_AND_LIVE_DRAFT',
            'PER_TURN_FIXED_PRECONDITIONS_NOT_COUNTERFACTUAL_FULL_ROLLOUT',
            'ONLY_ONE_OF_ORIGINAL_15_IN_LIVE_B', 'SCHEMA_REJECTION_NOT_CORRECT_AMBIGUITY',
            'INCOMPLETE_FULL_SEMANTIC_LABEL_AXES'])
    return summary
