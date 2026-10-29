from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app.config import Settings
from tools.cutover.build_evaluation_gold import freeze
from tools.cutover.evaluation_contract import digest
from tools.cutover.frozen_source_values import FrozenSourceValues, seal
from tools.cutover.run_raw_transition_benchmark import run, replay_turn
from tools.cutover.capture_source_values import capture
from test_v2_source_value_binding import catalog as source_catalog, source_step
from test_v2_raw_turn_recognition import ScriptedTransport, metric_step


@pytest.fixture
def evidence(source_catalog):
    import catalog_value_sources as native
    snapshot=deepcopy(source_catalog[4][(81,(205,))]); raw=json.dumps(snapshot,ensure_ascii=False).encode()
    catalog=freeze(snapshot,hashlib.sha256(raw).hexdigest(),'2026-09-09T09:00:00+08:00')
    field=snapshot['physical_catalog']['entity_value_sources']['fields'][0]
    entries=[dict(query='上',limit=8,observation=native.observe(catalog['scope'],field,'上',8)),
        dict(query='上',limit=64,observation=native.observe_probe(catalog['scope'],field,'上',64)),
        dict(query='上海',limit=8,observation=native.observe(catalog['scope'],field,'上海',8))]
    bundle=seal(entries,catalog=catalog,capture_mode='SYNTHETIC_TEST')
    return source_catalog,snapshot,raw,catalog,field,bundle


def store(evidence,bundle=None):
    _,snapshot,_,catalog,_,original=evidence;bundle=bundle or original
    return FrozenSourceValues(bundle,catalog=catalog,snapshot=snapshot,expected_hash=bundle['artifact_hash'],allow_synthetic=True)


def test_discovery_receipt_cannot_satisfy_exact_lookup(evidence):
    _,_,_,catalog,field,_=evidence;frozen=store(evidence)
    assert frozen.observe(catalog['scope'],field,'上',8)['values']==[]
    assert frozen.observe_probe(catalog['scope'],field,'上',64)['values']==['上海','北京','江苏']
    from tools.cutover.frozen_source_values import SourceValuesUnavailable
    with pytest.raises(SourceValuesUnavailable): frozen.observe(catalog['scope'],field,'上',64)
    with pytest.raises(SourceValuesUnavailable): frozen.observe_probe(catalog['scope'],field,'上海',64)


@pytest.mark.parametrize('fault',['mode','source','field','scope','value_size','incomplete_values','duplicate','limit'])
def test_rehashed_probe_receipts_still_enforce_contract(evidence,fault):
    bundle=deepcopy(evidence[-1]);entry=bundle['entries'][1];obs=entry['observation']
    if fault=='mode':obs['match_mode']='EXACT_NORMALIZED'
    if fault=='source':obs['source']='VERIFIED_SOURCE_EXACT_LOOKUP'
    if fault=='field':obs['field']['mapping_column']='other'
    if fault=='scope':obs['scope']['business_domain_ids']=[]
    if fault=='value_size':obs['values']=['a'*257]
    if fault=='incomplete_values':obs['complete']=False
    if fault=='duplicate':obs['values']=['same','same']
    if fault=='limit':entry['limit']=65
    obs['observation_hash']=digest({k:v for k,v in obs.items() if k not in {'observation_hash','observed_at'}})
    bundle['artifact_hash']=digest({k:v for k,v in bundle.items() if k!='artifact_hash'})
    with pytest.raises(ValueError):store(evidence,bundle)


@pytest.mark.asyncio
async def test_benchmark_and_exact_capture_replay_include_third_model_stage_without_sql(evidence):
    source_catalog,snapshot,raw,catalog,field,bundle=evidence
    step=source_step('上'); previous=metric_step('销售额'); scripted=ScriptedTransport([previous,step]);seen=[]
    def transport(req):
        context=json.loads(json.loads(req.content)['messages'][1]['content'])
        if 'candidates' not in context: return scripted(req)
        seen.append(context)
        decision=dict(status='ACCEPTED',candidate_id=next(c['candidate_id'] for c in context['candidates'] if c['value']=='上海'))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(decision)}}]})
    case=dict(case_id='PROBE1',history=[previous[0]],current_utterance=step[0],clock='2026-09-09T09:00:00+08:00',
        scope=catalog['scope'],catalog_ref=catalog['artifact_hash'],initial_pending=False,dataset=None,
        labels={'canonical_metrics':['METRIC:amount']},safety_checks=[],catalog_evidence=['METRIC:amount'],
        business_evidence=['CONTROLLED_VALUE_PROBE'],label_status='REVIEWED_FOR_LISTED_AXES')
    settings=Settings(_env_file=None,intent_model_name='fixture-model',intent_model_base_url='https://model.invalid/v1',intent_model_api_key='test')
    captures=[];reads=len(source_catalog[6])
    result=await run([case],catalog,raw,settings,transport=httpx.MockTransport(transport),private_capture=captures.append,
        source_observations=bundle,source_observations_hash=bundle['artifact_hash'])
    assert result['predictions'][0]['status']=='OK',result['turns']
    assert len(seen)==1 and len(captures[-1]['outputs'])==3
    receipt,typed=await replay_turn(captures[-1],case,catalog,raw,expected_capture_hash=digest(captures[-1]),
        source_observations=bundle,source_observations_hash=bundle['artifact_hash'])
    assert receipt['failure'] is None and typed is not None
    assert len(source_catalog[6])==reads and receipt['source_SQL_executions']==0
    assert typed.model_dump(mode='json')==captures[-1]['result']


def test_collector_checks_empty_exact_before_probe_and_retains_mode(evidence,monkeypatch):
    import catalog_release
    source_catalog,snapshot,raw,catalog,field,_=evidence
    monkeypatch.setattr(catalog_release,'capture_catalog',lambda *args:deepcopy(snapshot))
    from pathlib import Path
    query=dict(attribute_id=field['attribute_id'],query='上',limit=64,mode='CANDIDATE_DISCOVERY')
    bundle=capture(raw,catalog,[query],service_root=Path('unused'),allow_source_reads=True)
    assert bundle['entries'][0]['observation']['match_mode']=='CANDIDATE_DISCOVERY'
    query['query']='上海'
    with pytest.raises(ValueError,match='REQUIRES_EMPTY_EXACT'):
        capture(raw,catalog,[query],service_root=Path('unused'),allow_source_reads=True)
