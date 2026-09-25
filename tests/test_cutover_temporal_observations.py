import pytest

from test_v2_explicit_time import base_catalog, catalog, initial
from test_v2_raw_turn_recognition import planner,turns,metric_step,parse
from tools.cutover.evaluation_contract import digest
from tools.cutover.transition_observations import observe_v2_plan


def evaluation_context(result):
    catalog={'catalog_version':result.next_state.context.catalog_pin.catalog_version,
        'facts':[{'fact_id':'METRIC:amount'},{'fact_id':'METRIC:quantity'}]}
    catalog['artifact_hash']=digest(catalog)
    case={'case_id':'TIME_OBSERVATION_CONTROL','scope':{'semantic_model_id':81,'business_domain_ids':[205],
        'scope_mode':'EXPLICIT_DOMAINS'},'catalog_ref':catalog['artifact_hash']}
    return case,catalog


def temporal(text,component,operation,value=None):
    return text,parse(text,[(text,'TIME_RANGE' if component=='RANGE' else 'TIME_GRAIN','time_spec',operation)],follow=True),dict(
        payload_type='INHERIT',temporal_edits=[dict(component=component,operation=operation,value=value,evidence_mention_ids=['m0'])])


@pytest.mark.asyncio
async def test_time_observation_reads_actual_state_across_range_change_retention_and_clear(catalog):
    steps=[initial(),temporal('2026年','RANGE','REPLACE',dict(start='2026-01-01T00:00:00+08:00',end_exclusive='2027-01-01T00:00:00+08:00')),
        metric_step('再加销售数量','销售数量','ADD',True),temporal('不限时间','RANGE','CLEAR'),
        metric_step('不要销售数量','销售数量','REMOVE',True)]
    results=await turns(planner(catalog,steps)[0],steps)
    case,frozen=evaluation_context(results[-1]);actual=[]
    for index,result in enumerate(results):
        args=dict(catalog=frozen,mode='SCRIPTED_MODEL_PIPELINE',previous=results[index-1] if index else None,history=results[:index])
        clean=observe_v2_plan(result,case,**args)
        contaminated=observe_v2_plan(result,{**case,'labels':{'time_relation':'FABRICATED','time_grain':'FABRICATED'}},**args)
        assert clean==contaminated and clean['safety']=={}
        actual.append(clean['axes']['time_relation'])
    assert actual==['CHANGED','CHANGED','SAME_AS_PREVIOUS','ABSENT','ABSENT']


@pytest.mark.asyncio
async def test_grain_observation_does_not_claim_the_date_range_changed(catalog):
    steps=[initial(grain='MONTH'),temporal('按季度','GRAIN','REPLACE','QUARTER')]
    results=await turns(planner(catalog,steps)[0],steps)
    case,frozen=evaluation_context(results[-1])
    actual=observe_v2_plan(results[-1],case,catalog=frozen,mode='SCRIPTED_MODEL_PIPELINE',previous=results[0])
    assert actual['axes']['time_relation']=='SAME_AS_PREVIOUS' and actual['axes']['time_grain']=='QUARTER'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['digest','scope'])
async def test_temporal_history_comparison_rejects_unverified_prior_artifacts(catalog,fault):
    steps=[initial(),metric_step('再加销售数量','销售数量','ADD',True)]
    a,b=await turns(planner(catalog,steps)[0],steps)
    artifact=a.next_state
    if fault=='digest':artifact=artifact.model_copy(update={'payload_digest':'0'*64})
    else:artifact=artifact.model_copy(update={'context':artifact.context.model_copy(update={'state_namespace':'another-conversation'})})
    bad=a.model_copy(update={'next_state':artifact})
    case,frozen=evaluation_context(b)
    with pytest.raises(ValueError,match='V2_OBSERVATION_PREVIOUS_STATE_MISMATCH'):
        observe_v2_plan(b,case,catalog=frozen,mode='SCRIPTED_MODEL_PIPELINE',previous=bad)
