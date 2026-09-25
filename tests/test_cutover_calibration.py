from copy import deepcopy
import json
from pathlib import Path

import pytest
from tools.cutover.calibration_profile import profile,divergence,catalog_complexity
from tools.cutover.calibration_gold import build_slice,score,project_observation,validate,TAX
from tools.cutover.calibration_v1 import V1OutcomeObserver,compare_common,frozen_dependencies_only,FrozenDependencyMissing
from tools.cutover.harness_cli import public_cases

ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def catalog():return json.loads((ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json').read_text(encoding='utf-8'))

def observed_gold(case):
    expected=case['expected'];state=deepcopy(expected['expected_task_semantic_state'])
    def native(value):
        if isinstance(value,dict):
            if 'fact_id' in value:
                kind,code=value['fact_id'].split(':',1)
                return {'catalog_type':kind,'canonical_code':code,'semantic_role':value['role']}
            return {k:native(v) for k,v in value.items()}
        if isinstance(value,list):return [native(v) for v in value]
        return value
    operations=deepcopy(expected['task_operation']);patch={'base_task_version':0 if expected['target_task']=='NEW' else 1}
    for op in operations:
        operation='SET' if op['operation']=='INITIALIZE' else op['operation']
        phase={'SET':'sets','ADD':'adds','REMOVE':'removes','REPLACE':'replacements','CLEAR':'clears'}[operation]
        patch.setdefault(phase,[]).append({'slot_path':op['slot_path'],'operation':operation,'new_value':native(op['values'])})
    return {'axes':{'scope':deepcopy(expected['scope']),'target_task':expected['target_task'],'task_patch':patch,
                    'task_state':native(state),'query_shape':expected['query_shape'],
                    'semantic_query_ir':native(expected['expected_semantic_query_ir'])}}

def test_private_metric_names_are_not_relation_or_dimension_features(catalog):
    row=deepcopy(public_cases()[14]);row['current_utterance']='请提供已合作医院数的汇总';row['labels']={}
    p=profile([row],catalog)
    assert p['features']['relationship']['n']==0
    assert p['features']['multi_dimension_current_surface']['n']==0
    assert p['query_shape']=={'UNKNOWN_UNLABELED':1}

def test_profile_rejects_blind_before_feature_processing(catalog):
    row={'split':'BLIND_HOLDOUT'}
    with pytest.raises(ValueError,match='BLIND_HOLDOUT_FORBIDDEN'):profile([row],catalog)

def test_dependency_breadth_is_not_inferred_from_outputs_or_missing_labels():
    snapshot={'documents':[{'metrics':[{'metric_code':'a','source_dependency':{'bind_entity':['A']},'metric_name':'Amount'},
                                      {'metric_code':'b','source_dependency':{'bind_entity':['B']},'metric_name':'Count'}]}]}
    result=catalog_complexity([{'case_id':'x','labels':{'canonical_metrics':['METRIC:a','METRIC:b']}},
                               {'case_id':'y','labels':{}}],snapshot)
    assert result['metric_label_coverage']=={'n':1,'N':2}
    assert result['no_common_owner_for_multiple_metrics']=={'n':1,'N':1}

def test_divergence_is_unique_and_preserves_downstream():
    cases=[{'case_id':'a'}]
    result=divergence(cases,[{'case_id':'a','status':'FAIL','first_divergence_stage':'TURN_RESOLUTION',
                              'downstream_effects':[{'stage':'TASK_OPERATION'}]}])
    assert list(result['distribution'])==['TURN_RESOLUTION']
    assert result['cases'][0]['downstream_effects']==[{'stage':'TASK_OPERATION'}]
    with pytest.raises(ValueError,match='MISMATCH'):divergence([{'case_id':'b'}],result['cases'])

def test_independent_full_gold_complete_but_no_dryplan_is_not_pass(catalog):
    cases=build_slice(catalog,public_cases());assert len(cases)==16
    for case in cases:
        result=score(case,observed_gold(case),catalog)
        assert result['status']=='BLOCKED'
        assert result['axes']['expected_dry_plan_outcome']=='NOT_OBSERVED'
        assert set(result['axes'].values())=={'PASS','NOT_OBSERVED'}

@pytest.mark.parametrize('bad_source',['V1_OUTPUT','V2_OUTPUT','LLM','UNKNOWN'])
def test_runtime_answers_cannot_certify_full_gold(catalog,bad_source):
    case=build_slice(catalog,public_cases())[0]
    case['label_provenance']['metric_binding']['label_source']=bad_source
    with pytest.raises(ValueError,match='CANNOT_CERTIFY'):validate(case,catalog)

@pytest.mark.parametrize('mutation',['wrong_metric','wrong_role','wrong_scope','database_scope','knowledge_scope','extra_filter','extra_dimension','duplicate_metric'])
def test_full_plan_mutations_are_failures(catalog,mutation):
    case=build_slice(catalog,public_cases())[0];obs=observed_gold(case);a=obs['axes']
    if mutation=='wrong_metric':a['task_state']['metrics'][0]['canonical_code']='order_count'
    if mutation=='wrong_role':a['task_state']['metrics'][0]['semantic_role']='GROUP_BY'
    if mutation=='wrong_scope':a['scope']={**a['scope'],'semantic_model_id':82}
    if mutation=='database_scope':a['scope']['database_id']=99
    if mutation=='knowledge_scope':a['scope']['knowledge_base_names']=['unrequested']
    if mutation=='extra_filter':a['task_state']['filter_expression']={'operator':'EQ','value':'unrequested'}
    if mutation=='extra_dimension':a['task_state']['dimensions']=[{'catalog_type':'DIMENSION','canonical_code':'hospital','semantic_role':'GROUP_BY'}]
    if mutation=='duplicate_metric':a['task_state']['metrics']*=2
    assert score(case,obs,catalog)['status']=='FAIL'

def test_initialize_set_and_scalar_add_are_equivalent_only_on_empty_new_task(catalog):
    case=build_slice(catalog,public_cases())[0];obs=observed_gold(case)
    op=obs['axes']['task_patch'].pop('sets')[0];op['operation']='ADD';op['new_value']=op['new_value'][0]
    obs['axes']['task_patch']['adds']=[op]
    assert score(case,obs,catalog)['axes']['task_operation']=='PASS'
    obs['axes']['task_patch']['base_task_version']=1
    assert score(case,obs,catalog)['axes']['task_operation']=='FAIL'

def test_replace_followup_cannot_be_scored_as_add(catalog):
    case=next(c for c in build_slice(catalog,public_cases()) if c['source_case_id']=='G81-089');obs=observed_gold(case)
    op=obs['axes']['task_patch'].pop('replacements')[0];op['operation']='ADD'
    obs['axes']['task_patch']['adds']=[op]
    assert score(case,obs,catalog)['status']=='FAIL'

def test_provider_timeout_stays_blocked_not_semantic_failure(catalog):
    case=build_slice(catalog,public_cases())[0]
    result=score(case,{'error':{'type':'MODEL_TIMEOUT'}},catalog)
    assert result['status']=='BLOCKED'

def test_common_comparison_never_demands_v2_internal_objects():
    common={'clarification_decision':'NO_ASK'}
    result=compare_common(common,common,common,same_input_manifest=True)
    assert result['whole_outcome_comparable']
    with pytest.raises(ValueError,match='V2_INTERNAL_AXIS'):
        compare_common({}, {}, {'task_patch':{}},same_input_manifest=True)
    assert not compare_common({},common,common,same_input_manifest=True)['whole_outcome_comparable']

@pytest.mark.asyncio
async def test_v1_observer_calls_real_entry_and_does_not_change_semantic_response():
    from app.config import Settings
    from app.adapters import build_mock_adapters
    from app.intent import HybridIntentClassifier
    from app.domain.models import ChatRequest,TrustedIdentity
    from app.services.orchestrator import DataAnalysisOrchestrator
    from app.stores import InMemorySessionStore
    settings=Settings(_env_file=None,env='test',adapter_mode='mock',intent_model_enabled=False,
                      analysis_synthesis_enabled=False,chat_model_enabled=False,multi_question_enabled=False,
                      dynamic_skills_enabled=False)
    def runtime():return DataAnalysisOrchestrator(settings,HybridIntentClassifier(settings),build_mock_adapters(),InMemorySessionStore())
    chat=ChatRequest(question='查询2026年1月销售额',application_id='evaluation',conversation_id='no-op',message_id='t',semantic_model_id=81,business_domain_ids=[205])
    identity=TrustedIdentity(tenant_id='evaluation',user_id='evaluation')
    off=await runtime().handle(chat,identity)
    with V1OutcomeObserver(runtime()) as observer:on=await observer.run(chat,identity)
    # Request/trace UUIDs are intentionally not semantic response fields.
    for key in ('status','intent','understood_slots','clarification_questions','missing_slots'):
        assert getattr(on,key)==getattr(off,key)
    assert observer.outcome()['runtime_entry']=='DataAnalysisOrchestrator.handle'
    assert observer.events

@pytest.mark.asyncio
async def test_missing_v1_record_cannot_be_used_as_rule_fallback():
    import httpx
    with frozen_dependencies_only() as attempts:
        with pytest.raises(FrozenDependencyMissing):
            async with httpx.AsyncClient() as client:await client.get('https://invalid.example/')
    assert attempts==['HTTP']

def test_fake_orchestrator_is_not_runtime_adapter():
    with pytest.raises(ValueError,match='REAL_V1_ORCHESTRATOR'):V1OutcomeObserver(object())
