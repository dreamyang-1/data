"""Real scheduler and persisted synthetic datasets across question boundaries."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse, PrimaryIntent
from app.planning.compound_question import (
    CompoundJudgment, CompoundJudgmentEntry, CompoundTaskPlanner, CompoundQuestionRouter,
)
from app.planning.task_dag import DecomposedTask, QuestionDecomposition
from test_complex_execution_correctness import service, context, persist, IDENTITY


@pytest.mark.asyncio
@pytest.mark.parametrize('source_state', ['complete', 'empty', 'failed'])
async def test_ranked_question_feeds_two_queries_then_key_aligned_calculation(source_state):
    agent = service()
    agent.task_planner = CompoundQuestionRouter(agent.settings)
    chat, _ = context()
    first = '统计2025年各经销商订单笔数并取前2名'
    second = '查询上述经销商的含税销售总额和订单笔数，计算平均每单销售金额'
    independent = '单独统计2025年医院数量'
    chat.question = first + '；' + second + '；' + independent
    common = {'意图':'统计查询', '是否查询数据库':'是', '维度':['经销商编码'],
              '时间粒度':{'unit':'年','time_range':'2025年'}}
    selection = {'source_task_index':0,'operation':{'type':'pipeline','operations':[
        {'type':'sort','field':'订单笔数','descending':True},{'type':'limit','count':2}]}}
    decompositions = {
        first: QuestionDecomposition(tasks=(
            DecomposedTask(question='统计2025年各经销商订单笔数', structured={**common,'指标':[{'name':'订单笔数'}]}),
            DecomposedTask(question='根据上述结果取订单笔数前2名，不再查询数据库',depends_on=(0,),
                structured={**common,'是否查询数据库':'否','结果操作':selection}),
        )),
        second: QuestionDecomposition(tasks=(
            DecomposedTask(question='查询上述经销商2025年含税销售总额', structured={**common,'指标':[{'name':'含税销售总额'}],
                '依赖绑定':[{'source_column':'经销商编码'}]}),
            DecomposedTask(question='查询上述经销商2025年订单笔数', structured={**common,'指标':[{'name':'订单笔数'}],
                '依赖绑定':[{'source_column':'经销商编码'}]}),
            DecomposedTask(question='根据上述任务结果计算平均每单销售金额，不再查询数据库',depends_on=(0,1),
                structured={'意图':'指标计算','是否查询数据库':'否','指标':[{'name':'平均每单销售金额'}],
                    '维度':['经销商编码'],'计算绑定':{'operation':'divide',
                    'numerator':{'source_task_index':0,'column':'含税销售总额'},
                    'denominator':{'source_task_index':1,'column':'订单笔数'},'result_field':'平均每单销售金额'}}),
        )),
        independent: QuestionDecomposition(tasks=(DecomposedTask(question=independent,
            structured={'意图':'统计查询','是否查询数据库':'是','指标':[{'name':'医院数量'}]}),)),
    }
    async def plan(question, **kwargs):
        return decompositions[question]
    judgment = CompoundJudgment(is_compound=True, entries=(
        CompoundJudgmentEntry(question=first),
        CompoundJudgmentEntry(question=second,depends_on=(0,)),
        CompoundJudgmentEntry(question=independent),
    ))
    tasks = await CompoundTaskPlanner(agent.settings,SimpleNamespace(plan_question=plan)).plan(
        judgment,source_question=chat.question,semantic_model_id=81)
    assert [task.depends_on for task in tasks.tasks] == [[],['task-1'],['task-2'],['task-2'],['task-3','task-4'],[]]
    binding = tasks.tasks[4].extraction['计算绑定']
    assert binding['numerator']['source_task_id']=='task-3'
    assert binding['denominator']['source_task_id']=='task-4'
    calls=[]
    async def query(child, identity):
        structured=child._planner_extraction.structured
        metric=structured['指标'][0]['name']
        calls.append((metric,bool(child.dependency_constraints)))
        if metric=='医院数量':
            rows=[{'医院数量':7}];columns=['医院数量']
        elif not child.dependency_constraints:
            if source_state=='failed':
                raise RuntimeError('synthetic source unavailable')
            rows=[] if source_state=='empty' else [
                {'经销商编码':f'D{i:02}','订单笔数':i} for i in range(1,14)]
            columns=['经销商编码','订单笔数']
        else:
            assert [(x.source_task_id,x.values) for x in child.dependency_constraints]==[
                ('task-2',['D13','D12'])]
            assert child.dependency_constraints[0].source_column in {'dealer.dealer_code','经销商编码'}
            assert child.dataset_id is None
            # Opposite order deliberately proves alignment by identity.
            rows=([{'经销商编码':'D13','含税销售总额':1300}, {'经销商编码':'D12','含税销售总额':2400}]
                if metric=='含税销售总额' else
                [{'经销商编码':'D12','订单笔数':12}, {'经销商编码':'D13','订单笔数':13}])
            columns=['经销商编码',metric]
        ref=await persist(agent,child,columns,rows,conversation=child.conversation_id,
            column_bindings={'经销商编码':'dealer.dealer_code'})
        from app.domain.models import EvidenceItem
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,
            status='COMPLETED',intent=PrimaryIntent.METRIC_QUERY,answer='synthetic result',dataset_id=ref.dataset_id,
            evidence=[EvidenceItem(evidence_id='query',kind='QUERY_RESULT',source_ref=ref.dataset_id,
                payload={'row_count':len(rows),'returned_row_count':len(rows),'total_row_count':len(rows),
                         'total_row_count_confirmed':True,'truncated':False,'columns':columns})])
    agent._handle=query
    response=await agent._handle_task_plan(chat,IDENTITY,tasks)
    statuses=[task.status for task in response.task_results]
    if source_state=='complete':
        assert statuses==['COMPLETED']*6
        assert response.status=='COMPLETED'
        calculation=next(x.payload for x in response.evidence if x.kind=='ANALYSIS_RESULT'
                         and any('平均每单销售金额' in row for row in x.payload.get('preview',[])))
        assert {x['经销商编码']:x['平均每单销售金额'] for x in calculation['preview']}=={'D13':100,'D12':200}
        assert len(calls)==4
    else:
        assert statuses==['COMPLETED' if source_state=='empty' else 'FAILED','SKIPPED','SKIPPED','SKIPPED','SKIPPED','COMPLETED']
        assert response.status=='PARTIAL_SUCCESS'
        assert len(calls)==2
    assert len(response.task_plan.question_groups)==3


@pytest.mark.asyncio
async def test_entity_grain_does_not_invent_extra_detail_columns(monkeypatch):
    from test_task_planning_stages import setup_planner
    question='查询订单笔数最多的经销商及其合作医院'
    boundaries={'tasks':[{'question':'查询订单笔数最多的经销商','metric_targets':['订单笔数'],'grain':['经销商']},
        {'question':'查询上述经销商合作的医院名单','depends_on':[0],'grain':['经销商','医院']}]}
    def extraction(fixed):
        return {'tasks':[{'task_index':0,'extraction':{'意图':'统计查询','指标':[{'name':'订单笔数'}],'维度':['经销商']}},
            {'task_index':1,'extraction':{'意图':'明细查询','指标':[],
                '展示字段':[{'entity':'经销商','field':'经销商编码'},{'entity':'医院','field':'医院名称'}]}}]}
    planner,_=setup_planner(monkeypatch,boundaries,extract=extraction)
    result=await planner.plan_question(question,semantic_model_id=130)
    child=result.tasks[1]
    assert child.structured['展示字段']==[{'entity':'经销商','field':'经销商编码'},{'entity':'医院','field':'医院名称'}]
    from app.planning.dependency_contract import missing_display_fields
    assert not missing_display_fields(child.structured,['经销商编码','医院名称'])
    assert missing_display_fields(child.structured,['经销商编码'])==['医院名称']


@pytest.mark.parametrize('key',['计算绑定','结果操作','计算待确认'])
def test_empty_optional_computation_object_is_absent(key):
    from app.planning.task_dag import SingleQuestionPlanner
    result=SingleQuestionPlanner._sanitize_extraction({'意图':'明细查询',key:{}},intent=PrimaryIntent.DETAIL_QUERY)
    assert key not in result


def test_new_related_fields_override_an_invented_local_metric_projection():
    from app.planning.task_dag import _repair_unavailable_local_projection
    original=QuestionDecomposition(tasks=(
        DecomposedTask(question='查询销售额前3名经销商',structured={
            '指标':[{'name':'含税销售总额'}],'展示字段':[{'entity':'经销商','field':'经销商编码'}]}),
        DecomposedTask(question='查询上述经销商的合作医院名单，不再查询数据库',depends_on=(0,),structured={
            '意图':'指标计算','指标':[{'name':'有效订单去重医院数'}],'是否查询数据库':'否',
            '展示字段':[{'entity':'医院','field':'医院名称'}],
            '结果操作':{'source_task_index':0,'operation':{'pipeline':[{'select':['医院']}]}}}),
    ))
    fixed=_repair_unavailable_local_projection(original,'查询销售额前3名经销商及其合作医院')
    child=fixed.tasks[1]
    assert child.structured['是否查询数据库']=='是' and child.structured['指标']==[]
    assert '结果操作' not in child.structured and '不再查询数据库' not in child.question
    assert child.depends_on==(0,) and '医院名称' in child.question
    assert original.tasks[1].structured.get('结果操作')


@pytest.mark.asyncio
async def test_invalid_local_operation_is_replanned_once_before_execution(monkeypatch):
    from app.planning.task_dag import SingleQuestionPlanner, TaskInputBindingError
    from unittest.mock import AsyncMock
    from test_task_planning_stages import setup_planner
    planner,_=setup_planner(monkeypatch,{'tasks':[{'question':'查询经销商订单笔数'}]})
    invalid=QuestionDecomposition(boundary_plan_complete=True,tasks=(
        DecomposedTask(question='查询经销商订单笔数',structured={'意图':'统计查询','指标':[{'name':'订单笔数'}]}),
        DecomposedTask(question='根据结果取前2名，不再查询数据库',depends_on=(0,),structured={
            '是否查询数据库':'否','结果操作':{'source_task_index':0,'operation':{'type':'lookup'}}}),
    ))
    from dataclasses import replace
    valid=replace(invalid,tasks=(invalid.tasks[0],replace(invalid.tasks[1],structured={
        '是否查询数据库':'否','结果操作':{'source_task_index':0,'operation':{'type':'sort_limit','field':'订单笔数','count':2}}})))
    mocked=AsyncMock(side_effect=[invalid,valid])
    monkeypatch.setattr(planner,'_model_decompose',mocked)
    result=await planner.plan_question('查询经销商订单笔数并取前2名')
    assert result==valid and mocked.await_count==2
    assert '未知或嵌套结果操作' in mocked.await_args_list[1].kwargs['retry_feedback']
    mocked.side_effect=[invalid,invalid]
    # 2026-10-21 用户裁决：重试耗尽的无效结果操作不再拦停整个计划，
    # 降级为丢弃结果操作后按原样继续执行（拆错了就拆错了）。
    degraded=await planner.plan_question('查询经销商订单笔数并取前2名')
    assert mocked.await_count==4
    assert degraded.tasks[1].structured.get('结果操作') is None
    assert degraded.tasks[0].structured.get('指标')==[{'name':'订单笔数'}]


@pytest.mark.asyncio
async def test_rejected_multi_task_plan_never_falls_through_to_one_query():
    from app.config import Settings
    from app.planning.task_dag import TaskPlanningError
    class BrokenPlanner:
        async def plan(self,question,**kwargs):
            raise TaskPlanningError('synthetic invalid operation')
    agent=service();agent.task_planner=BrokenPlanner()
    chat,_=context();chat.question='统计订单笔数，再查询这些经销商的医院名单'
    calls=[]
    async def unexpected(*args,**kwargs):
        calls.append(True)
        raise AssertionError('query must not execute after rejected planning')
    agent._handle=unexpected
    response=await agent.handle(chat,IDENTITY)
    assert response.status=='SAFE_FALLBACK'
    assert any(x.kind=='TASK_PLANNING_REJECTED' and x.payload['sql_executed'] is False for x in response.evidence)
    assert not calls


def test_planning_rejection_stream_keeps_seven_stages():
    import json,re
    from app.planning.task_dag import TaskPlanningError
    from test_api import TestClient,build_test_app
    agent=service()
    class BrokenPlanner:
        async def plan(self,question,**kwargs):
            raise TaskPlanningError('synthetic invalid operation')
    agent.task_planner=BrokenPlanner()
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):
            return {'response':await agent.handle(state['chat'],state['identity'])}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'rejected-planning-stages','message_id':'m1',
            'question':'统计订单笔数，再查询这些经销商的医院名单'})
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    assert re.findall(r'#### ◉ ([^\r\n]+)',text)==[
        '意图识别','任务拆分与规划','解析校验','调度执行','结果校验','数据洞察分析','最终输出']
    assert next(e for e in events if e.get('type')=='complete')['status']=='SAFE_FALLBACK'
