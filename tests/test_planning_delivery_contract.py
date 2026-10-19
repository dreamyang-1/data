"""Full task dispatch and public delivery contracts with synthetic business rows."""
import asyncio
from datetime import datetime, timezone
import json
import re
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import AgentResponse, ChatRequest, EvidenceItem, PrimaryIntent, TrustedIdentity
from app.planning import CompoundQuestionRouter
from app.services.progress import emit_progress, progress_scope
from test_api import TestClient, build_test_app
from test_v2_authorized_catalog_bridge import provider


@pytest.mark.parametrize('status',['NEEDS_CLARIFICATION','SAFE_FALLBACK','COMPLETED'])
def test_single_task_terminal_state_and_six_nodes_survive_early_stop(status):
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):
            await emit_progress('INTENT_RECOGNITION','COMPLETED','已识别数据问题')
            await emit_progress('TASK_PLANNING','COMPLETED','任务规划已就绪',task_table=[
                {'任务':'task-1','名称':'查询合作时长','是否查询数据库':'是','依赖':[],'执行状态':'待执行'}])
            await emit_progress('ASL_GENERATION','NEEDS_INPUT' if status=='NEEDS_CLARIFICATION' else 'COMPLETED','受控测试绑定结果')
            return {'response':AgentResponse(request_id=uuid4(),conversation_id=state['chat'].conversation_id,
                status=status,intent=PrimaryIntent.METRIC_QUERY,answer='测试业务状态')}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'terminal-nodes','message_id':status,'question':'查询合作时长'})
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    assert re.findall(r'#### ◉ ([^\r\n]+)',text)==['意图识别','任务拆分与规划','调度执行','结果校验','数据洞察分析','最终输出']
    tables=[event['task_table'] for event in events if event.get('task_table')]
    assert tables[-1][0]['执行状态']=={'NEEDS_CLARIFICATION':'待补充','SAFE_FALLBACK':'已失败','COMPLETED':'已完成'}[status]
    assert '本环节尚未执行' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_second',[False,True])
async def test_two_level_plan_executes_bound_threshold_then_answers_every_question(fail_second):
    from app.adapters import build_mock_adapters
    from app.intent import RuleBasedIntentClassifier
    from app.services.orchestrator import DataAnalysisOrchestrator
    from app.stores import InMemorySessionStore
    from minio_followup_store import DatasetScope
    from test_conversation_result_followup import _ResultStore
    from test_planning_integrity_contract import CATALOG

    first='查询合作时长大于12个月的经销商名单'
    second='统计2025年订单笔数'
    source='请分别回答：'+first+'；'+second
    async def handler(request):
        body=json.loads(request.content)
        if '问题拆分判断器' in body['messages'][0]['content']:
            result={'split':True,'sub_questions':[{'question':first},{'question':second}],
                'analyze_summary':'两项业务结果分别交付','split_reason_code':'EXPLICIT_SEPARATE_RESULTS'}
        elif first in body['messages'][1]['content']:
            result={'intent':'DETAIL_QUERY','tasks':[{'question':first,'extraction':{
                '意图':'明细查询','实体':['经销商'],'展示字段':[{'entity':'经销商','field':'经销商名称'}],
                '过滤条件':[{'entity':'经销商','field':'合作时长','op':'>','value':['12个月']}],
                '是否查询数据库':'是'}}]}
        else:
            result={'intent':'METRIC_QUERY','tasks':[{'question':second,'extraction':{
                '意图':'统计查询','实体':['销售订单'],'指标':[{'name':'订单笔数'}],
                '时间粒度':{'unit':'年','time_range':'2025年'},'是否查询数据库':'是'}}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
    settings=Settings(_env_file=None,env='test',multi_question_model_enabled=True,
        analysis_synthesis_enabled=False,intent_model_api_key=SecretStr('fixture-key'))
    router=CompoundQuestionRouter(settings,transport=httpx.MockTransport(handler))
    plan=(await router.route_and_plan(source,semantic_context=CATALOG)).plan
    assert plan is not None and len(plan.tasks)==3
    assert [group.task_count for group in plan.question_groups]==[2,1]
    agent=DataAnalysisOrchestrator(settings=settings,classifier=RuleBasedIntentClassifier(),
        task_planner=router,adapters=build_mock_adapters(),sessions=InMemorySessionStore(7200,7200))
    agent.dataset_store=_ResultStore()
    calls=[]
    async def query(child,identity):
        calls.append(('query_start',child.question))
        if '订单笔数' in child.question and fail_second:
            raise RuntimeError('合成订单服务失败')
        await asyncio.sleep(0)
        rows=([{'经销商名称':f'SYNTH-{value}','合作时长':value} for value in [11,12,13]]
              if '合作时长' in child.question else [{'订单笔数':7}])
        scope=DatasetScope(tenant_id=identity.tenant_id,user_id=identity.user_id,
            application_id=child.application_id,conversation_id=child.conversation_id,
            authorized_semantic_scope_fingerprint=child.authorized_semantic_scope.fingerprint())
        ref=agent.dataset_store.save_dataset(scope=scope,columns=list(rows[0]),rows=rows,snapshot_id='',
            data_as_of=datetime.now(timezone.utc),source_type='QUERY_RESULT',source_ref='synthetic-query',semantic_model_id=81)
        await agent.sessions.put_dataset_reference(ref.to_dict(),recent_limit=20)
        table=agent._markdown_result_table(list(rows[0]),rows)
        child._dag_deferred_insight={'summary':'合成查询结果已返回','facts':{},'presentation':{'table':table}}
        calls.append(('query_done',child.question))
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,status='COMPLETED',
            intent=PrimaryIntent.METRIC_QUERY,answer=table,dataset_id=ref.dataset_id,
            evidence=[EvidenceItem(evidence_id='synthetic',kind='QUERY_RESULT',source_ref=ref.dataset_id,
                payload={'row_count':len(rows),'returned_row_count':len(rows),'truncated':False})])
    agent._handle=query
    original=agent._respond_pure_computation
    async def computation(*args,**kwargs):
        assert any(kind=='query_done' and '合作时长' in text for kind,text in calls)
        calls.append(('compute',first))
        return await original(*args,**kwargs)
    agent._respond_pure_computation=computation
    chat=ChatRequest(application_id='app',conversation_id='dispatch-contract',message_id=str(uuid4()),
        semantic_model_id=81,question=source)
    with progress_scope(lambda event: None):
        response=await agent._handle_task_plan(chat,TrustedIdentity(tenant_id='t',user_id='u'),plan)
    assert response.status==('PARTIAL_SUCCESS' if fail_second else 'COMPLETED')
    assert [item.status for item in response.task_results]==['COMPLETED','COMPLETED','FAILED' if fail_second else 'COMPLETED']
    assert len([kind for kind,_ in calls if kind=='query_start'])==2
    assert len([kind for kind,_ in calls if kind=='compute'])==1
    assert '### 问题1：'+first in response.answer and '### 问题2：'+second in response.answer
    block=response.answer.split('### 问题2：',1)[0]
    assert 'SYNTH-13' in block and 'SYNTH-12' not in block and 'SYNTH-11' not in block
    assert response.answer.index('### 问题1：') < response.answer.index('### 问题2：')


def test_planner_threshold_error_never_asks_user_to_rephrase():
    from app.domain.models import CanonicalAnalysisRequest,SemanticAmbiguity
    from app.services.clarification_policy import decide_clarification
    from app.services.orchestrator import DataAnalysisOrchestrator
    request=CanonicalAnalysisRequest(tenant_id='t',user_id='u',conversation_id='system-failure',original_question='查询合作时长大于12个月的经销商名单',
        primary_intent=PrimaryIntent.METRIC_QUERY,missing_slots=['semantic_ambiguity'],
        semantic_ambiguities=[SemanticAmbiguity(type='context',question='需要修正任务规划',
            ambiguity_id='PLANNER_METRIC_THRESHOLD_RESULT_OPERATION',affected_slots=['task_plan'])])
    _,trace=decide_clarification(request,'semantic_ambiguity',source_stage='STRUCTURED_ASL_BINDING',asked_keys=set())
    assert trace.decision=='SUPPRESS' and trace.reason_type=='SYSTEM_FAILURE'
    assert trace.system_repair_possible and not trace.is_user_ambiguity
    answer=DataAnalysisOrchestrator._plain_binding_failure_answer(request)
    assert '换个说法' not in answer and '无需重复' in answer


@pytest.mark.parametrize('changed',[False,True])
def test_cross_question_ranking_reuses_only_an_identical_complete_query(changed):
    from app.domain.models import AtomicTask,TaskPlan
    from app.planning.dependency_contract import reuse_dependency_rankings
    extraction={'意图':'统计查询','是否查询数据库':'是','实体':['经销商'],'指标':[{'name':'订单笔数'}],
        '维度':['经销商'],'展示字段':[],'过滤条件':[],'时间粒度':{'unit':'年','time_range':'2025年'}}
    target={**extraction,'排序':[{'field':'订单笔数','order':'desc'}],'限制':1}
    if changed:target['时间粒度']={'unit':'年','time_range':'2024年'}
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id='task-1',question='统计2025年各经销商订单笔数',primary_intent=PrimaryIntent.METRIC_QUERY,extraction=extraction),
        AtomicTask(task_id='task-2',question='找出订单笔数最多的经销商',depends_on=['task-1'],
            primary_intent=PrimaryIntent.METRIC_QUERY,extraction=target)])
    result=reuse_dependency_rankings(plan).tasks[1]
    assert (result.extraction['是否查询数据库']=='否') is not changed
    if not changed:
        assert result.extraction['结果操作']=={'source_task_id':'task-1','operation':{'type':'pipeline','operations':[
            {'type':'sort','field':'订单笔数','descending':True},{'type':'limit','count':1}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',[None,'truncated','wrong_operation','no_plan'])
async def test_exact_planned_top_one_can_bind_a_new_query_without_claiming_population_coverage(fault):
    from app.domain.models import AtomicTask
    from app.planning.dependency_contract import dependency_selection_complete
    from minio_followup_store import dataset_source_complete
    from test_complex_execution_correctness import service,context,persist,IDENTITY
    agent=service();chat,_=context()
    source=await persist(agent,chat,['经销商编码','订单笔数'],
        [{'经销商编码':'SYNTH-A','订单笔数':1},{'经销商编码':'SYNTH-B','订单笔数':5}],
        source_row_count=2,truncated=fault=='truncated',column_bindings={'经销商编码':'dealer.dealer_code'})
    operation={'type':'pipeline','operations':[{'type':'sort','field':'订单笔数','descending':True},{'type':'limit','count':1}]}
    result=agent.dataset_store.execute_followup(source,current_scope=source.scope,operation=operation)
    loaded=agent.dataset_store.load_dataset(result.reference,current_scope=source.scope)
    assert loaded.rows==({'经销商编码':'SYNTH-B','订单笔数':5},)
    assert not dataset_source_complete(result.reference,1)
    planned={'结果操作':{'source_task_id':'query','operation':operation}}
    if fault=='wrong_operation':planned={'结果操作':{'operation':{'type':'limit','count':2}}}
    if fault=='no_plan':planned=None
    assert dependency_selection_complete(result.reference,1,planned)==(fault is None)
    if fault is not None:return
    await agent.sessions.put_dataset_reference(result.reference.to_dict(),recent_limit=20)
    response=AgentResponse(request_id=uuid4(),conversation_id=chat.conversation_id,status='COMPLETED',
        intent=PrimaryIntent.METRIC_QUERY,answer='selected',dataset_id=result.reference.dataset_id)
    constraints=await agent._compile_dependency_constraints(AtomicTask(task_id='task-3',
        question='查询上述经销商合作的医院名单',depends_on=['task-2']),chat,IDENTITY,
        {'task-2':response},{'task-2':chat.conversation_id},{'task-2':planned})
    assert constraints[0].source_column=='dealer.dealer_code'
    assert constraints[0].values==['SYNTH-B']


@pytest.mark.asyncio
async def test_v2_cached_business_response_replays_the_five_pre_output_stages(provider):
    from test_v2_context_v1_execution_bridge import handler,install_resolution,request,response,IDENTITY,DeploymentRedis
    calls=[]
    async def v1(chat,identity):calls.append(chat.message_id);return response(chat)
    bridge=handler(provider,DeploymentRedis(),v1)
    install_resolution(bridge,provider,completed='查询销售总额')
    chat=request(question='查询销售总额',message_id='replay-node-contract')
    await bridge.handle(chat,IDENTITY)
    events=[]
    with progress_scope(events.append):await bridge.handle(chat,IDENTITY)
    assert len(calls)==1
    assert [event['stage'] for event in events]==['INTENT_RECOGNITION','TASK_PLANNING','DATA_RETRIEVAL','RELIABILITY_CHECK','INSIGHT_ANALYSIS']
    assert '未执行新的ASL、SQL或数据库查询' in events[2]['message']


@pytest.mark.asyncio
async def test_bound_child_pronoun_does_not_become_a_new_literal_filter():
    from app.domain.models import DependencyConstraint,PlannerExtraction
    from app.services.intent_asl_contract import build_intent_asl_contract,validate_intent_asl_contract_completeness
    from app.services.orchestrator import _INTERNAL_ASSUMPTIONS
    from test_complex_execution_correctness import service,context,IDENTITY
    agent=service();chat,_=context()
    chat.question='根据上述任务的查询结果筛选出订单笔数最多的经销商，并列出该经销商在2025年上海合作的医院名单'
    chat._completed_question_execution=True
    chat.dependency_constraints=[DependencyConstraint(source_task_id='task-2',source_dataset_id='synthetic-rank',
        source_column='dealer.dealer_code',values=['SYNTH-B'],value_fingerprint=hashlib.sha256(b'["SYNTH-B"]').hexdigest())]
    chat._planner_extraction=PlannerExtraction(intent=PrimaryIntent.DETAIL_QUERY,structured={
        '意图':'明细查询','实体':['销售订单','医院','经销商'],'展示字段':[{'entity':'医院','field':'医院名称'}],
        '过滤条件':[{'entity':'医院','field':'省份名称','op':'=','value':['上海']}],
        '时间粒度':{'unit':'年','time_range':'2025年'},'是否查询数据库':'是'})
    original=agent.adapters.query.query;seen=[]
    async def check(request,*args,**kwargs):
        seen.append(request)
        assert not validate_intent_asl_contract_completeness(build_intent_asl_contract(request),request)
        assert all('该经销商' not in str(item) for item in request.filters)
        return await original(request,*args,**kwargs)
    agent.adapters.query.query=check
    token=_INTERNAL_ASSUMPTIONS.set(('DAG_BOUND_STRUCTURED_QUERY',))
    try:
        with progress_scope(lambda event:None):await agent._handle(chat,IDENTITY)
    finally:_INTERNAL_ASSUMPTIONS.reset(token)
    assert len(seen)==1
    assert seen[0].original_question==chat.question
    assert seen[0].dependency_constraints[0].values==['SYNTH-B']
