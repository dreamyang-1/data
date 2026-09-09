from copy import deepcopy

import pytest

from tools.cutover.harness_contract import FULL_PLAN_AXES,common_set,score_case,summarize,equivalent


def specimen():
    # Independently specified evaluator contract fixture, never copied from a
    # V1/V2/model result and never counted as a business FULL_PLAN_GOLD case.
    labels={'scope':{'semantic_model_id':81,'business_domain_ids':[205]},
        'target_task':'PREVIOUS','dialogue_act':'ADD','canonical_metrics':['METRIC:amount'],
        'canonical_dimensions':['DIMENSION:city'],'entity':{'canonical_id':'entity:hospital'},
        'bindings':[{'canonical_id':'metric:amount','business_domain_ids':[205]}],
        'time':{'start':'2025-01-01','end_exclusive':'2026-01-01'},
        'task_patch':{'clears':['region'],'adds':['METRIC:amount']},
        'filters':None,'task_state':{'metrics':['METRIC:amount'],'region':None},
        'query_shape':'GROUPED_AGGREGATE','semantic_query_ir':{'measure':'metric:amount'},
        'dry_plan':{'projection':['metric:amount']},'ranking':{'direction':'DESC','limit':5},
        'dataset_truncation':{'truncated':True,'global_rank_allowed':False}}
    case={'case_id':'contract-fixture','scope_fingerprint':'scope205','labels':labels,
        'label_provenance':{a:{'label_source':'DETERMINISTIC_RULE','evidence':['INDEPENDENT_EVALUATOR_CONTRACT_FIXTURE']} for a in labels},
        'label_coverage':'FULL_PLAN','turn_count':3,'safety_checks':[]}
    observation={'case_id':case['case_id'],'axes':deepcopy(labels),'scope_evidence':{'current':'scope205'},
        'turns':[{'executed':True,'semantic_status':s} for s in ('PASS','PASS','FAIL')]}
    return case,observation


MUTATIONS=[
    ('target_task','NEW'),('dialogue_act','REPLACE'),('canonical_metrics',['METRIC:wrong']),
    ('canonical_dimensions',['DIMENSION:wrong']),('entity',{'canonical_id':'entity:wrong'}),
    ('bindings',[{'canonical_id':'invented','business_domain_ids':[205]}]),
    ('time',{'start':'2024-01-01','end_exclusive':'2026-01-01'}),
    ('task_patch',{'removes':['region'],'replacements':['METRIC:amount']}),
    ('scope',{'semantic_model_id':81,'business_domain_ids':[]}),
    ('task_state',{'metrics':['METRIC:wrong'],'region':'revived'}),
    ('query_shape','SCALAR_AGGREGATE'),('semantic_query_ir',{'measure':'metric:wrong'}),
    ('ranking',{'direction':'ASC','limit':5}),
    ('dataset_truncation',{'truncated':True,'global_rank_allowed':True}),
]


@pytest.mark.parametrize('axis,wrong',MUTATIONS)
def test_mutations_cannot_pass(axis,wrong):
    case,actual=specimen();actual['axes'][axis]=wrong
    result=score_case(case,actual)
    assert result['status']=='FAIL' and result['axis_results'][axis]=='FAIL'
    assert result['first_divergence_stage'] and isinstance(result['downstream_effects'],list)


@pytest.mark.parametrize('axis',['target_task','bindings','task_state','semantic_query_ir','dry_plan'])
def test_missing_axis_is_not_a_pass(axis):
    case,actual=specimen();actual['axes'].pop(axis)
    assert score_case(case,actual)['status']=='BLOCKED'


@pytest.mark.parametrize('axis',['canonical_metrics','canonical_dimensions','bindings','candidate_set'])
def test_semantically_unordered_candidates_and_collections_do_not_false_fail(axis):
    a=[{'canonical_id':'one'},{'canonical_id':'two'}]
    assert equivalent(a,list(reversed(a)),axis)
    assert not equivalent(a,a+[a[0]],axis)  # Do not erase duplicate candidates.


def test_mapping_order_equivalence_and_order_sensitive_plan_counterexample():
    assert equivalent({'id':'x','scope':205},{'scope':205,'id':'x'},'entity')
    assert not equivalent({'projection':['a','b']},{'projection':['b','a']},'dry_plan')
    assert not equivalent(True,1,'target_task')


def test_partial_mention_labels_allow_governed_role_alternatives_and_order():
    expected=[{'surface':'object','start':0,'end':6,'roles':['SUBJECT_ENTITY','TARGET_ENTITY']}]
    actual=[{'end':6,'start':0,'surface':'object','roles':['TARGET_ENTITY']}]
    assert equivalent(expected,actual,'mentions')
    actual[0]['roles']=['MEASURE']
    assert not equivalent(expected,actual,'mentions')


def test_scope_safety_cannot_be_hidden_by_unlabeled_axis():
    case,actual=specimen();case['labels'].pop('scope');case['label_provenance'].pop('scope');case['label_coverage']='TURN_ONLY'
    actual['scope_evidence']['stored_state']='foreign'
    result=score_case(case,actual)
    assert result['status']=='FAIL' and result['safety_violations']==['scope_expansion']


@pytest.mark.parametrize('source',['CURRENT_V1_OUTPUT','CURRENT_V2_OUTPUT','CURRENT_LLM_OUTPUT'])
def test_outputs_cannot_become_truth(source):
    case,actual=specimen();case['label_provenance']['target_task']['label_source']=source
    with pytest.raises(ValueError,match='PROVENANCE'):score_case(case,actual)


def test_unknown_truth_and_partial_plan_denominators_are_explicit():
    case,actual=specimen();case['label_provenance']['target_task']['label_source']='UNKNOWN'
    assert score_case(case,actual)['status']=='BLOCKED'
    case['labels'].pop('dry_plan');case['label_provenance'].pop('dry_plan')
    with pytest.raises(ValueError,match='PARTIAL_LABELS'):score_case(case,actual)
    case['label_coverage']='TURN_ONLY'
    report=summarize([case],[actual],provenance={})
    assert report['FULL_PLAN_GOLD_COUNT']==report['WHOLE_PLAN_PASS']==report['WHOLE_PLAN_FAIL']==0


def test_turn_and_case_denominators_do_not_mix():
    case,actual=specimen();actual['axes']['target_task']='WRONG'
    report=summarize([case],[actual],provenance={})
    assert report['TURN_LEVEL']['semantic_match']['n']==2
    assert report['TURN_LEVEL']['semantic_match']['N']==3
    assert report['CASE_LEVEL']['exact_match']['n']==0
    assert report['CASE_LEVEL']['exact_match']['N']==1


@pytest.mark.parametrize('kind',['MODEL_HTTP_ERROR','MODEL_TIMEOUT','MODEL_SCHEMA_ERROR','RUNTIME_ERROR','EVALUATOR_ERROR','FIXTURE_ERROR','CATALOG_ERROR'])
def test_request_errors_keep_their_own_category(kind):
    case,actual=specimen();actual['error']={'type':kind,'stage':'EXTERNAL_BLOCKER','reason_code':'BOUNDED'}
    result=score_case(case,actual)
    assert result['status']!='PASS' and result['error']['type']==kind


def test_http_failure_is_not_added_to_semantic_error_denominator():
    case,actual=specimen();actual['error']={'type':'MODEL_HTTP_ERROR','stage':'EXTERNAL_BLOCKER','reason_code':'HTTP_ERROR'}
    report=summarize([case],[actual],provenance={})
    assert report['CASE_LEVEL']['exact_match']['N']==0
    assert report['CASE_LEVEL']['excluded_not_run_or_blocked']==1


def test_common_set_excludes_missing_axes_and_different_contracts():
    case,actual=specimen();a=summarize([case],[actual],provenance={})
    other=deepcopy(case);other['case_id']='other';obs=deepcopy(actual);obs['case_id']='other'
    b=summarize([other],[obs],provenance={})
    common=common_set(a,b)
    assert common['COMMON_EVALUABLE_SET']==[] and common['V1']['N']==common['V2']['N']==0


def test_matching_mention_boundary_with_wrong_role_is_role_divergence():
    case,actual=specimen();mention={'surface':'x','start':0,'end':1,'roles':['MEASURE']}
    case['labels']['mentions']=[mention]
    case['label_provenance']['mentions']={'label_source':'DETERMINISTIC_RULE','evidence':['ROLE_CONTRACT']}
    actual['axes']['mentions']=[{**mention,'roles':['ATTRIBUTE']}]
    assert score_case(case,actual)['first_divergence_stage']=='SEMANTIC_ROLE'
    actual['axes']['mentions'][0]['end']=2
    assert score_case(case,actual)['first_divergence_stage']=='MENTION_BOUNDARY'


def test_common_set_requires_same_frozen_inputs_and_runtime_parity():
    case,actual=specimen()
    provenance={k:'frozen' for k in ('gold_hash','catalog_hash','candidate_snapshot_hash','as_of','scope','evaluator_version','evaluator_hash')}
    provenance['runtime_parity_verified']=True
    a=summarize([case],[actual],provenance=provenance);b=deepcopy(a)
    assert common_set(a,b)['COMMON_EVALUABLE_SET']==[case['case_id']]
    b['catalog_hash']='different'
    assert not common_set(a,b)['comparison_valid'] and not common_set(a,b)['COMMON_EVALUABLE_SET']


def test_parse_only_difference_is_not_an_accepted_plan_safety_claim():
    from tools.cutover.harness_safety import ACCEPTANCE_AXES
    assert 'mentions' not in ACCEPTANCE_AXES and 'candidate_set' not in ACCEPTANCE_AXES
    assert not {'turn_relation','dialogue_act','metric_operations'}&ACCEPTANCE_AXES
    assert {'task_state','canonical_metrics','query_shape'}<=ACCEPTANCE_AXES


def test_schema_failures_keep_the_actual_model_stage_and_catalog_errors_are_separate():
    from tools.cutover.harness_runtime import error_category
    assert error_category({'reason':'V2_MODEL_OUTPUT_INVALID','last_model_stage':'v2_semantic_edits'},[],[])['stage']=='SEMANTIC_QUERY_IR'
    assert error_category({'reason':'CATALOG_RELATIONSHIP_REQUIRED'},[],[])['type']=='CATALOG_ERROR'


@pytest.mark.parametrize('reason,stage',[
    ('V2_ENTITY_ALIAS_REQUIRES_PATH','CANONICAL_BINDING'),
    ('V2_RELATION_BINDING_ROLE_CONFLICT','SEMANTIC_ROLE'),
    ('V2_TEMPORAL_BASE_REQUIRED','TIME_NORMALIZATION')])
def test_inner_contract_boundary_is_not_hidden_by_outer_patch_observer(reason,stage):
    from tools.cutover.harness_runtime import error_category
    events=[{'stage':'TaskPatchInput','error_type':'RecognitionFailure'}]
    assert error_category({'reason':reason},[],events)['stage']==stage
