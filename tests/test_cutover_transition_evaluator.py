from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.cutover.build_transition_gold import build
from tools.cutover.evaluation_contract import digest
from tools.cutover.transition_evaluator import evaluate_transitions,validate_transitions
from tools.cutover.transition_observations import observe_legacy_rules,observe_v2_plan
from tools.cutover.run_transition_baseline import run


@pytest.fixture
def corpus():
    catalog=json.loads((Path(__file__).parents[1]/'docs/cutover/evaluation_gates/frozen_catalog.json').read_text(encoding='utf-8'))
    return build(catalog),catalog


def observation(row,**changes):
    value={'case_id':row['case_id'],'scope':row['scope'],'catalog_ref':row['catalog_ref'],
        'component':'CONTROL','mode':'SCRIPTED_MODEL_PIPELINE','status':'OK','axes':deepcopy(row['labels']),
        'safety':{k:False for k in row['safety_checks']}}
    value.update(changes);return value


def score(rows,values,catalog):
    return evaluate_transitions(rows,values,catalog,component='CONTROL',mode='SCRIPTED_MODEL_PIPELINE')


def test_fixed_catalog_gold_has_twenty_reviewed_transitions_not_complete_labels(corpus):
    rows,catalog=corpus
    assert len(validate_transitions(rows,catalog))==20
    assert all(r['missing_labels'] and r['label_status']=='REVIEWED_FOR_LISTED_AXES' for r in rows)
    assert len({digest({'history':r['history'],'text':r['current_utterance'],'dataset':r['dataset'],'pending':r['initial_pending']}) for r in rows})==20


@pytest.mark.parametrize('fault',['missing','failed','not_run','partial'])
def test_missing_observations_never_turn_into_perfect_safety_or_shrunk_denominators(corpus,fault):
    rows,catalog=corpus;row=rows[0];p=observation(row)
    if fault in {'failed','not_run'}:p['status']=fault.upper()
    if fault=='partial':p['axes']={};p['safety']={}
    result=score([row],[] if fault=='missing' else [p],catalog)
    assert result['metrics']['metric_surfaces']=={'passed':0,'denominator':1,'unobserved':1,'value':0.0}
    assert result['safety']['wrong_inheritance']['gate']=='NOT_EVALUATED'
    assert result['safety']['wrong_inheritance']['unobserved']==1
    assert result['production_cutover_pass'] is False


@pytest.mark.parametrize('axis,bad',[
    ('metric_surfaces',['订单笔数']), # ADD became REPLACE.
    ('metric_surfaces',['含税销售总额','销售总数量','订单笔数','订单笔数']),
    ('canonical_metrics',['METRIC:cooperating_hospital_count']),
    ('target_task','NEW'),
    ('metric_operations',['REPLACE']),
])
def test_state_and_operation_contrasts_fail_independently(corpus,axis,bad):
    rows,catalog=corpus;row=rows[0];p=observation(row);p['axes'][axis]=bad
    result=score([row],[p],catalog)
    assert result['metrics'][axis]['passed']==0
    assert result['metrics']['turn_relation']['passed']==1


def test_collection_order_can_differ_but_types_and_duplicates_cannot(corpus):
    rows,catalog=corpus;row=rows[0];p=observation(row)
    p['axes']['metric_surfaces'].reverse()
    assert score([row],[p],catalog)['metrics']['metric_surfaces']['passed']==1
    row=rows[14];p=observation(row);p['axes']['metric_clarification_required']=0
    assert score([row],[p],catalog)['metrics']['metric_clarification_required']['passed']==0


@pytest.mark.parametrize('fault',['catalog','scope','duplicate','unknown','mode','component','safety_type'])
def test_invalid_or_mixed_receipts_are_rejected(corpus,fault):
    rows,catalog=corpus;row=rows[0];p=observation(row);values=[p]
    if fault=='catalog':p['catalog_ref']='foreign'
    if fault=='scope':p['scope']={**p['scope'],'business_domain_ids':[]}
    if fault=='duplicate':values.append(deepcopy(p))
    if fault=='unknown':p['case_id']='unknown'
    if fault=='mode':p['mode']='LIVE_MODEL_PLAN_ONLY'
    if fault=='component':p['component']='V2'
    if fault=='safety_type':p['safety']['wrong_inheritance']=0
    with pytest.raises(ValueError):score([row],values,catalog)


def test_a_safety_violation_is_not_hidden_by_perfect_semantic_axes(corpus):
    rows,catalog=corpus;row=rows[0];p=observation(row)
    p['safety']['wrong_inheritance']=True
    result=score([row],[p],catalog)
    assert all(v['value']==1 for v in result['metrics'].values())
    assert result['safety']['wrong_inheritance']['gate']=='FAIL'
    assert result['production_cutover_pass'] is False


def test_unlabeled_scope_safety_violation_is_not_discarded(corpus):
    rows,catalog=corpus;row=rows[0];p=observation(row)
    assert 'scope_expansion' not in row['safety_checks']
    p['safety']['scope_expansion']=True
    result=score([row],[p],catalog)
    assert result['safety']['scope_expansion']['gate']=='FAIL'
    assert result['safety']['scope_expansion']['violations']==1
    assert result['safety']['cross_scope_dataset_reuse']['gate']=='NOT_EVALUATED'


@pytest.mark.parametrize('fault',['hash','unknown_axis','unknown_fact','no_evidence','unknown_safety'])
def test_gold_requires_authoritative_scoped_label_evidence(corpus,fault):
    rows,catalog=deepcopy(corpus);row=rows[0]
    if fault=='hash':catalog['catalog_version']='forged'
    if fault=='unknown_axis':row['labels']['overall_accuracy']=1
    if fault=='unknown_fact':row['labels']['canonical_metrics']=['METRIC:invented']
    if fault=='no_evidence':row['business_evidence']=[]
    if fault=='unknown_safety':row['safety_checks']=['imagined']
    with pytest.raises(ValueError):validate_transitions([row],catalog)


def test_v1_actual_service_observation_never_reads_expected_answers(corpus):
    rows,_=corpus;row=rows[0]
    clean={k:v for k,v in row.items() if k not in {'labels','safety_checks'}}
    first=observe_legacy_rules(clean)
    wrong=deepcopy(row);wrong['labels']={'metric_surfaces':['INJECTED']}
    second=observe_legacy_rules(wrong)
    assert first==second
    assert set(first['axes']['metric_surfaces'])=={'含税销售总额','销售总数量','订单笔数'}
    assert first['axes']['metric_operations']==['ADD']
    assert 'canonical_metrics' not in first['axes']
    assert 'wrong_inheritance' not in first['safety']


def test_actual_v1_dataset_limit_and_global_rank_contrast(corpus):
    rows,_=corpus
    actual=[observe_legacy_rules(r) for r in rows[-3:]]
    assert [p['axes']['dataset_route'] for p in actual]==['DISPLAY_LIMIT','GLOBAL_RANK_REPLAN_OR_REJECT','LOCAL_RANK_COMPLETE_DATASET']
    assert all(p['safety']['unsafe_truncated_ranking'] is False for p in actual)


def test_runner_denies_connect_and_restores_process_boundary(corpus,monkeypatch):
    import socket
    import tools.cutover.run_transition_baseline as module
    rows,catalog=corpus;original=socket.socket.connect;attempts=[]
    def probe(row):
        with socket.socket() as sock:
            with pytest.raises(RuntimeError,match='NETWORK_DENIED'):sock.connect(('127.0.0.1',1))
        attempts.append(row['case_id'])
        return observe_legacy_rules(row)
    monkeypatch.setattr(module,'observe_legacy_rules',probe)
    _,report=run(rows[:1],catalog)
    assert len(attempts)==1 and socket.socket.connect is original
    assert report['real_model_calls']==0


@pytest.mark.asyncio
async def test_v2_adapter_reads_actual_scoped_multiturn_state_and_patch_without_gold():
    from test_v2_raw_turn_recognition import catalog as fixture,metric_step,planner,turns
    service=fixture.__wrapped__();steps=[metric_step('销售额'),metric_step('再加订单笔数','订单笔数','ADD',True)]
    engine,_=planner(service,steps);results=await turns(engine,steps)
    catalog={'catalog_version':results[-1].next_state.context.catalog_pin.catalog_version,
        'facts':[{'fact_id':'METRIC:amount'},{'fact_id':'METRIC:orders'}]}
    catalog['artifact_hash']=digest(catalog)
    case={'case_id':'ADAPTER_CONTROL','scope':{'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'},'catalog_ref':catalog['artifact_hash']}
    actual=observe_v2_plan(results[-1],case,catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE',previous=results[0],history=results[:1])
    assert actual['axes']['canonical_metrics']==['METRIC:amount','METRIC:orders']
    assert actual['axes']['metric_operations']==['ADD']
    assert actual['axes']['target_task']=='PREVIOUS'
    assert actual['mode']=='SCRIPTED_MODEL_PIPELINE' and actual['safety']=={}
    bad={**case,'scope':{**case['scope'],'business_domain_ids':[]}}
    with pytest.raises(ValueError,match='SCOPE_OR_CATALOG'):observe_v2_plan(results[-1],bad,catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE')
    with pytest.raises(ValueError,match='CATALOG_HASH'):observe_v2_plan(results[-1],case,catalog={**catalog,'catalog_version':'foreign'},mode='SCRIPTED_MODEL_PIPELINE')
    with pytest.raises(ValueError,match='SCOPE_OR_CATALOG'):observe_v2_plan(results[-1],{**case,'database_id':42},catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE')


@pytest.mark.asyncio
async def test_v2_actual_pending_ask_and_answer_remain_distinct_observations():
    from test_v2_pending_recognition import catalog as fixture,ask,answer
    service=fixture.__wrapped__();pending,_=await ask(service)
    resolved=await answer(service,pending,pending.decision.options[0].display_label)
    catalog={'catalog_version':pending.next_state.context.catalog_pin.catalog_version,
        'facts':[{'fact_id':'METRIC:amount'},{'fact_id':'METRIC:quantity'}]}
    catalog['artifact_hash']=digest(catalog)
    case={'case_id':'PENDING_CONTROL','scope':{'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'},'catalog_ref':catalog['artifact_hash']}
    first=observe_v2_plan(pending,case,catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE')
    second=observe_v2_plan(resolved,case,catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE',previous=pending)
    assert first['axes']['clarification_decision']=='ASK'
    assert first['evidence']['already_asked'] is False
    assert second['axes']['clarification_decision']=='NO_ASK'
    assert second['axes']['turn_relation']=='ANSWER_PENDING'
    assert second['axes']['target_task']=='PREVIOUS'
