"""Evaluation adapter for the existing sequential RawTurnPlanner runner."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

from app.domain.semantic_scope import AuthorizedSemanticScope
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_contract import VERSION,score_case,summarize,validate_labels
from tools.cutover.harness_fixtures import (FixtureGap,ImplementationGap,pending_fixture,
                                          dataset_fixture,verify_v2_dataset_gap)
from tools.cutover.harness_observation import RuntimeObserver,successful_axes


def error_category(record,calls,events):
    reason=record.get('reason','CONTRACT_OR_HARNESS_FAILURE')
    if any(c.get('transport_error_type')=='MODEL_TIMEOUT' for c in calls):
        kind,stage='MODEL_TIMEOUT','EXTERNAL_BLOCKER'
    elif any(c.get('transport_error_type')=='MODEL_HTTP_ERROR' or
             (isinstance(c.get('status'),int) and c['status']!=200) for c in calls):
        kind,stage='MODEL_HTTP_ERROR','EXTERNAL_BLOCKER'
    elif reason in {'V2_MODEL_OUTPUT_INVALID','V2_MODEL_OUTPUT_INCOMPLETE'}:
        kind='MODEL_SCHEMA_ERROR'
        stage='MENTION_EXTRACTION' if record.get('last_model_stage')=='v2_current_turn' else 'SEMANTIC_QUERY_IR'
    elif reason=='FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED':
        kind,stage='FIXTURE_ERROR','FIXTURE_GAP'
    elif reason in {'MODEL_REQUEST_BUDGET_EXHAUSTED','MODEL_BUDGET_OR_ACCESS_UNAVAILABLE'}:
        kind,stage='MODEL_HTTP_ERROR','EXTERNAL_BLOCKER'
    elif reason.startswith('CATALOG_'):
        kind,stage='CATALOG_ERROR','CATALOG_GAP'
    else:
        kind='MODEL_SEMANTIC_ERROR' if reason.startswith('V2_') else 'RUNTIME_ERROR'
        known={'TaskPatchInput':'TASK_OPERATION','TaskSemanticState':'TASK_REDUCER',
               'SemanticQueryIR':'SEMANTIC_QUERY_IR','LogicalPlan':'DRY_PLAN',
               'CanonicalBinding':'CANONICAL_BINDING','TurnResolutionInput':'TURN_RESOLUTION',
               'CurrentTurnParseInput':'MENTION_BOUNDARY','SemanticResolutionInput':'TURN_RESOLUTION'}
        rejected=[known[e['stage']] for e in events if e['error_type'] and e['stage'] in known]
        stage=rejected[0] if rejected else ('ENTITY_VALUE_GROUNDING' if 'SOURCE_VALUE' in reason else
              'QUERY_SHAPE' if 'QUERY_SHAPE' in reason else 'TURN_RESOLUTION' if 'PENDING' in reason or 'HISTORICAL' in reason else 'SEMANTIC_QUERY_IR')
    return {'type':kind,'stage':stage,'reason_code':reason}


class HarnessRuntime:
    def __init__(self,cases,*,evaluator_commit,evaluator_hash,pending_codes=()):
        self.cases={c['case_id']:c for c in cases};self.observer=RuntimeObserver()
        self.captures=[];self.fixtures={};self.fixture_errors={};self.pending_codes=pending_codes
        self.provenance={'evaluator_version':VERSION,'evaluator_git_commit':evaluator_commit,
                         'evaluator_hash':evaluator_hash,'unit_policy':'CASE_AND_TURN_SEPARATE',
                         'entry':'EXISTING_RAW_TURN_PLANNER','full_production_orchestrator':False}

    def validate_rows(self,rows,catalog):
        if catalog.get('artifact_hash')!=digest({k:v for k,v in catalog.items() if k!='artifact_hash'}):
            raise ValueError('HARNESS_CATALOG_DIGEST_INVALID')
        if len({r['case_id'] for r in rows})!=len(rows):raise ValueError('DUPLICATE_HARNESS_CASE')
        for row in rows:
            validate_labels(self.cases[row['case_id']])
            if row['scope']!=catalog['scope'] or row['catalog_ref']!=catalog['artifact_hash']:
                raise ValueError('HARNESS_CATALOG_SCOPE_MISMATCH')
            if not isinstance(row['history'],list) or not row['current_utterance']:
                raise ValueError('HARNESS_TURN_INPUT_REQUIRED')

    def begin_turn(self,case_id,index,*,state=None,pending=None,plans=()):
        self.observer.begin_turn(case_id,index)
        self.observer.record('PreviousTask',{},state)
        self.observer.record('RecentTasks',{},plans)
        self.observer.record('PendingState',{},pending)
        self.observer.record('DatasetReference',{},state.payload.get('datasets',{}) if state else {})

    def capture(self,value):self.captures.append(deepcopy(value))

    def initialize(self,case,publication):
        self.observer.active=None
        metadata=self.cases[case['case_id']]
        if metadata.get('fixture_gap'):raise FixtureGap(metadata['fixture_gap'])
        if case.get('initial_pending'):
            initial,receipt=pending_fixture(case,publication,candidate_codes=self.pending_codes)
            self.fixtures[case['case_id']]=receipt
            return initial
        if case.get('dataset') is not None:
            # Fill metric metadata from frozen governed column names, not labels.
            reference,receipt=dataset_fixture(case)
            self.fixtures[case['case_id']]=receipt
            verify_v2_dataset_gap(case,publication,reference)
        return None

    def fixture_failure(self,case,exc):
        kind='IMPLEMENTATION_GAP' if isinstance(exc,ImplementationGap) else 'FIXTURE_ERROR'
        reason=str(exc) if isinstance(exc,(FixtureGap,ImplementationGap)) else 'TYPED_FIXTURE_VALIDATION_FAILED'
        self.fixture_errors[case['case_id']]={'type':kind,'stage':'IMPLEMENTATION_GAP' if kind=='IMPLEMENTATION_GAP' else 'FIXTURE_GAP',
                                             'reason_code':reason}
        return reason

    def build_report(self,rows,predictions,catalog,records,calls,mode):
        self.observations=[]
        events={(t['case_id'],t['turn_index']):t['events'] for t in self.observer.turns}
        for row,prediction in zip(rows,predictions):
            case=self.cases[row['case_id']]
            captures=[c for c in self.captures if c['case_id']==case['case_id']]
            current={'case_id':case['case_id'],'axes':{},'scope_evidence':{},'safety':{},'turns':[]}
            prior=[]
            for capture in captures:
                index=capture['turn_index'];turn_events=events.get((case['case_id'],index),[])
                turn={'turn_index':index,'executed':True,'semantic_status':'UNLABELED_HISTORY',
                      'scope_fingerprint':case['scope_fingerprint']}
                axes={};scopes={}
                for event in turn_events:
                    if event['stage']=='AuthorizedSemanticScope' and event['output']:
                        scopes['current']=AuthorizedSemanticScope.model_validate(event['output']).fingerprint()
                    if event['stage']=='ModelInput':
                        context=event['inputs']['context']
                        if 'clock' in context:turn['as_of']=context['clock']
                    if event['stage']=='CanonicalBinding' and event['output']:
                        scopes['binding_context']=AuthorizedSemanticScope.model_validate(event['inputs']['authorized_scope']).fingerprint()
                    if event['stage']=='CurrentTurnParseInput' and event['output']:
                        parse=event['output']
                        axes['mentions']=[{'surface':v['surface'],'start':v['start_char'],'end':v['end_char'],
                            'roles':v['candidate_roles']} for v in parse.get('mentions',[])]
                    if event['stage']=='CandidateSet' and event['output']:
                        axes['candidate_set']=event['output'][1]
                        if scopes.get('current'):scopes['candidate_context']=scopes['current']
                    if event['stage']=='TurnResolutionInput' and event['output']:
                        decision=event['output'];act=decision['dialogue_act']
                        axes['dialogue_act']=act
                        axes['turn_relation']='NEW_TASK' if act=='NEW_TASK' else 'RETURN_TO_TOPIC' if act=='RETURN_TO_TOPIC' else 'ANSWER_PENDING' if act=='ANSWER_CLARIFICATION' else 'FOLLOW_UP'
                result=capture.get('result');outcome=capture['outcome']
                if result:
                    accepted,accepted_scopes=successful_axes(result,before=capture['before'],prior_results=prior,catalog=catalog)
                    axes.update(accepted);scopes.update(accepted_scopes)
                    # Reuse the existing observed range/grain contract, never Gold.
                    axes.update({k:v for k,v in outcome.get('observation',{}).get('axes',{}).items()
                                 if k in {'time_relation','time_grain'}})
                    prior.append(result)
                # Failure in history is the case's primary observed stop; current
                # labels cannot be scored against a different earlier utterance.
                is_current=index==len(case['history'])
                current.update(scope_evidence=scopes)
                if is_current:current['axes']=axes
                if is_current and result:
                    from tools.cutover.harness_contract import equivalent
                    from tools.cutover.harness_safety import state_safety,removal_history_safety
                    current['safety'].update(state_safety(result,capture['before']))
                    current['safety'].update(removal_history_safety(prior))
                    current['safety']['scope_expansion']=current['safety'].get('scope_expansion',False) or any(v!=case['scope_fingerprint'] for v in scopes.values())
                    if result.get('plan'):
                        # A returned plan proves acceptance. Only independently
                        # supplied labels can prove a silent wrong acceptance.
                        # A parse-span discrepancy alone does not prove a wrong
                        # accepted query. Safety needs independently labeled
                        # plan/state behavior, not an intermediate mention axis.
                        from tools.cutover.harness_safety import ACCEPTANCE_AXES
                        checked=[equivalent(v,axes[k],k) for k,v in case['labels'].items()
                                 if k in ACCEPTANCE_AXES and k in axes and case['label_provenance'][k]['label_source']!='UNKNOWN']
                        if checked:current['safety']['wrong_silent_auto_accept']=not all(checked)
                        if 'pending_action' in case['labels'] and 'pending_action' in axes:
                            current['safety']['pending_hijack']=(case['labels']['pending_action']=='DETACHED' and axes['pending_action']=='ANSWER')
                        inherited_axes={'target_task','metric_surfaces','dimension_surfaces','region_values','time_relation','canonical_metrics','canonical_dimensions'}
                        required=set(case['labels'])&inherited_axes
                        if 'wrong_inheritance' in case.get('safety_checks',[]) and required and required<=axes.keys():
                            current['safety']['wrong_inheritance']=any(not equivalent(case['labels'][k],axes[k],k) for k in required)
                if outcome.get('error_type'):
                    selected_calls=[c for c in calls if c['case_id']==case['case_id'] and c['turn_index']==index]
                    current['error']=error_category(outcome,selected_calls,turn_events)
                    turn['execution_error']=current['error']['type']
                current['turns'].append(turn)
                if is_current:
                    score=score_case(case,current)
                    turn['semantic_status']=score['status']
            if case['case_id'] in self.fixture_errors:
                current['error']=self.fixture_errors[case['case_id']]
                current['fixture_contract_attempted']=True
            elif not captures:
                current['execution_status']='NOT_RUN'
            if prediction.get('status')=='FAILED' and not current.get('error'):
                current['error']={'type':'RUNTIME_ERROR','stage':'SEMANTIC_QUERY_IR','reason_code':prediction.get('reason','RUNTIME_FAILED')}
            self.observations.append(current)
        report=summarize([self.cases[r['case_id']] for r in rows],self.observations,
                         provenance={**self.provenance,'mode':mode})
        report['fixture_receipts']=self.fixtures
        report['stage_observations']=dict(Counter(e['stage'] for t in self.observer.turns for e in t['events']))
        return report
