"""Frozen development context controls from existing regressions and user contract.

Actual planner/state/Pending, controlled recognition and synthetic governed catalog.
These eight cases are not live Gold accuracy, source-value quality or Whole Plan.
"""
from copy import deepcopy
from datetime import datetime
import json
import pytest
from app.semantic_v2.recognition import RawTurnPlanner
from tools.cutover.harness_observation import RuntimeObserver
from test_v2_raw_turn_recognition import IDENTITY,NOW,authority,binding,edit,parse,planner,publish,request,reseal,system,metric_step
from test_v2_structured_edits import scalar,filter_step,temporal_step
from test_v2_pending_recognition import ambiguous_step
from test_v2_operation_round1 import active


@pytest.fixture
def context_catalog():
    service,store,registry,redis,_,overrides=system();source=authority();doc=source['documents'][0]
    doc['metrics'][0]['synonyms']=['销售']
    doc['metrics'].extend([
        dict(metric_code='quantity',metric_name='销售数量',synonyms=['销售'],business_domain=205,formula='SUM(quantity)'),
        dict(metric_code='orders',metric_name='订单笔数',business_domain=205,formula='COUNT(order_id)')])
    # Reuse the existing raw/structured/catalog-plan fixture contract. Its 城市
    # member edits exercise generic region state preservation, not geographic grounding.
    for identifier,code,name,main in [(1207,'order_date','订单日期',False),(1210,'name','医院名称',True)]:
        doc['entities'][0]['attributes'].append(dict(attribute_id=identifier,attr_code=code,attr_name=name,
            is_main_attribute=main,field_mapping='hospitals.'+code))
        source['physical_catalog']['tables'][0]['fields'].append(dict(field_id=identifier,field_name=code,table_id=1))
    doc['dimensions'].append(dict(dim_code='order_date',dim_name='订单日期',bind_entities=[dict(entity='205',attr='1207',businessDomain='205')]))
    overrides[(81,(205,))]=reseal(source);publish(service)
    return service,store,registry,redis,overrides


def initial(text,regions=('上海',),metrics=('销售额',),year=2025):
    specs=[(r,'FILTER_FIELD','filter_expression','SET') for r in regions]
    specs.extend((m,'MEASURE','metrics','SET') for m in metrics)
    if year is not None:specs.append(('去年','TIME_FIELD','time_spec','SET'))
    def draft(c):
        offset=len(regions);mids=tuple('m'+str(offset+i) for i in range(len(metrics)))
        edits=[edit('metrics',[binding(c,m,'MEASURE',mid) for m,mid in zip(metrics,mids)],ids=mids)]
        if regions:
            edits.append(edit('filter_expression',dict(node_type='PREDICATE',field_ref=binding(c,'城市','FILTER_FIELD','m0','ATTRIBUTE'),
                operator='EQ' if len(regions)==1 else 'IN',value=scalar(regions[0]) if len(regions)==1 else
                dict(value_type='LIST',values=[scalar(r) for r in regions]),source='USER_EXPLICIT',scope='CURRENT_TASK'),
                ids=tuple('m'+str(i) for i in range(len(regions)))))
        if year is not None:
            mid='m'+str(len(specs)-1)
            edits.append(edit('time_spec',dict(anchor=binding(c,'订单日期','TIME_FIELD',mid,'DIMENSION'),
                range=dict(start=f'{year}-01-01T00:00:00+08:00',end_exclusive=f'{year+1}-01-01T00:00:00+08:00'),
                grain='NONE',timezone='Asia/Shanghai',source='USER_EXPLICIT',as_of=NOW.isoformat()),ids=(mid,)))
        return dict(payload_type='SCALAR_AGGREGATE',edits=edits)
    return text,parse(text,specs),draft


def context_case(index):
    first=initial('去年上海销售额')
    if index==1:return [first,filter_step('那江苏呢','REPLACE',scalar('江苏'))]
    if index==2:return [first,metric_step('再加订单笔数','订单笔数','ADD',True)]
    if index==3:return [initial('去年上海销售额和销售数量',metrics=('销售额','销售数量')),
        metric_step('不要销售数量','销售数量','REMOVE',True)]
    if index==4:
        last=temporal_step('换今年','RANGE','REPLACE',dict(start='2026-01-01T00:00:00+08:00',end_exclusive='2027-01-01T00:00:00+08:00'))
        last=(last[0],parse(last[0],[('今年','TIME_FIELD','time_spec','REPLACE')],follow=True),last[2])
        return [first,filter_step('不限地区','CLEAR'),last]
    if index==5:return [initial('上海和北京去年销售额',regions=('上海','北京')),filter_step('不要上海','REMOVE',scalar('上海'))]
    if index==6:return [initial('上海销售额',year=None),filter_step('换北京和江苏','REPLACE',
        dict(value_type='LIST',values=[scalar('北京'),scalar('江苏')]))]
    if index==7:
        history=('返回刚才上海任务',parse('返回刚才上海任务',history=True),
            lambda c:dict(payload_type='INHERIT',historical_task_handle=next(t['task_handle'] for t in c['tasks'] if t['filter_targets'])))
        return [initial('上海销售额',year=None),metric_step('医院销售额'),history]
    if index==8:
        text='江苏有哪些医院？';parsed=parse(text,[('江苏','FILTER_FIELD','filter_expression','SET'),('医院','SUBJECT_ENTITY','subject','SET')])
        parsed['dialogue_act_candidates']=['NEW_TASK']
        def draft(c):return dict(payload_type='DETAIL_ROWS',edits=[
            edit('subject',binding(c,'医院','SUBJECT_ENTITY','m1','ENTITY'),ids=('m1',)),
            edit('filter_expression',dict(node_type='PREDICATE',field_ref=binding(c,'城市','FILTER_FIELD','m0','ATTRIBUTE'),
                operator='EQ',value=scalar('江苏'),source='USER_EXPLICIT',scope='CURRENT_TASK'))])
        ask=ambiguous_step()
        def ask_draft(c):
            value=ask[2](c)
            offered={v['binding_handle'] for v in c['catalog_candidates'] if '销售' in v['aliases'] and v['role']=='MEASURE'}
            value['ambiguities'][0]['candidate_handles']=[h for h in value['ambiguities'][0]['candidate_handles'] if h in offered]
            return value
        return [(ask[0],ask[1],ask_draft),(text,parsed,draft)]
    raise ValueError(index)


@pytest.mark.asyncio
@pytest.mark.parametrize('case_index',range(1,9),ids=lambda i:f'CFCS-{i:02d}')
async def test_context_followup_critical_slice(context_catalog,case_index,record_property):
    steps=context_case(case_index);engine,transport=planner(context_catalog,steps)
    state=None;plans={};pending=None;results=[];observations=[]
    for i,(text,_,_) in enumerate(steps):
        before=deepcopy(state.model_dump(mode='json')) if state else None
        observer=RuntimeObserver()
        with observer:
            observer.begin_turn(f'CFCS-{case_index:02d}',i)
            result=await engine.run(request(question=text,message_id='turn'+str(i)),IDENTITY,state=state,
                plans=tuple(plans.values()),pending=pending)
        events=observer.turns[0]['events']
        resolutions=[e['output'] for e in events if e['stage']=='TurnResolutionInput' and e['output']]
        patches=[e['output'][0] for e in events if e['stage']=='TaskPatchInput' and e['output']]
        assert resolutions and patches
        if state:assert state.model_dump(mode='json')==before
        state=result.next_state;results.append(result)
        if result.plan:
            task_id=result.plan['logical_plan']['task_id'];plans[task_id]=result.plan_state;pending=None
            task,version=active(result)
            observations.append({'turn':i,'TurnResolution':result.resolution['dialogue_act'],'TargetTask':task_id,
                'TaskOperation':{'structured_edits':[t.model_dump(mode='json') for t in result.edit_trace],
                    'patch':result.resolution['task_patch']},'TaskPatch':result.resolution['task_patch'],
                'TaskSemanticState':version['semantics'],'ClarificationDecision':'PROCEED',
                'clear_barriers':task['clear_barriers'],'pending_records':state.payload['pending_records']})
        else:
            pending=result.pending_state
            task_id=pending.payload['task_id'];task=state.payload['tasks'][task_id]
            observations.append({'turn':i,'TurnResolution':resolutions[-1]['dialogue_act'],'TargetTask':task_id,
                'TaskOperation':{'applied':patches[-1],'status':'DEFERRED_PENDING'},'TaskPatch':patches[-1],
                'TaskSemanticState':task['versions'][-1]['semantics'],
                'ClarificationDecision':result.trace.model_dump(mode='json'),'pending_id':pending.payload['pending_id']})
        observations[-1]['observed_turn_resolution']=resolutions[-1]
        observations[-1]['observed_pre_default_task_patch']=patches[-1]
    last=results[-1];_,v=active(last);s=v['semantics']
    expected_regions={1:['江苏'],2:['上海'],3:['上海'],4:[],5:['北京'],6:['北京','江苏'],7:['上海'],8:['江苏']}[case_index]
    f=s['filter_expression'];value=f['value'] if f else None
    actual=([x['value'] for x in value['values']] if value and value['value_type']=='LIST' else [value['value']] if value else [])
    assert actual==expected_regions
    assert {m['canonical_code'] for m in s['metrics']}==({'amount','orders'} if case_index==2 else set() if case_index==8 else {'amount'})
    if case_index<=5:
        assert datetime.fromisoformat(s['time_spec']['range']['start']).astimezone(NOW.tzinfo).year==(2026 if case_index==4 else 2025)
    if case_index<=6:
        assert observations[0]['TargetTask']==observations[-1]['TargetTask']
    if case_index==4:assert 'filter_expression' in active(last)[0]['clear_barriers']
    if case_index==7:
        assert observations[-1]['TurnResolution']=='RETURN_TO_TOPIC'
        assert observations[-1]['TargetTask']==observations[0]['TargetTask']!=observations[1]['TargetTask']
    if case_index==8:
        assert results[0].trace.is_user_ambiguity and last.resolution['dialogue_act']=='NEW_TASK'
        assert observations[0]['TargetTask']!=observations[-1]['TargetTask']
        assert all(r['status']=='SUSPENDED' for r in state.payload['pending_records'].values())
    assert all(r.plan is not None for r in results[(1 if case_index==8 else 0):])
    record_property('context_slice_observation',json.dumps({'case_id':f'CFCS-{case_index:02d}',
        'history':[s[0] for s in steps[:-1]],'current_utterance':steps[-1][0],'turns':observations,
        'result':'PASS','WrongInheritance':False,'PendingHijack':False,'Resurrection':False,
        'evidence_kind':'CONTROLLED_RECOGNITION_NATIVE_RUNTIME; NOT_LIVE_GOLD',
        'clock':NOW.isoformat(),'scope':{'semantic_model_id':81,'business_domain_ids':[205]}}))
