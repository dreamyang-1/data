"""Raw request -> mocked model HTTP -> actual pinned catalog/reducer/compiler.

Recorded model responses test orchestration and guards, not model accuracy.
"""
from copy import deepcopy
from datetime import datetime
import json

import httpx
import pytest

from app.config import Settings
from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.recognition_client import RecognitionFailure, RecognitionModelClient
from test_v2_authorized_catalog_bridge import IDENTITY, request, authority, publish, reseal, system

NOW = datetime.fromisoformat('2026-09-08T12:00:00+08:00')


@pytest.fixture
def catalog():
    service,store,registry,redis,_,overrides=system()
    source=authority()
    source['documents'][0]['metrics'].extend([
        dict(metric_code='quantity',metric_name='销售数量',business_domain=205,formula='SUM(quantity)'),
        dict(metric_code='orders',metric_name='订单笔数',business_domain=205,formula='COUNT(order_id)')])
    overrides[(81,(205,))]=reseal(source)
    publish(service)
    return service,store,registry,redis,overrides


def parse(text, specs=(), *, follow=False, history=False, shape=None):
    mentions=[];slots={};markers=[]
    for index,(surface,role,slot,operation) in enumerate(specs):
        identifier='m'+str(index);start=text.index(surface)
        mentions.append(dict(mention_id=identifier,surface=surface,normalized_surface=surface,start_char=start,
            end_char=start+len(surface),candidate_roles=[role],source_turn_id='filled-by-transport'))
        if slot:slots.setdefault(slot,[]).append(identifier)
        if operation:markers.append(dict(mention_id=identifier,operation_hint=operation,slot_name=slot))
    return dict(mentions=mentions,explicit_slot_mentions=slots,operation_markers=markers,
        reference_signals=['HISTORICAL'] if history else ['ELLIPSIS'] if follow else [],query_shape_prediction=shape)


def binding(context, name, role, mention='m0', kind=None):
    found=[c for c in context['catalog_candidates'] if c['name']==name and c['role']==role
        and c['mention_id']==mention and (kind is None or c['catalog_type']==kind)]
    assert len(found)==1,found
    return {'binding_handle':found[0]['binding_handle']}


def edit(slot,value,operation='SET',ids=('m0',)):
    return dict(slot_path=slot,operation=operation,evidence_mention_ids=list(ids),value=value)


def metric_step(text,name='销售额',operation='SET',follow=False):
    return (text,parse(text,[(name,'MEASURE','metrics',operation)],follow=follow),
        lambda c:dict(payload_type='INHERIT' if follow else 'SCALAR_AGGREGATE',
            edits=[edit('metrics',[binding(c,name,'MEASURE')],operation)]))


class ScriptedTransport:
    def __init__(self,steps):self.steps=steps;self.calls=[];self.next=0
    def __call__(self,req):
        body=json.loads(req.content);context=json.loads(body['messages'][1]['content'])
        self.calls.append(body)
        if 'mention' in context and 'candidates' in context:
            # This legacy fixture has no alias ground truth. A bounded probe
            # cannot manufacture a match; specific choices use their own fixture.
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps({'status':'REJECTED','candidate_id':None})}}]})
        question,parsed,draft=self.steps[self.next]
        assert context['question']==question
        if 'parse' not in context:
            data=deepcopy(parsed)
            if 'task_context' in context:
                from context_fixture_contract import fixture_proposal
                data['context_proposal']=fixture_proposal(data,context)
            for mention in data.get('mentions',[]):mention['source_turn_id']=context['turn_id']
        else:
            data=draft(context) if callable(draft) else deepcopy(draft);self.next+=1
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]})


def planner(catalog,steps):
    transport=ScriptedTransport(steps)
    settings=Settings(_env_file=None,intent_model_base_url='https://model.invalid/v1',
        intent_model_api_key='test-only-key',intent_model_name='existing-configured-model',intent_model_max_retries=0)
    client=RecognitionModelClient(settings,httpx.MockTransport(transport))
    return RawTurnPlanner(client,catalog[0],clock=lambda:NOW),transport


async def turns(engine,steps):
    state=None;plans={};results=[]
    for index,(text,_,_) in enumerate(steps):
        result=await engine.run(request(question=text,message_id='turn'+str(index)),IDENTITY,state=state,plans=tuple(plans.values()))
        state=result.next_state;plans[result.plan['logical_plan']['task_id']]=result.plan_state;results.append(result)
    return results


@pytest.mark.asyncio
async def test_raw_input_reaches_model_catalog_and_plan(catalog):
    steps=[metric_step('销售额')];engine,transport=planner(catalog,steps)
    result=(await turns(engine,steps))[0]
    assert result.plan['logical_plan']['payload']['measures'][0]['canonical_code']=='amount'
    assert result.plan['backend_contract']['mode']=='SHADOW_ONLY'
    assert len(transport.calls)==2
    assert all(c['model']=='existing-configured-model' and c['temperature']==0 for c in transport.calls)
    assert 'tasks' not in json.loads(transport.calls[0]['messages'][1]['content'])
    assert result.next_state.context.authorized_scope.business_domain_ids==(205,)


@pytest.mark.asyncio
async def test_unique_surface_span_repair_reaches_scoped_plan_and_preserves_add(catalog,caplog):
    steps=[metric_step('查询销售额'),metric_step('再加订单笔数','订单笔数','ADD',True)]
    for _,parsed,_ in steps:
        mention=parsed['mentions'][0]
        mention['start_char']=0;mention['end_char']=len(mention['surface'])
    original=deepcopy([parsed for _,parsed,_ in steps])
    engine,transport=planner(catalog,steps)
    with caplog.at_level('INFO',logger='app.semantic_v2.recognition'):
        results=await turns(engine,steps)
    assert {r['canonical_code'] for r in results[-1].plan['logical_plan']['payload']['measures']}=={'amount','orders'}
    assert results[-1].plan['backend_contract']['mode']=='SHADOW_ONLY'
    assert results[-1].next_state.context.authorized_scope.business_domain_ids==(205,)
    assert len(transport.calls)==4
    first_schema=json.loads(transport.calls[0]['messages'][0]['content'].split('JSON Schema:\n',1)[1])
    assert first_schema['properties']['explicit_slot_mentions']['additionalProperties'] is False
    assert 'limit' not in first_schema['properties']['explicit_slot_mentions']['properties']
    assert [parsed for _,parsed,_ in steps]==original
    traces=[r.parse_repairs for r in caplog.records if hasattr(r,'parse_repairs')]
    assert len(traces)==2
    assert all('销售额' not in json.dumps(trace,ensure_ascii=False) for trace in traces)


@pytest.mark.asyncio
async def test_add_replace_remove_apply_to_materialized_multiturn_state(catalog):
    steps=[metric_step('销售额'),metric_step('再加销售数量','销售数量','ADD',True),
        metric_step('再加订单笔数','订单笔数','ADD',True),metric_step('不要订单笔数','订单笔数','REMOVE',True),
        metric_step('换成销售数量','销售数量','REPLACE',True)]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert [{r['canonical_code'] for r in result.plan['logical_plan']['payload']['measures']} for result in results]==[
        {'amount'},{'amount','quantity'},{'amount','quantity','orders'},{'amount','quantity'},{'quantity'}]
    assert len({r.plan['logical_plan']['task_id'] for r in results})==1


def filtered_step():
    text='上海销售额'
    return (text,parse(text,[('上海','FILTER_FIELD','filter_expression','SET'),('销售额','MEASURE','metrics','SET')]),
        lambda c:dict(payload_type='SCALAR_AGGREGATE',edits=[edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',)),
            edit('filter_expression',dict(node_type='PREDICATE',field_ref=binding(c,'城市','FILTER_FIELD',kind='ATTRIBUTE'),operator='EQ',
                value=dict(value_type='STRING',value='上海'),source='USER_EXPLICIT',scope='CURRENT_TASK'))]))


@pytest.mark.asyncio
async def test_recorded_single_item_add_shape_preserves_multiple_metrics_and_followup(catalog):
    # The live 81/205 model emitted two ADD edits, each with one offered handle,
    # rather than an array. Replay that representation with independent fixtures.
    text='销售额和销售数量'
    first=(text,parse(text,[('销售额','MEASURE','metrics','ADD'),('销售数量','MEASURE','metrics','ADD')]),
        lambda c:dict(payload_type='SCALAR_AGGREGATE',edits=[
            edit('metrics',binding(c,'销售额','MEASURE','m0'),'ADD',('m0',)),
            # Recorded model copied the quantity handle offered for m0, while
            # its declared quantity evidence was m1. Identity itself is correct.
            edit('metrics',binding(c,'销售数量','MEASURE','m0'),'ADD',('m1',))]))
    next_text='再加订单笔数'
    second=(next_text,parse(next_text,[('订单笔数','MEASURE','metrics','ADD')],follow=True),
        lambda c:dict(payload_type='INHERIT',edits=[edit('metrics',binding(c,'订单笔数','MEASURE'),'ADD')]))
    results=await turns(planner(catalog,[first,second])[0],[first,second])
    assert {m['canonical_code'] for m in results[0].plan['logical_plan']['payload']['measures']}=={'amount','quantity'}
    assert {m['canonical_code'] for m in results[1].plan['logical_plan']['payload']['measures']}=={'amount','quantity','orders'}
    assert results[0].plan['logical_plan']['task_id']==results[1].plan['logical_plan']['task_id']
    assert results[1].plan['backend_contract']['mode']=='SHADOW_ONLY'
    assert results[1].next_state.context.authorized_scope.business_domain_ids==(205,)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['ADD','REMOVE'])
@pytest.mark.parametrize('slot',['metrics','dimensions'])
async def test_single_item_collection_edit_matches_array_and_preserves_other_state(catalog,operation,slot):
    initial_text='销售额和订单笔数'+('按城市分组' if slot=='dimensions' and operation=='REMOVE' else '')
    specs=[('销售额','MEASURE','metrics','SET'),('订单笔数','MEASURE','metrics','SET')]
    if slot=='dimensions' and operation=='REMOVE':specs.append(('城市','GROUP_BY','dimensions','SET'))
    def first_draft(c):
        edits=[edit('metrics',[binding(c,'销售额','MEASURE','m0'),binding(c,'订单笔数','MEASURE','m1')],ids=('m0','m1'))]
        if len(specs)==3:edits.append(edit('dimensions',[binding(c,'城市','GROUP_BY','m2')],ids=('m2',)))
        return dict(payload_type='GROUPED_AGGREGATE' if len(specs)==3 else 'SCALAR_AGGREGATE',edits=edits)
    first=(initial_text,parse(initial_text,specs),first_draft)
    surface=('销售数量' if operation=='ADD' else '订单笔数') if slot=='metrics' else '城市'
    role='MEASURE' if slot=='metrics' else 'GROUP_BY'
    current=('再加' if operation=='ADD' else '不要')+surface
    parsed=parse(current,[(surface,role,slot,operation)],follow=True)
    observed=[]
    for singleton in (False,True):
        def second_draft(c):
            value=binding(c,surface,role)
            kind='GROUPED_AGGREGATE' if slot=='dimensions' and operation=='ADD' else 'SCALAR_AGGREGATE'
            return dict(payload_type=kind,edits=[edit(slot,value if singleton else [value],operation)])
        second=(current,parsed,second_draft)
        result=(await turns(planner(catalog,[first,second])[0],[first,second]))[-1]
        payload=result.plan['logical_plan']['payload']
        observed.append(({m['canonical_code'] for m in payload['measures']},{d['canonical_code'] for d in payload.get('group_by',[])}))
    assert observed[0]==observed[1]
    assert observed[1][0]==({'amount','orders','quantity'} if slot=='metrics' and operation=='ADD' else {'amount'} if slot=='metrics' else {'amount','orders'})
    assert observed[1][1]==({'city'} if slot=='dimensions' and operation=='ADD' else set())


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,reason',[('SET','V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'),
    ('REPLACE','V2_CONTRACT_VALIDATION_FAILURE')])
async def test_assignment_still_requires_the_complete_collection(catalog,operation,reason):
    step=metric_step('销售额',operation=operation)
    bad=(step[0],step[1],lambda c:dict(payload_type='SCALAR_AGGREGATE',edits=[edit('metrics',binding(c,'销售额','MEASURE'),operation)]))
    with pytest.raises(RecognitionFailure,match=reason):
        await turns(planner(catalog,[bad])[0],[bad])


@pytest.mark.asyncio
@pytest.mark.parametrize('slot',['metrics','dimensions'])
async def test_single_offered_replacement_matches_a_complete_single_item_collection(catalog,slot):
    # Oracle binding: both variants select the same real current-scope catalog
    # candidate; only the captured model's list-vs-singleton representation varies.
    if slot=='metrics':
        first=metric_step('销售额');second=metric_step('再加销售数量','销售数量','ADD',True)
        prefix=[first,second];name,role='订单笔数','MEASURE'
    else:
        source=deepcopy(catalog[4][(81,(205,))]);doc=source['documents'][0]
        doc['entities'][0]['attributes'].append(dict(attribute_id=1207,attr_code='order_date',attr_name='订单日期',field_mapping='hospitals.order_date'))
        source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=1207,field_name='order_date',table_id=1))
        doc['dimensions'].append(dict(dim_code='order_date',dim_name='订单日期',bind_entities=[dict(entity='205',attr='1207',businessDomain='205')]))
        catalog[4][(81,(205,))]=reseal(source);publish(catalog[0],publication_id='replacement-oracle')
        text='按城市看销售额'
        first=(text,parse(text,[('城市','GROUP_BY','dimensions','SET'),('销售额','MEASURE','metrics','SET')]),
            lambda c:dict(payload_type='GROUPED_AGGREGATE',edits=[edit('dimensions',[binding(c,'城市','GROUP_BY')]),
                edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',))]))
        prefix=[first];name,role='订单日期','GROUP_BY'
    text='换成'+name;parsed=parse(text,[(name,role,slot,'REPLACE')],follow=True)
    observed=[]
    for singleton in (False,True):
        def draft(c):
            value=binding(c,name,role)
            return dict(payload_type='INHERIT',edits=[edit(slot,value if singleton else [value],'REPLACE')])
        steps=[*prefix,(text,parsed,draft)];result=(await turns(planner(catalog,steps)[0],steps))[-1]
        payload=result.plan['logical_plan']['payload'];op=result.resolution['task_patch']['replacements'][0]
        assert isinstance(op['new_value'],list) and len(op['new_value'])==1
        assert op['operation']=='REPLACE'
        if singleton:assert op['reason_code']=='CURRENT_TURN_SINGLETON_REPLACEMENT'
        observed.append(([m['display_name'] for m in payload['measures']],[d['display_name'] for d in payload.get('group_by',[])]))
    assert observed[0]==observed[1]
    expected=(['订单笔数'],[]) if slot=='metrics' else (['销售额'],['订单日期'])
    assert observed[1]==expected


@pytest.mark.asyncio
async def test_single_replacement_and_add_have_different_state_semantics(catalog):
    results=[]
    for operation in ('ADD','REPLACE'):
        text='修改订单笔数';parsed=parse(text,[('订单笔数','MEASURE','metrics',operation)],follow=True)
        current=(text,parsed,lambda c:dict(payload_type='INHERIT',edits=[edit('metrics',binding(c,'订单笔数','MEASURE'),operation)]))
        steps=[metric_step('销售额'),current]
        result=(await turns(planner(catalog,steps)[0],steps))[-1]
        results.append({m['canonical_code'] for m in result.plan['logical_plan']['payload']['measures']})
    assert results==[{'amount','orders'},{'orders'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,reason',[
    ('foreign','V2_BINDING_HANDLE_NOT_OFFERED'),('extra','V2_BINDING_HANDLE_NOT_OFFERED'),
    ('authority','V2_MODEL_AUTHORITY_FIELD_FORBIDDEN'),('role','V2_SLOT_ROLE_CONFLICT'),
    ('evidence','V2_BINDING_OUTSIDE_EDIT_EVIDENCE'),('null','V2_CONTRACT_VALIDATION_FAILURE'),
    ('string','V2_CONTRACT_VALIDATION_FAILURE'),('operation','V2_SLOT_OPERATION_CONFLICT')])
async def test_single_replacement_keeps_catalog_role_evidence_and_operation_guards(catalog,fault,reason):
    text='订单笔数销售数量';parsed=parse(text,[('订单笔数','MEASURE','metrics','REPLACE'),('销售数量','MEASURE',None,None)],follow=True)
    if fault=='role':parsed['mentions'][0]['candidate_roles']=['GROUP_BY']
    def draft(c):
        value=({'binding_handle':'not-offered'} if fault=='foreign' else
            {'canonical_id':'invented'} if fault=='authority' else
            binding(c,'城市','GROUP_BY') if fault=='role' else
            binding(c,'销售数量','MEASURE','m1') if fault=='evidence' else
            None if fault=='null' else 'unbound-name' if fault=='string' else binding(c,'订单笔数','MEASURE'))
        if fault=='extra':value['business_domain_ids']=[206]
        return dict(payload_type='INHERIT',edits=[edit('metrics',value,'ADD' if fault=='operation' else 'REPLACE')])
    steps=[metric_step('销售额'),(text,parsed,draft)]
    with pytest.raises(RecognitionFailure,match=reason):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
async def test_singleton_set_is_not_inferred_to_be_a_followup_replace(catalog):
    text='订单笔数';parsed=parse(text,[(text,'MEASURE','metrics','SET')],follow=True)
    step=(text,parsed,lambda c:dict(payload_type='INHERIT',edits=[edit('metrics',binding(c,'订单笔数','MEASURE'),'SET')]))
    steps=[metric_step('销售额'),step]
    with pytest.raises(RecognitionFailure,match='V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['ADD','REMOVE'])
@pytest.mark.parametrize('fault',['foreign_handle','wrong_role','outside_evidence','null_value'])
async def test_single_item_edits_keep_handle_role_and_current_evidence_guards(catalog,operation,fault):
    text='销售额销售数量'
    parsed=parse(text,[('销售额','MEASURE','metrics',operation),('销售数量','MEASURE','metrics',None)])
    if fault=='wrong_role':parsed['mentions'][0]['candidate_roles']=['GROUP_BY']
    def draft(c):
        value=({'binding_handle':'not-offered'} if fault=='foreign_handle' else
            binding(c,'城市','GROUP_BY') if fault=='wrong_role' else
            binding(c,'销售数量','MEASURE','m1') if fault=='outside_evidence' else None)
        return dict(payload_type='SCALAR_AGGREGATE',edits=[edit('metrics',value,operation)])
    expected={'foreign_handle':'V2_BINDING_HANDLE_NOT_OFFERED','wrong_role':'V2_SLOT_ROLE_CONFLICT',
        'outside_evidence':'V2_BINDING_OUTSIDE_EDIT_EVIDENCE','null_value':'V2_CONTRACT_VALIDATION_FAILURE'}[fault]
    with pytest.raises(RecognitionFailure,match=expected):
        await turns(planner(catalog,[(text,parsed,draft)])[0],[(text,parsed,draft)])


@pytest.mark.asyncio
async def test_clear_filter_survives_next_add(catalog):
    clear=('不限地区',parse('不限地区',[('不限地区','FILTER_FIELD','filter_expression','CLEAR')],follow=True),
        dict(payload_type='INHERIT',edits=[edit('filter_expression',None,'CLEAR')]))
    steps=[filtered_step(),clear,metric_step('再加销售数量','销售数量','ADD',True)]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert results[0].plan['logical_plan']['payload']['filters']['value']['value']=='上海'
    for r in results[1:]:
        assert r.plan['logical_plan']['payload']['filters'] is None
        assert 'filter_expression' in next(iter(r.next_state.payload['tasks'].values()))['clear_barriers']


@pytest.mark.asyncio
async def test_new_task_does_not_inherit_region_or_metric(catalog):
    steps=[filtered_step(),metric_step('销售数量','销售数量')]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert results[1].plan['logical_plan']['task_id']!=results[0].plan['logical_plan']['task_id']
    assert results[1].plan['logical_plan']['payload']['filters'] is None
    assert [r['canonical_code'] for r in results[1].plan['logical_plan']['payload']['measures']]==['quantity']


@pytest.mark.asyncio
async def test_historical_task_selected_only_from_scoped_offer(catalog):
    history=('回到销售额，再加订单笔数',parse('回到销售额，再加订单笔数',[('订单笔数','MEASURE','metrics','ADD')],history=True),
        lambda c:dict(payload_type='INHERIT',historical_task_handle=next(t['task_handle'] for t in c['tasks'] if t['slot_labels']['metrics']==['销售额']),
            edits=[edit('metrics',[binding(c,'订单笔数','MEASURE')],'ADD')]))
    steps=[metric_step('销售额'),metric_step('销售数量','销售数量'),history]
    engine,_=planner(catalog,steps);results=await turns(engine,steps)
    assert results[2].plan['logical_plan']['task_id']==results[0].plan['logical_plan']['task_id']
    assert {r['canonical_code'] for r in results[2].plan['logical_plan']['payload']['measures']}=={'amount','orders'}


@pytest.mark.parametrize('historical,handles',[(False,[]),(False,['task:one']),(True,[]),(True,['task:one','task:two'])])
def test_semantic_generation_schema_exposes_only_admissible_history(historical,handles):
    from app.semantic_v2.recognition import SemanticTaskDraft, semantic_task_schema
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    before=SemanticTaskDraft.model_json_schema()
    parsed=CurrentTurnSemanticParse(reference_signals=['HISTORICAL'] if historical else [])
    schema=semantic_task_schema(parsed,dict.fromkeys(handles))
    field=schema['properties']['historical_task_handle']
    if historical and handles:
        assert field['anyOf']==[{'type':'string','enum':handles},{'type':'null'}]
    else:assert field=={'type':'null','default':None}
    assert SemanticTaskDraft.model_json_schema()==before


@pytest.mark.asyncio
async def test_generation_constraint_rejects_unoffered_history_before_runtime(catalog):
    first=metric_step('销售额');next_step=metric_step('再加订单笔数','订单笔数','ADD',True)
    def bad_draft(context):
        value=next_step[2](context)
        # Attack with the known prior handle even if the current model context
        # omits it. The runtime guard must remain effective independently.
        value['historical_task_handle']='task:'+contract_digest({'task':'task:turn0'})[:24]
        return value
    steps=[first,(next_step[0],next_step[1],bad_draft)]
    engine,transport=planner(catalog,steps)
    with pytest.raises(RecognitionFailure,match='V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'):
        await turns(engine,steps)
    schema=json.loads(transport.calls[-1]['messages'][0]['content'].split('JSON Schema:\n',1)[1])
    assert schema['properties']['historical_task_handle']=={'type':'null','default':None}


@pytest.mark.asyncio
async def test_followup_context_exposes_only_current_state_without_historical_handle(catalog):
    steps=[metric_step('销售额'),metric_step('销售数量','销售数量'),
        metric_step('再加订单笔数','订单笔数','ADD',True)]
    engine,transport=planner(catalog,steps);results=await turns(engine,steps)
    context=json.loads(transport.calls[-1]['messages'][1]['content'])
    assert len(context['tasks'])==1
    current=context['tasks'][0]
    assert current['reference_kind']=='CURRENT_TASK' and 'task_handle' not in current
    assert current['slot_labels']['metrics']==['销售数量']
    assert current['payload_type']=='SCALAR_AGGREGATE'
    assert {m['canonical_code'] for m in results[-1].plan['logical_plan']['payload']['measures']}=={'quantity','orders'}
    assert results[-1].plan['logical_plan']['task_id']==results[-2].plan['logical_plan']['task_id']


@pytest.mark.asyncio
async def test_complete_new_task_has_no_inheritable_task_context(catalog):
    steps=[filtered_step(),metric_step('订单笔数','订单笔数')]
    engine,transport=planner(catalog,steps);results=await turns(engine,steps)
    context=json.loads(transport.calls[-1]['messages'][1]['content'])
    assert context['tasks']==[]
    assert results[-1].plan['logical_plan']['payload']['filters'] is None
    assert results[-1].plan['logical_plan']['task_id']!=results[0].plan['logical_plan']['task_id']


@pytest.mark.asyncio
async def test_historical_context_retains_scoped_handles_and_recorded_plan_shapes(catalog):
    def historical(c):
        assert len(c['tasks'])==1  # Joint first-stage proposal already selected the historical task.
        assert all(t['reference_kind']=='HISTORICAL_CANDIDATE' and t['payload_type']=='SCALAR_AGGREGATE' for t in c['tasks'])
        handle=next(t['task_handle'] for t in c['tasks'] if t['slot_labels']['metrics']==['销售额'])
        return dict(payload_type='INHERIT',historical_task_handle=handle,edits=[edit('metrics',[binding(c,'订单笔数','MEASURE')],'ADD')])
    text='回到销售额，再加订单笔数'
    steps=[metric_step('销售额'),metric_step('销售数量','销售数量'),
        (text,parse(text,[('订单笔数','MEASURE','metrics','ADD')],history=True),historical)]
    results=await turns(planner(catalog,steps)[0],steps)
    assert results[-1].plan['logical_plan']['task_id']==results[0].plan['logical_plan']['task_id']


@pytest.mark.asyncio
async def test_clear_barrier_remains_visible_in_current_task_context(catalog):
    text='不限地区';clear=(text,parse(text,[(text,'FILTER_FIELD','filter_expression','CLEAR')],follow=True),
        dict(payload_type='INHERIT',edits=[edit('filter_expression',None,'CLEAR')]))
    steps=[filtered_step(),clear,metric_step('再加订单笔数','订单笔数','ADD',True)]
    engine,transport=planner(catalog,steps);results=await turns(engine,steps)
    context=json.loads(transport.calls[-1]['messages'][1]['content'])
    assert 'filter_expression' in context['tasks'][0]['cleared_slots']
    assert context['tasks'][0]['filter_targets']==[]
    assert results[-1].plan['logical_plan']['payload']['filters'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('malicious_handle',[False,True])
async def test_explicit_topic_shift_overrides_historical_offer(catalog,malicious_handle):
    step=metric_step('订单笔数','订单笔数')
    step[1].update(reference_signals=['HISTORICAL'],topic_shift_signals=['EXPLICIT_NEW_TASK'])
    def draft(c):
        assert c['tasks']==[]
        value=step[2](c)
        if malicious_handle:value['historical_task_handle']='task:'+contract_digest({'task':'task:turn0'})[:24]
        return value
    steps=[metric_step('销售额'),(step[0],step[1],draft)]
    engine,transport=planner(catalog,steps)
    if malicious_handle:
        with pytest.raises(RecognitionFailure,match='V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'):await turns(engine,steps)
    else:
        results=await turns(engine,steps)
        assert results[-1].plan['logical_plan']['task_id']!=results[0].plan['logical_plan']['task_id']
    schema=json.loads(transport.calls[-1]['messages'][0]['content'].split('JSON Schema:\n',1)[1])
    assert schema['properties']['historical_task_handle']=={'type':'null','default':None}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['REMOVE','CLEAR'])
async def test_agreed_destructive_edit_act_does_not_require_a_duplicate_followup_signal(catalog,operation):
    if operation=='REMOVE':
        first=metric_step('销售额');second=metric_step('再加订单笔数','订单笔数','ADD',True)
        text='不要订单笔数'
        parsed=parse(text,[('订单笔数','MEASURE','metrics','REMOVE')])
        parsed['dialogue_act_candidates']=['REMOVE']
        step=(text,parsed,lambda c:dict(payload_type='INHERIT',edits=[edit('metrics',binding(c,'订单笔数','MEASURE'),'REMOVE')]))
        steps=[first,second,step]
    else:
        text='不限地区';parsed=parse(text,[(text,'FILTER_FIELD','filter_expression','CLEAR')])
        parsed['dialogue_act_candidates']=['CLEAR']
        step=(text,parsed,dict(payload_type='INHERIT',edits=[edit('filter_expression',None,'CLEAR')]))
        steps=[filtered_step(),step,metric_step('再加订单笔数','订单笔数','ADD',True)]
    engine,transport=planner(catalog,steps);results=await turns(engine,steps)
    assert len({r.plan['logical_plan']['task_id'] for r in results})==1
    assert 'task_handle' not in json.loads(transport.calls[3 if operation=='CLEAR' else 5]['messages'][1]['content'])['tasks'][0]
    if operation=='REMOVE':assert [m['canonical_code'] for m in results[-1].plan['logical_plan']['payload']['measures']]==['amount']
    else:
        assert results[-1].plan['logical_plan']['payload']['filters'] is None
        assert 'filter_expression' in next(iter(results[-1].next_state.payload['tasks'].values()))['clear_barriers']


@pytest.mark.asyncio
@pytest.mark.parametrize('acts,operations,topic_shift,has_history,expected',[
    (['REMOVE'],['REMOVE'],False,True,'CURRENT_TASK'),
    (['CLEAR'],['CLEAR'],False,True,'CURRENT_TASK'),
    (['REMOVE','NEW_TASK'],['REMOVE'],False,True,'SELF_CONTAINED'),
    (['REMOVE'],['SET'],False,True,'SELF_CONTAINED'),
    ([],['REMOVE'],False,True,'SELF_CONTAINED'),
    (['REMOVE'],['REMOVE','SET'],False,True,'SELF_CONTAINED'),
    (['REMOVE'],[],False,True,'SELF_CONTAINED'),
    (['REMOVE'],['REMOVE'],True,True,'SELF_CONTAINED'),
    (['REMOVE'],['REMOVE'],False,False,'UNRESOLVED_REFERENCE')])
async def test_destructive_act_dependency_requires_agreement_and_respects_new_task(catalog,acts,operations,topic_shift,has_history,expected):
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse,CurrentTurnParser,TurnResolver
    from app.semantic_v2.state_machine import ConversationState
    from app.semantic_v2.slot_reducer import TaskPatch
    from app.semantic_v2.models import SemanticResolutionContract
    first=metric_step('销售额');result=(await turns(planner(catalog,[first])[0],[first]))[0]
    state=(ConversationState.model_validate(result.next_state.payload) if has_history else
        ConversationState(state_version=0,**{k:result.next_state.payload[k] for k in ('conversation_id','tenant_id','user_id','application_id')}))
    parsed=parse('订单笔数',[('订单笔数','MEASURE','metrics',None)])
    parsed.update(dialogue_act_candidates=acts,topic_shift_signals=['EXPLICIT_NEW_TASK'] if topic_shift else [])
    parsed['mentions'][0]['source_turn_id']='oracle-turn'
    parsed['operation_markers']=[dict(mention_id='m0',slot_name='metrics',operation_hint=o) for o in operations]
    checked=CurrentTurnParser.parse(text='订单笔数',turn_id='oracle-turn',text_ref='oracle-turn',parsed=CurrentTurnSemanticParse.model_validate(parsed))
    resolution=TurnResolver.resolve(checked,state=state,task_patch=TaskPatch(base_task_version=0),semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))
    assert resolution.referential_completeness.relation==expected
    if expected=='CURRENT_TASK':assert resolution.target_task_id==result.plan['logical_plan']['task_id']
    if expected=='UNRESOLVED_REFERENCE':assert resolution.decision.decision_type!='PROCEED'


@pytest.mark.asyncio
async def test_field_lineage_does_not_require_metric(catalog):
    text='城市字段来自哪个表'
    step=(text,parse(text,[('城市','PROJECTION_FIELD','projection_spec','SET')],shape='LINEAGE_GRAPH'),
        lambda c:dict(payload_type='LINEAGE',edits=[edit('projection_spec',dict(items=[dict(output_field_id='field:city',
            ref=binding(c,'城市','PROJECTION_FIELD',kind='ATTRIBUTE'),role='PROJECTION_FIELD',position=0)]))]))
    engine,_=planner(catalog,[step]);result=(await turns(engine,[step]))[0]
    assert result.plan['logical_plan']['payload']['lineage_target']['target_type']=='FIELD'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code',[
    ('operation','V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'),('evidence','V2_EDIT_EVIDENCE_NOT_CURRENT'),
    ('handle','V2_BINDING_HANDLE_NOT_OFFERED'),('authority','V2_MODEL_DYNAMIC_SCHEMA_VIOLATION'),
    ('dropped','V2_EXPLICIT_OPERATION_DROPPED'),('unresolved','V2_RECOGNITION_UNRESOLVED'),
    ('shape','V2_QUERY_SHAPE_CONFLICT'),('history','V2_MODEL_DYNAMIC_SCHEMA_VIOLATION')])
async def test_model_output_cannot_bypass_deterministic_guards(catalog,fault,code):
    text,parsed,base=metric_step('销售额')
    if fault=='shape':parsed['query_shape_prediction']='RANKING'
    def corrupt(context):
        value=base(context)
        if fault=='operation':value['edits'][0]['operation']='ADD'
        if fault=='evidence':value['edits'][0]['evidence_mention_ids']=['old-turn-mention']
        if fault=='handle':value['edits'][0]['value']=[{'binding_handle':'foreign'}]
        if fault=='authority':value['edits'][0]['value']=[{'canonical_id':'invented'}]
        if fault=='dropped':value['edits']=[]
        if fault=='unresolved':value['unresolved_mention_ids']=['m0']
        if fault=='history':value['historical_task_handle']='foreign'
        return value
    step=(text,parsed,corrupt);engine,_=planner(catalog,[step])
    with pytest.raises(ValueError,match=code):await turns(engine,[step])


@pytest.mark.asyncio
async def test_scope_change_rejected_before_history_reaches_model(catalog):
    steps=[metric_step('销售额')];engine,transport=planner(catalog,steps);result=(await turns(engine,steps))[0]
    artifact=result.next_state.model_dump(mode='json')
    artifact['context']['authorized_scope']['knowledge_base_names']=['foreign']
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        await engine.run(request(question='销售额',message_id='new'),IDENTITY,state=ScopedArtifact.model_validate(artifact))
    assert len(transport.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['MODEL_TRANSPORT','MODEL_OUTPUT','MODEL_INCOMPLETE','PARSE_SPAN'])
async def test_model_system_failures_are_bounded_and_never_user_asks(catalog,kind):
    calls=[]
    def transport(req):
        calls.append(req)
        if kind=='MODEL_TRANSPORT':return httpx.Response(503)
        if kind=='MODEL_OUTPUT':return httpx.Response(200,json={'choices':[]})
        if kind=='MODEL_INCOMPLETE':return httpx.Response(200,json={'choices':[dict(finish_reason='length',message=dict(content='{}'))]})
        data=parse('销售额',[('销售额','MEASURE','metrics','SET')]);data['mentions'][0]['source_turn_id']='turn0';data['mentions'][0]['surface']='wrong'
        return httpx.Response(200,json={'choices':[dict(message=dict(content=json.dumps(data)))]})
    settings=Settings(_env_file=None,intent_model_api_key='test-only-key',intent_model_base_url='https://model.invalid/v1',intent_model_max_retries=0)
    engine=RawTurnPlanner(RecognitionModelClient(settings,httpx.MockTransport(transport)),catalog[0],clock=lambda:NOW)
    with pytest.raises(ValueError,match='V2_MODEL_|SURFACE_PARSE_FAILURE'):
        await engine.run(request(question='销售额',message_id='turn0'),IDENTITY)
    assert len(calls)==1


def envelope_with_payload(artifact,payload):
    return ScopedArtifact(kind=artifact.kind,context=artifact.context,payload=payload,payload_digest=contract_digest(payload))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['LIMIT','GLOBAL_TOP_N'])
async def test_existing_dataset_limit_is_local_and_global_rank_is_rejected(catalog,operation):
    first=metric_step('销售额')
    def draft(c):
        return dict(payload_type='DATASET_TRANSFORM',dataset_handle=c['datasets'][0]['dataset_handle'],
            dataset_operation=dict(operation_type=operation,limit=5))
    follow=('只看前5条',parse('只看前5条',[('5','LIMIT',None,None)],follow=True,shape='DATASET_TRANSFORM'),draft)
    engine,transport=planner(catalog,[first,follow])
    initial=await engine.run(request(question='销售额',message_id='turn0'),IDENTITY)
    raw=deepcopy(initial.next_state.model_dump(mode='json')['payload']);task=next(iter(raw['tasks'].values()))
    raw['datasets']['dataset1']=dict(dataset_id='dataset1',task_id=task['task_id'],task_version=task['active_version'])
    task['last_dataset_id']='dataset1'
    scoped=envelope_with_payload(initial.next_state,raw)
    async def invoke():return await engine.run(request(question=follow[0],message_id='turn1'),IDENTITY,state=scoped,plans=[initial.plan_state])
    if operation=='GLOBAL_TOP_N':
        with pytest.raises(RecognitionFailure,match='V2_CONTRACT_VALIDATION_FAILURE'):await invoke()
    else:
        result=await invoke()
        assert result.plan['execution_backend']=='DATASET_LOCAL'
        assert result.plan['logical_plan']['payload']['operation']==dict(operation_type='LIMIT',limit=5,preserve_existing_order=True)
        assert result.plan['backend_contract']['mode']=='SHADOW_ONLY'
    assert len(transport.calls)==4


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_new',[True,False])
async def test_live_pending_never_silently_becomes_wrong_new_task(catalog,explicit_new):
    first=metric_step('销售额');second=metric_step('销售数量','销售数量')
    if explicit_new:second[1]['topic_shift_signals']=['EXPLICIT_NEW_TASK']
    engine,transport=planner(catalog,[first,second])
    initial=await engine.run(request(question=first[0],message_id='turn0'),IDENTITY)
    raw=deepcopy(initial.next_state.model_dump(mode='json')['payload']);task=next(iter(raw['tasks'].values()))
    raw['pending_records']['pending1']=dict(pending_id='pending1',task_id=task['task_id'],task_version=task['active_version'],
        topic_id=task['topic_id'],slot_path='metrics',question='请选择指标',asked_at=NOW.isoformat())
    scoped=envelope_with_payload(initial.next_state,raw)
    async def invoke():return await engine.run(request(question=second[0],message_id='turn1'),IDENTITY,state=scoped,plans=[initial.plan_state])
    if explicit_new:
        result=await invoke()
        assert result.resolution['dialogue_act']=='NEW_TASK'
        assert result.next_state.payload['pending_records']['pending1']['status']=='SUSPENDED'
    else:
        with pytest.raises(RecognitionFailure,match='V2_CONTEXT_UNRESOLVED'):await invoke()


@pytest.mark.asyncio
async def test_grouping_and_ranking_survive_followup(catalog):
    text='按城市看销售额最高5名'
    specs=[('城市','GROUP_BY','dimensions','SET'),('销售额','MEASURE','metrics','SET'),
        ('销售额','ORDER_BY','ranking_spec','SET')]
    first=(text,parse(text,specs,shape='RANKING'),lambda c:dict(payload_type='RANKING',edits=[
        edit('dimensions',[binding(c,'城市','GROUP_BY','m0',kind='DIMENSION')]),
        edit('metrics',[binding(c,'销售额','MEASURE','m1')],ids=('m1',)),
        edit('ranking_spec',dict(rank_by=binding(c,'销售额','ORDER_BY','m2'),direction='DESC',limit=5),ids=('m2',))]))
    second=metric_step('再加销售数量','销售数量','ADD',True)
    engine,_=planner(catalog,[first,second]);results=await turns(engine,[first,second])
    assert all(r.plan['logical_plan']['payload']['payload_type']=='RANKING' for r in results)
    assert results[1].plan['logical_plan']['payload']['ranking']==results[0].plan['logical_plan']['payload']['ranking']


@pytest.mark.asyncio
async def test_unmarked_explicit_slot_cannot_disappear(catalog):
    text,parsed,base=metric_step('销售额');parsed['operation_markers']=[]
    step=(text,parsed,dict(payload_type='CHAT',edits=[]));engine,_=planner(catalog,[step])
    with pytest.raises(RecognitionFailure,match='V2_EXPLICIT_SLOT_DROPPED'):await turns(engine,[step])


@pytest.mark.asyncio
async def test_payload_cannot_discard_bound_filter(catalog):
    text,parsed,base=filtered_step()
    def changed(c):
        draft=base(c);draft['payload_type']='METRIC_DEFINITION';return draft
    engine,_=planner(catalog,[(text,parsed,changed)])
    with pytest.raises(RecognitionFailure,match='V2_PAYLOAD_WOULD_DROP_SEMANTICS'):
        await turns(engine,[(text,parsed,changed)])


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['missing_plan','foreign_plan','duplicate_message'])
async def test_prior_plan_and_message_identity_are_checked(catalog,fault):
    first=metric_step('销售额');second=metric_step('再加销售数量','销售数量','ADD',True)
    engine,_=planner(catalog,[first,second]);initial=await engine.run(request(question=first[0],message_id='turn0'),IDENTITY)
    plans=[] if fault=='missing_plan' else [initial.plan_state]
    if fault=='foreign_plan':
        raw=deepcopy(initial.plan_state.model_dump(mode='json')['payload']);raw['plan_id']='foreign-plan'
        plans=[envelope_with_payload(initial.plan_state,raw)]
    code={'missing_plan':'V2_PRIOR_PAYLOAD_IDENTITY_REQUIRED','foreign_plan':'V2_PRIOR_PLAN_STATE_MISMATCH',
        'duplicate_message':'V2_MESSAGE_ALREADY_PLANNED'}[fault]
    with pytest.raises(RecognitionFailure,match=code):
        await engine.run(request(question=second[0],message_id='turn0' if fault=='duplicate_message' else 'turn1'),
            IDENTITY,state=initial.next_state,plans=plans)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected_calls',[(429,2),(503,2),(400,1)])
async def test_existing_transport_retry_policy_is_bounded(status,expected_calls,monkeypatch):
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    import app.semantic_v2.recognition_client as module
    async def no_delay(_):pass
    monkeypatch.setattr(module.asyncio,'sleep',no_delay)
    calls=[]
    def handler(req):
        calls.append(req)
        if len(calls)==1:return httpx.Response(status)
        return httpx.Response(200,json={'choices':[dict(message=dict(content='{}'),finish_reason='stop')]})
    settings=Settings(_env_file=None,intent_model_api_key='test-only-key',intent_model_base_url='https://model.invalid/v1',intent_model_max_retries=1)
    client=RecognitionModelClient(settings,httpx.MockTransport(handler))
    if status==400:
        with pytest.raises(RecognitionFailure,match='V2_MODEL_TRANSPORT_FAILURE'):
            await client.complete(stage='parse',instruction='fixture',context={},output_model=CurrentTurnSemanticParse)
    else:
        assert await client.complete(stage='parse',instruction='fixture',context={},output_model=CurrentTurnSemanticParse)==CurrentTurnSemanticParse()
    assert len(calls)==expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('slot',['subject','projection_spec','filter_expression'])
async def test_handle_membership_does_not_override_slot_role(catalog,slot):
    text='销售额'
    parsed=parse(text,[(text,'MEASURE',slot,'SET')])
    def draft(c):
        ref=binding(c,'销售额','MEASURE')
        value=ref if slot=='subject' else dict(items=[dict(output_field_id='field',ref=ref,role='PROJECTION_FIELD',position=0)]) if slot=='projection_spec' else dict(
            node_type='PREDICATE',field_ref=ref,operator='EQ',value=dict(value_type='STRING',value='上海'),source='USER_EXPLICIT',scope='CURRENT_TASK')
        return dict(payload_type='DETAIL_ROWS',edits=[edit(slot,value)])
    step=(text,parsed,draft);engine,_=planner(catalog,[step])
    with pytest.raises(RecognitionFailure,match='V2_SLOT_ROLE_CONFLICT'):await turns(engine,[step])
