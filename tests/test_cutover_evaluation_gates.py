from copy import deepcopy
import pytest
from tools.cutover.evaluation_contract import (stage_gate, snapshot_unique, identity_decision,
    declared_identity, NAME_WARNING, COLLISION_WARNING, digest)
from tools.cutover.semantic_evaluator import evaluate, validate_gold


OBS={'catalog_version':'v1','snapshot_hash':'abc','scope':{'semantic_model_id':81,'business_domain_ids':[205]}}
GOOD={'identity_candidate':['hospital_id'],'candidate_kind':'CODE_OR_ID','exists':True,
    'total_count':3,'non_null_count':3,'distinct_count':3,'duplicate_count':0,'duplicate_groups':0,'blank_count':0}


@pytest.mark.parametrize('stage', ['GOLD','EVALUATOR','MODEL_BENCHMARK','PLAN_ONLY_SHADOW'])
def test_production_recovery_does_not_globally_block_evaluation(stage):
    assert stage_gate(stage,frozen_snapshot=True,labels_ready=True,evaluator_verified=True,
        model_configured=True,evaluation_passed=True,state_isolated=True)['eligible']


@pytest.mark.parametrize('stage',['CANARY','CUTOVER'])
def test_production_still_requires_recovery_publication_and_trust(stage):
    gate=stage_gate(stage,frozen_snapshot=True,labels_ready=True,evaluator_verified=True,
        evaluation_passed=True,state_isolated=True,shadow_passed=True)
    assert not gate['eligible']
    assert {'PRODUCTION_REDIS_RECOVERY_REQUIRED','NATIVE_CATALOG_PUBLICATION_REQUIRED','DEPLOYED_TRUST_BOUNDARY_REQUIRED'} <= set(gate['missing'])


@pytest.mark.parametrize('flag',['writes_production_state','executes_sql','takes_over_response'])
def test_shadow_cannot_hide_side_effects_behind_offline_gate(flag):
    result=stage_gate('PLAN_ONLY_SHADOW',frozen_snapshot=True,labels_ready=True,evaluator_verified=True,
        evaluation_passed=True,state_isolated=True,**{flag:True})
    assert not result['eligible']


def test_cutover_always_needs_user_approval():
    result=stage_gate('CUTOVER',frozen_snapshot=True,labels_ready=True,evaluator_verified=True,
        evaluation_passed=True,state_isolated=True,native_publication=True,redis_recovery=True,
        deployment_trust=True,shadow_passed=True)
    assert result['missing']==['EXPLICIT_USER_REPLACEMENT_APPROVAL_REQUIRED']


@pytest.mark.parametrize('changes',[
    {'total_count':0,'non_null_count':0,'distinct_count':0},
    {'non_null_count':2}, {'distinct_count':2,'duplicate_count':1,'duplicate_groups':1},
    {'blank_count':1}, {'distinct_count':True}, {'total_count':'3'}, {'duplicate_count':None},
])
def test_empty_nullable_duplicate_or_malformed_snapshot_is_not_provisional(changes):
    candidate={**GOOD,**changes}
    assert not snapshot_unique(candidate)
    decision=identity_decision(declared_key=None,candidate=candidate,observation=OBS,query_shape='JOIN')
    assert not decision['allowed']


def test_same_name_ids_keep_identity_and_warning():
    value=identity_decision(declared_key=None,candidate=GOOD,observation=OBS,
        query_shape='RELATIONSHIP',same_name_different_ids=True)
    assert value['allowed'] and value['identity_fields']==['hospital_id']
    assert value['identity_mode']=='PROVISIONAL_VERIFIED_IDENTITY'
    assert not value['catalog_declared'] and value['uniqueness_verified']
    assert value['warning']==COLLISION_WARNING


@pytest.mark.parametrize('shape',['JOIN','ATTRIBUTION','ENTITY_RANKING','DISTINCT_ENTITY_COUNT','RELATIONSHIP'])
def test_name_fallback_never_claims_exact_entity_results(shape):
    candidate={**GOOD,'identity_candidate':['hospital_name'],'candidate_kind':'NAME'}
    value=identity_decision(declared_key=None,candidate=candidate,observation=OBS,query_shape=shape)
    assert not value['allowed'] and value['warning']==NAME_WARNING


def test_name_display_and_explicit_name_count_require_warning():
    for shape in ('DISPLAY','NAME_LIST','EXPLICIT_NAME_COUNT'):
        value=identity_decision(declared_key=None,candidate=None,observation=OBS,query_shape=shape)
        assert value['allowed'] and value['identity_mode']=='NAME_FALLBACK' and value['warning']


def test_known_distinct_ids_forbid_name_merge_even_for_display():
    assert not identity_decision(declared_key=None,candidate=None,observation=OBS,
        query_shape='DISPLAY',same_name_different_ids=True)['allowed']


def test_row_key_does_not_decide_business_order_grain():
    assert not identity_decision(declared_key=None,candidate=GOOD,observation=OBS,
        query_shape='DISTINCT_ENTITY_COUNT',grain_verified=False)['allowed']


def test_attribute_declaration_is_not_lost_when_entity_pk_is_empty():
    for code in ('salesperson','project','sales_company'):
        entity={'entity_code':code,'primary_key':None,'attributes':[{'attr_code':'id','is_primary_key':True}]}
        value=declared_identity(entity)
        assert value['key']==['id'] and value['metadata_inconsistency']
        assert entity['primary_key'] is None


def test_string_zero_and_multiple_flags_cannot_manufacture_identity():
    assert declared_identity({'attributes':[{'attr_code':'id','is_primary_key':'0'}]})['key'] is None
    assert declared_identity({'attributes':[{'attr_code':c,'is_primary_key':True} for c in ('a','b')]})['key'] is None
    assert declared_identity({'primary_key':['a','b'],'attributes':[{'attr_code':c,'is_primary_key':True} for c in ('a','b')]})['key']==['a','b']


def sample():
    cat={'scope':OBS['scope'],'catalog_version':'v1','facts':[{'fact_id':'m'}]}
    cat['artifact_hash']=digest(cat)
    row={'case_id':'a','current_utterance':'订单笔数','scope':cat['scope'],'catalog_ref':cat['artifact_hash'],
        'catalog_evidence':['m'],'label_status':'REVIEWED_FOR_LISTED_AXES',
        'labels':{'query_shape':'SCALAR_AGGREGATE','mentions':[{'surface':'订单笔数','start':0,'end':4,'roles':['MEASURE']}]}}
    pred={'case_id':'a','scope':cat['scope'],'catalog_ref':cat['artifact_hash'],'status':'OK',
        'prediction':{'query_shape':'SCALAR_AGGREGATE','mentions':[{'start':0,'end':4,'roles':['MEASURE']}]}}
    return cat,row,pred


def test_evaluator_calibration_and_missing_predictions_are_not_passes():
    cat,row,pred=sample()
    assert all(m['value']==1 for m in evaluate([row],[pred],cat)['metrics'].values())
    result=evaluate([row],[],cat)
    assert all(m['value']==0 and m['denominator']==1 for m in result['metrics'].values())
    assert not result['production_cutover_pass']


@pytest.mark.parametrize('mutation',['duplicate','foreign_scope','catalog_tamper','unknown_case'])
def test_evaluator_rejects_cross_scope_or_corrupted_inputs(mutation):
    cat,row,pred=sample();predictions=[pred]
    if mutation=='duplicate':predictions.append(deepcopy(pred))
    if mutation=='foreign_scope':pred['scope']={'semantic_model_id':82}
    if mutation=='catalog_tamper':cat['facts']=[]
    if mutation=='unknown_case':pred['case_id']='missing'
    with pytest.raises(ValueError):evaluate([row],predictions,cat)


def test_enumerating_all_roles_does_not_score_pure_role_resolution():
    cat,row,pred=sample();pred['prediction']['mentions'][0]['roles']+=['PROJECTION_FIELD','GROUP_BY']
    result=evaluate([row],[pred],cat)
    assert result['metrics']['critical_role_recall']['value']==1
    assert result['metrics']['critical_role_purity']['value']==0


def test_frozen_business_corpus_covers_100_cases_and_all_four_operations():
    from pathlib import Path
    import json
    from tools.cutover.semantic_evaluator import read_jsonl
    from tools.cutover.build_evaluation_gold import build
    root=Path(__file__).resolve().parents[1]/'docs/cutover/evaluation_gates'
    catalog=json.loads((root/'frozen_catalog.json').read_text(encoding='utf-8'))
    rows=read_jsonl(root/'gold_axes.jsonl')
    assert len(validate_gold(rows,catalog))==100
    assert rows==build(catalog)
    assert {r['labels']['operation'] for r in rows if 'operation' in r['labels']}=={'ADD','REPLACE','REMOVE','CLEAR'}
    assert any(r['history'] for r in rows)
    assert all(not r['strict_identity_required_for_labeled_axes'] for r in rows)


def test_formal_declaration_cannot_override_missing_field_or_observed_duplicates():
    for changes in ({'exists':False},{'distinct_count':2,'duplicate_count':1,'duplicate_groups':1}):
        value=identity_decision(declared_key=['hospital_id'],candidate={**GOOD,**changes},
            observation=OBS,query_shape='JOIN')
        assert not value['allowed']


def test_normalized_parser_projection_preserves_turn_priority_and_unknown_shape():
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    from tools.cutover.run_parser_benchmark import normalize
    parsed=CurrentTurnSemanticParse(reference_signals=['HISTORICAL'],topic_shift_signals=['EXPLICIT_NEW_TASK'])
    value=normalize(parsed)
    assert value['turn_relation']=='NEW_TASK' and value['query_shape'] is None
    assert normalize(CurrentTurnSemanticParse(reference_signals=['ELLIPSIS']))['turn_relation']=='FOLLOW_UP'


@pytest.mark.asyncio
async def test_model_benchmark_only_sends_existing_current_turn_context(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    import httpx
    from app.config import Settings
    from pydantic import SecretStr
    from tools.cutover import run_parser_benchmark as runner
    cat,row,_=sample();row.update(clock='2026-09-09T09:00:00+08:00',history=['private prior turn'])
    (tmp_path/'catalog.json').write_text(json.dumps(cat),encoding='utf-8')
    (tmp_path/'gold.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
    calls=[]
    def respond(req):
        body=json.loads(req.content);context=json.loads(body['messages'][1]['content']);calls.append(context)
        assert set(context)=={'question','turn_id','clock','slots'}
        assert 'private prior turn' not in req.content.decode()
        assert 'labels' not in context and 'history' not in context
        return httpx.Response(200,json={'model':'mock-only','usage':{'total_tokens':1},'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'query_shape_prediction':'SCALAR_AGGREGATE'})}}]})
    monkeypatch.setattr(runner.httpx,'AsyncHTTPTransport',lambda **kwargs:httpx.MockTransport(respond))
    monkeypatch.setattr(runner,'Settings',lambda:Settings(_env_file=None,intent_model_api_key=SecretStr('test-only'),intent_model_base_url='https://example.invalid'))
    args=SimpleNamespace(catalog=tmp_path/'catalog.json',gold=tmp_path/'gold.jsonl',limit=None,case_ids=None,
        model='mock-only',thinking=False,output_dir=tmp_path/'out',concurrency=1)
    await runner.run(args)
    report=json.loads((tmp_path/'out/evaluation.json').read_text(encoding='utf-8'))
    assert len(calls)==1 and report['production_state_writes']==report['sql_executions']==0
    assert report['metrics']['query_shape']['passed']==1
    assert report['metrics']['critical_mention_recall']['passed']==0
