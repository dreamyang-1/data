from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from tools.cutover.build_evaluation_gold import freeze
from tools.cutover.evaluation_contract import digest
from tools.cutover.frozen_source_values import FrozenSourceValues, seal
from tools.cutover.capture_source_values import capture
from tools.cutover.run_raw_transition_benchmark import run, SourceValuesUnavailable
from test_v2_source_value_binding import catalog as source_catalog, source_step, referenced_edit
from test_v2_raw_turn_recognition import ScriptedTransport


@pytest.fixture
def value_inputs(source_catalog):
    from catalog_value_sources import observe
    snapshot=deepcopy(source_catalog[4][(81,(205,))])
    raw=json.dumps(snapshot,ensure_ascii=False).encode()
    catalog=freeze(snapshot,hashlib.sha256(raw).hexdigest(),'2026-09-09T09:00:00+08:00')
    field=snapshot['physical_catalog']['entity_value_sources']['fields'][0]
    assert field['attr_code']=='city'
    entries=[dict(query=value,limit=8,observation=observe(catalog['scope'],field,value,8)) for value in ['上海','江苏','不存在']]
    bundle=seal(entries,catalog=catalog,capture_mode='SYNTHETIC_TEST')
    return snapshot,raw,catalog,bundle,source_catalog


def fixture_store(values,bundle=None,allow_synthetic=True):
    snapshot,raw,catalog,original,_=values
    bundle=bundle or original
    return FrozenSourceValues(bundle,catalog=catalog,snapshot=snapshot,expected_hash=bundle['artifact_hash'],allow_synthetic=allow_synthetic)


def reseal_bundle(bundle):
    bundle['artifact_hash']=digest({k:v for k,v in bundle.items() if k!='artifact_hash'})


@pytest.mark.asyncio
async def test_real_pin_restores_and_rechecks_frozen_values_without_source_connection(value_inputs):
    snapshot,raw,catalog,bundle,source_catalog=value_inputs
    steps=[source_step(),referenced_edit('江苏','REPLACE')]
    row=dict(case_id='SV1',history=[steps[0][0]],current_utterance=steps[1][0],clock='2026-09-09T09:00:00+08:00',
        scope=catalog['scope'],catalog_ref=catalog['artifact_hash'],initial_pending=False,dataset=None,
        labels={'canonical_metrics':['METRIC:amount'],'target_task':'PREVIOUS'},safety_checks=['wrong_inheritance'],
        catalog_evidence=['METRIC:amount'],business_evidence=['EXPLICIT_REPLACE_CONTRACT'],label_status='REVIEWED_FOR_LISTED_AXES')
    settings=Settings(_env_file=None,intent_model_name='fixture-model',intent_model_base_url='https://model.invalid/v1',intent_model_api_key='private-test-token')
    reads=len(source_catalog[6]);captures=[]
    result=await run([row],catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),
        source_observations=bundle,source_observations_hash=bundle['artifact_hash'],private_capture=captures.append)
    assert result['predictions'][0]['status']=='OK', [r.get('reason') for r in result['turns']]
    assert len(source_catalog[6])==reads
    assert result['evaluation']['source_value_observations_replayed']>=4
    assert result['evaluation']['source_value_reads_denied']==result['evaluation']['source_SQL_executions']==0
    assert result['evaluation']['source_value_evidence_mode']=='SYNTHETIC_TEST'
    assert result['evaluation']['safety']['wrong_inheritance']['gate']=='NOT_EVALUATED'
    actual=captures[-1]['result']
    assert actual['plan']['logical_plan']['payload']['filters']['value']['ref']['display_name']=='江苏'
    assert actual['next_state']['source_value_bindings'] and actual['plan_state']['source_value_bindings']
    assert captures[-1]['before']['state']==captures[0]['result']['next_state']
    from app.semantic_v2.recognition import RecognizedPlan
    from tools.cutover.transition_observations import observe_v2_plan
    typed=RecognizedPlan.model_validate(actual)
    for changes in ({'payload':{}},{'source_value_bindings':()}):
        corrupt=typed.model_copy(update={'next_state':typed.next_state.model_copy(update=changes)})
        with pytest.raises(ValueError,match='V2_OBSERVATION_STATE_DIGEST_INVALID'):
            observe_v2_plan(corrupt,row,catalog=catalog,mode='SCRIPTED_MODEL_PIPELINE')


def test_empty_observation_is_not_replaced_with_a_fabricated_candidate(value_inputs):
    store=fixture_store(value_inputs);entry=value_inputs[3]['entries'][-1];obs=entry['observation']
    assert store.observe(obs['scope'],obs['field'],entry['query'],8)['values']==[]
    assert store.calls[-1]['status']=='REPLAYED'


def test_replay_does_not_allow_caller_mutation_to_change_later_evidence(value_inputs):
    store=fixture_store(value_inputs);entry=value_inputs[3]['entries'][0];obs=entry['observation']
    first=store.observe(obs['scope'],obs['field'],entry['query'],8)
    first['values'].clear();first['field']['mapping_column']='changed'
    assert store.observe(obs['scope'],obs['field'],entry['query'],8)==obs


@pytest.mark.parametrize('fault',['scope','field','query','limit'])
def test_missing_query_never_falls_back_to_live_or_wider_scope(value_inputs,fault):
    store=fixture_store(value_inputs);entry=deepcopy(value_inputs[3]['entries'][0]);obs=entry['observation']
    scope,field,value,limit=obs['scope'],obs['field'],entry['query'],entry['limit']
    if fault=='scope':scope['business_domain_ids']=[]
    if fault=='field':field['mapping_column']='different'
    if fault=='query':value='上海市'
    if fault=='limit':limit=32
    with pytest.raises(SourceValuesUnavailable):store.observe(scope,field,value,limit)
    assert store.calls[-1]['status']=='MISSING'


@pytest.mark.parametrize('fault',['hash','catalog','scope','field','query','value','complete','timestamp','observation_hash','duplicate','extra'])
def test_tampered_or_incompatible_evidence_is_rejected(value_inputs,fault):
    bundle=deepcopy(value_inputs[3]);entry=bundle['entries'][0];obs=entry['observation']
    if fault=='hash':bundle['artifact_hash']='wrong'
    if fault=='catalog':bundle['catalog_version']='different'
    if fault=='scope':bundle['scope']['business_domain_ids']=[206]
    if fault=='field':obs['field']['mapping_column']='different'
    if fault=='query':entry['query']='北京'
    if fault=='value':obs['values']=['invented']
    if fault=='complete':obs['complete']=False
    if fault=='timestamp':obs['observed_at']='2026-09-09T09:00:00'
    if fault=='observation_hash':obs['observation_hash']='wrong'
    if fault=='duplicate':bundle['entries'].append(deepcopy(entry))
    if fault=='extra':obs['sql']='SELECT arbitrary'
    if fault!='hash':reseal_bundle(bundle)
    with pytest.raises(ValueError):fixture_store(value_inputs,bundle)


def test_a_synthetic_fixture_cannot_claim_live_source_provenance(value_inputs):
    with pytest.raises(ValueError,match='PROVENANCE_INVALID'):fixture_store(value_inputs,allow_synthetic=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['imported','script'])
async def test_missing_frozen_evidence_has_the_same_failure_code_from_script_entry(value_inputs,entry):
    import runpy
    snapshot,raw,catalog,bundle,_=value_inputs
    bundle=deepcopy(bundle);bundle['entries']=bundle['entries'][1:];reseal_bundle(bundle)
    step=source_step()
    row=dict(case_id='MISS',history=[step[0]],current_utterance=step[0],clock='2026-09-09T09:00:00+08:00',
        scope=catalog['scope'],catalog_ref=catalog['artifact_hash'],initial_pending=False,dataset=None,
        labels={'canonical_metrics':['METRIC:amount']},safety_checks=[],catalog_evidence=['METRIC:amount'],
        business_evidence=['EXACT_SOURCE_QUERY'],label_status='REVIEWED_FOR_LISTED_AXES')
    settings=Settings(_env_file=None,intent_model_name='fixture-model',intent_model_base_url='https://model.invalid/v1',intent_model_api_key='private-test-token')
    execute=run if entry=='imported' else runpy.run_path(str(Path(__file__).parents[1]/'tools/cutover/run_raw_transition_benchmark.py'))['run']
    result=await execute([row],catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport([step])),
        source_observations=bundle,source_observations_hash=bundle['artifact_hash'])
    assert result['turns'][0]['reason']=='FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED'
    assert result['evaluation']['source_value_reads_denied']==1 and result['evaluation']['source_SQL_executions']==0


def test_read_only_collector_uses_existing_source_reader_and_current_catalog(value_inputs,monkeypatch):
    import catalog_release
    snapshot,raw,catalog,_,source_catalog=value_inputs
    snapshots=[]
    def current(*args):snapshots.append(args);return deepcopy(snapshot)
    monkeypatch.setattr(catalog_release,'capture_catalog',current)
    field=snapshot['physical_catalog']['entity_value_sources']['fields'][0]
    queries=[dict(attribute_id=field['attribute_id'],query='上海',limit=8)]
    reads=len(source_catalog[6])
    result=capture(raw,catalog,queries,service_root=Path('unused'),allow_source_reads=True)
    assert len(source_catalog[6])==reads+1 and len(snapshots)==2
    assert result['capture_mode']=='READ_ONLY_SOURCE'
    assert fixture_store(value_inputs,result,allow_synthetic=False).entries


@pytest.mark.parametrize('fault',['opt_in','drift','field','budget'])
def test_collector_rejects_invalid_authority_before_source_reads(value_inputs,monkeypatch,fault):
    import catalog_release
    snapshot,raw,catalog,_,source_catalog=value_inputs
    monkeypatch.setattr(catalog_release,'capture_catalog',lambda *a:{'catalog_version':'changed'})
    field=snapshot['physical_catalog']['entity_value_sources']['fields'][0]
    queries=[dict(attribute_id=field['attribute_id'],query='上海',limit=8)]
    if fault=='field':queries[0]['attribute_id']='unknown'
    if fault=='budget':queries*=21
    reads=len(source_catalog[6])
    with pytest.raises(ValueError):capture(raw,catalog,queries,service_root=Path('unused'),allow_source_reads=fault!='opt_in')
    assert len(source_catalog[6])==reads
