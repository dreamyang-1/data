"""Exercise the shared DAG path with full synthetic source artifacts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import httpx
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse, AtomicTask, PlannerExtraction, PrimaryIntent, TaskPlan
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner, materialize_computation_binding
from app.services.orchestrator import _INTERNAL_ASSUMPTIONS
from app.planning.compound_question import deduplicate_plan, execution_layers
from test_complex_execution_correctness import service, context, persist, IDENTITY


ORIGINAL = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'


def source_plan(question, *, descending=True, count=5, label='平均每单销售金额'):
    return QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY, tasks=(DecomposedTask(
        question=question, intent=PrimaryIntent.METRIC_QUERY, structured={
            '意图':'统计查询', '指标':[{'name':label}], '维度':['业务员'],
            '展示字段':[{'entity':'业务员','field':'姓名'}, {'entity':'销售公司','field':'所属公司'}],
            '时间粒度':{'time_range':'2025年'}, '过滤条件':[],
            '排序':[{'field':label,'order':'desc' if descending else 'asc'}], '限制':count,
            '是否查询数据库':'是', '计算绑定':{'operation':'divide',
                'numerator':{'column':'含税销售总额'}, 'denominator':{'column':'订单笔数'},
                'result_field':label,'format':'number'}}),))


@pytest.mark.asyncio
@pytest.mark.parametrize('question,descending,count,label', [
    (ORIGINAL,True,5,'平均每单销售金额'),
    ('列出2025年每单平均金额最低的前三位销售人员及所属公司',False,3,'平均每单销售金额'),
    ('统计2025年各业务员的每单均额，按金额从高到低取前4名并展示公司',True,4,'每单均额'),
])
async def test_original_and_similar_questions_execute_complete_three_task_dag(question,descending,count,label):
    agent=service(); chat,_=context();chat.question=question
    agent.task_planner=SimpleNamespace(deduplicate=deduplicate_plan,execution_layers=execution_layers)
    raw=source_plan(question,descending=descending,count=count,label=label)
    split=SingleQuestionPlanner._split_compound_metric_tasks(raw,[(label,('含税销售总额','订单笔数'))])
    tasks=[]
    for index,item in enumerate(split.tasks):
        dependencies=[f'task-{dep+1}' for dep in item.depends_on]
        tasks.append(AtomicTask(task_id=f'task-{index+1}',question=item.question,
            depends_on=dependencies, primary_intent=item.intent,
            extraction=materialize_computation_binding(item.structured,task_index=index,dependencies=dependencies)))
    async def query(child,identity):
        assert 'DAG_CALCULATION_INPUT' in _INTERNAL_ASSUMPTIONS.get()
        canonical=await agent._classify(child.question,identity,child.conversation_id,
            pre_resolved=True,planner_extraction=child._planner_extraction)
        assert canonical.dimensions==['业务员']
        assert canonical.fields==['姓名','所属公司']
        metric=child._planner_extraction.structured['指标'][0]['name']
        values=range(1,9) if metric=='含税销售总额' else range(8,0,-1)
        rows=[{'业务员（编码）':f'P{i}','姓名':'同名销售员','所属公司':f'公司{i%2}',
            metric:i*i*100 if metric=='含税销售总额' else i} for i in values]
        ref=await persist(agent,child,list(rows[0]),rows,conversation=child.conversation_id)
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,
            status='COMPLETED',intent=PrimaryIntent.METRIC_QUERY,answer='源统计已完成',dataset_id=ref.dataset_id)
    agent._handle=query
    result=await agent._handle_task_plan(chat,IDENTITY,TaskPlan(planner='STRUCTURED_MODEL',tasks=tasks))
    assert [task.status for task in result.task_results]==['COMPLETED']*3
    output=next(item.payload for item in result.evidence if item.kind=='ANALYSIS_RESULT' and 'input_binding' in item.payload)
    assert output['row_count']==count
    assert [row[label] for row in output['preview']]==sorted([i*100 for i in range(1,9)],reverse=descending)[:count]
    assert all(row['所属公司'] for row in output['preview'])
    assert tasks[-1].extraction['计算绑定']['numerator']['source_task_id']=='task-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('task_id',['task-2','task-3','task-4'])
async def test_two_scalar_calculation_is_independent_of_task_number(task_id):
    agent=service();chat,_=context()
    await persist(agent,chat,['金额','其他数值'],[{'金额':1000,'其他数值':9000}],conversation='amount')
    await persist(agent,chat,['订单数'],[{'订单数':4}],conversation='orders')
    task=AtomicTask(task_id=task_id,question='计算一个新的均额指标',depends_on=['a','b'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide','numerator':{'source_task_id':'a','column':'金额'},
        'denominator':{'source_task_id':'b','column':'订单数'},'result_field':'未预置的均额名称','format':'number'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'amount','b':'orders'})
    assert result.status=='COMPLETED'
    assert result.evidence[0].payload['preview'][0]['未预置的均额名称']==250


@pytest.mark.asyncio
async def test_one_row_total_with_own_dimension_code_broadcasts_as_scalar():
    """覆盖率类计算：1行分母只带自身维度身份列（省份编码），对多行分子是标量。"""
    agent=service();chat,_=context()
    rows=[{'经销商名称':f'经销商{i}','经销商（编码）':f'C{i}','有效订单去重医院数':i+1} for i in range(3)]
    await persist(agent,chat,list(rows[0]),rows,conversation='dealers',
        column_bindings={'经销商名称':'dealer.dealer_name','经销商（编码）':'dealer.dealer_code'})
    await persist(agent,chat,['省份名称','省份（编码）','区域全部医院总数'],
        [{'省份名称':'上海市','省份（编码）':'310000','区域全部医院总数':122}],conversation='province',
        column_bindings={'省份名称':'province.province_name','省份（编码）':'province.province_code'})
    task=AtomicTask(task_id='task-3',question='计算上海地区各经销商的区域医院覆盖率',depends_on=['a','b'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide',
        'numerator':{'source_task_id':'a','column':'有效订单去重医院数'},
        'denominator':{'source_task_id':'b','column':'区域全部医院总数'},
        'result_field':'区域医院覆盖率','format':'percent'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'dealers','b':'province'})
    assert result.status=='COMPLETED'
    preview=result.evidence[0].payload['preview']
    assert [row['区域医院覆盖率'] for row in preview]==['0.82%','1.64%','2.46%']
    assert [row['经销商（编码）'] for row in preview]==['C0','C1','C2']


@pytest.mark.asyncio
async def test_one_row_operand_sharing_identity_still_requires_alignment():
    """1行操作数与分子共享同一身份字段时不得当标量广播，保持按编号对齐合同。"""
    agent=service();chat,_=context()
    rows=[{'经销商（编码）':'C1','金额':100},{'经销商（编码）':'C2','金额':200}]
    await persist(agent,chat,list(rows[0]),rows,conversation='dealers',
        column_bindings={'经销商（编码）':'dealer.dealer_code'})
    await persist(agent,chat,['经销商（编码）','单数'],[{'经销商（编码）':'C1','单数':2}],conversation='orders',
        column_bindings={'经销商（编码）':'dealer.dealer_code'})
    task=AtomicTask(task_id='task-3',question='计算各经销商均额',depends_on=['a','b'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide',
        'numerator':{'source_task_id':'a','column':'金额'},
        'denominator':{'source_task_id':'b','column':'单数'},
        'result_field':'均额','format':'number'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'dealers','b':'orders'})
    assert result.status=='FAILED'


@pytest.mark.asyncio
async def test_growth_pipeline_executes_in_order_without_asking_model_to_calculate():
    agent=service();chat,_=context()
    await persist(agent,chat,['上期金额','本期金额'],[{'上期金额':100,'本期金额':120}],conversation='source')
    task=AtomicTask(task_id='task-3',question='计算增长率',depends_on=['q'],extraction={'是否查询数据库':'否',
        '结果操作':{'source_task_id':'q','operation':{'type':'pipeline','operations':[
            {'type':'derive','left_field':'本期金额','right_field':'上期金额','operator':'subtract','result_field':'增量'},
            {'type':'derive','left_field':'增量','right_field':'上期金额','operator':'divide','result_field':'增长率'}]}}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'q':'source'})
    assert result.status=='COMPLETED'
    assert result.evidence[0].payload['preview'][0]['增长率']==0.2


@pytest.mark.asyncio
async def test_structured_ranking_is_enforced_when_calculation_text_omits_it():
    agent=service();chat,_=context()
    rows=[{'编码':str(i),'金额':i*100,'单数':1} for i in [1,3,2]]
    await persist(agent,chat,list(rows[0]),rows,conversation='source')
    task=AtomicTask(task_id='task-3',question='计算均额',depends_on=['q'],extraction={
        '是否查询数据库':'否','计算绑定':{'operation':'divide','numerator':{'source_task_id':'q','column':'金额'},
        'denominator':{'source_task_id':'q','column':'单数'},'result_field':'均额','format':'number'},
        '排序':[{'field':'均额','order':'desc'}],'限制':2})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'q':'source'})
    assert [row['均额'] for row in result.evidence[0].payload['preview']]==[300,200]


@pytest.mark.asyncio
async def test_wrong_source_reference_cannot_select_another_task_dataset():
    agent=service();chat,_=context()
    await persist(agent,chat,['金额','单数'],[{'金额':100,'单数':2}],conversation='source')
    task=AtomicTask(task_id='task-3',question='计算均额',depends_on=['q'],extraction={
        '计算绑定':{'operation':'divide','numerator':{'source_task_id':'unrelated','column':'金额'},
        'denominator':{'source_task_id':'q','column':'单数'},'result_field':'均额'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'q':'source'})
    assert result.status=='FAILED' and not result.dataset_id


@pytest.mark.asyncio
async def test_formula_roles_follow_registered_expression_instead_of_component_order():
    from app.planning.calculation_bindings import resolve_compound_formulas
    agent=service()
    spec='| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n| 新口径 | 无 | 登记金额 ÷ 登记数量 | 元 | 登记数量, 登记金额 |'
    result=await resolve_compound_formulas(agent.settings,None,spec,{'新口径':('登记数量','登记金额')},['新口径'])
    assert result['新口径']['numerator']['column']=='登记金额'
    assert result['新口径']['denominator']['column']=='登记数量'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_operand',[False,True])
async def test_formula_binding_is_grounded_in_the_current_description(bad_operand):
    from app.planning.calculation_bindings import resolve_compound_formulas
    agent=service()
    settings=agent.settings.model_copy(update={'intent_model_api_key':'fixture'})
    from pydantic import SecretStr
    settings=settings.model_copy(update={'intent_model_api_key':SecretStr('fixture')})
    calls=[]
    async def respond(request):
        calls.append(json.loads(request.content))
        content={'bindings':[{'metric':'新均额','operation':'divide','numerator':'外部指标' if bad_operand else '登记金额',
            'denominator':'登记笔数','format':'number'}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content,ensure_ascii=False)}}]})
    spec='| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n| 新均额 | 无 | 收入合计÷交易笔数 | 元 | 登记金额, 登记笔数 |'
    if bad_operand:
        with pytest.raises(ValueError,match='登记成分'):
            await resolve_compound_formulas(settings,httpx.MockTransport(respond),spec,{'新均额':('登记金额','登记笔数')},['新均额'])
    else:
        result=await resolve_compound_formulas(settings,httpx.MockTransport(respond),spec,{'新均额':('登记金额','登记笔数')},['新均额'])
        assert result['新均额']['numerator']['column']=='登记金额'
    assert len(calls)==1
    assert '收入合计÷交易笔数' in calls[0]['messages'][1]['content']


@pytest.mark.asyncio
async def test_model_plan_without_a_formula_gets_a_bound_three_task_plan(monkeypatch):
    from pydantic import SecretStr
    from app.domain import semantic_description
    from app.planning.compound_question import CompoundQuestionRouter, CompoundJudgment
    spec=('| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |\n| --- | --- | --- | --- | --- | --- |\n'
        '| 平均每单销售金额 | 每单平均金额 | 销售总金额 ÷ 订单总数 | 元 | 复合口径指标 | 含税销售总额, 订单笔数 |\n'
        '| 含税销售总额 | 销售总金额 | 金额合计 | 元 | 原子指标 | 无 |\n'
        '| 订单笔数 | 订单总数 | 去重订单数 | 笔 | 原子指标 | 无 |')
    monkeypatch.setattr(semantic_description,'load_semantic_description',AsyncMock(return_value={'available':True,'content':spec,'source':'semantic_model_81.md'}))
    raw=source_plan(ORIGINAL).tasks[0].structured.copy();raw.pop('计算绑定')
    calls=[]
    async def respond(request):
        body=json.loads(request.content);calls.append(body)
        content=({'bindings':[{'metric':'平均每单销售金额','operation':'divide','numerator':'含税销售总额',
            'denominator':'订单笔数','format':'number'}]} if '只绑定给定规范' in body['messages'][0]['content']
            else {'intent':'METRIC_QUERY','tasks':[{'question':ORIGINAL,'extraction':raw}]})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content,ensure_ascii=False)}}]})
    agent=service();settings=agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':SecretStr('fixture')})
    planner=SingleQuestionPlanner(settings,transport=httpx.MockTransport(respond))
    router=CompoundQuestionRouter(settings);router._single=planner
    outcome=await router.plan_with_judgment(CompoundJudgment(is_compound=False),ORIGINAL,semantic_model_id=81)
    assert outcome.plan is not None, repr(outcome)
    assert len(outcome.plan.tasks)==3
    binding=outcome.plan.tasks[-1].extraction['计算绑定']
    assert binding['numerator']['source_task_id']=='task-1'
    assert binding['denominator']['source_task_id']=='task-2'
    assert len(calls)==2


@pytest.mark.asyncio
async def test_real_query_string_metric_metadata_and_proven_code_alias_are_accepted():
    agent=service();chat,_=context()
    provenance={'time_range':{'start':'2025-01-01','end_exclusive':'2026-01-01'},'metrics':['金额']}
    source=await persist(agent,chat,['业务员','金额'],[{'业务员':'P1','金额':1000},{'业务员':'P2','金额':300}],
        conversation='a',fingerprint_components=provenance,column_bindings={'业务员':'salesperson.salesperson_code'})
    await persist(agent,chat,['业务员（编码）','单数'],[{'业务员（编码）':'P2','单数':2},{'业务员（编码）':'P1','单数':4}],
        conversation='b',fingerprint_components={**provenance,'metrics':['单数']},column_bindings={'业务员（编码）':'salesperson.salesperson_code'})
    task=AtomicTask(task_id='task-3',question='计算均额',depends_on=['a','b'],extraction={'计算绑定':{
        'operation':'divide','numerator':{'source_task_id':'a','column':'金额'},
        'denominator':{'source_task_id':'b','column':'单数'},'result_field':'均额','format':'number'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'a','b':'b'})
    assert result.status=='COMPLETED'
    assert {row['业务员']:row['均额'] for row in result.evidence[0].payload['preview']}=={'P1':250,'P2':150}


def test_calculation_sources_follow_query_deduplication():
    binding={'operation':'divide','numerator':{'source_task_id':'task-2','column':'金额'},
        'denominator':{'source_task_id':'task-1','column':'单数'},'result_field':'均额'}
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='task-1',question='查询金额和单数'),
        AtomicTask(task_id='task-2',question='查询金额和单数'),
        AtomicTask(task_id='task-3',question='计算均额，不再查询数据库',depends_on=['task-1','task-2'],extraction={'计算绑定':binding})])
    reduced,aliases=deduplicate_plan(plan)
    assert aliases=={'task-2':'task-1'}
    assert reduced.tasks[-1].depends_on==['task-1']
    assert reduced.tasks[-1].extraction['计算绑定']['numerator']['source_task_id']=='task-1'
    assert binding['numerator']['source_task_id']=='task-2'


@pytest.mark.asyncio
async def test_subtraction_and_model_order_limit_spellings_execute_the_same_bounded_contract():
    agent=service();chat,_=context()
    rows=[{'金额':100,'成本':40},{'金额':80,'成本':10},{'金额':90,'成本':80}]
    await persist(agent,chat,['金额','成本'],rows,conversation='source')
    operation={'type':'pipeline','operations':[
        {'type':'derive','left_field':'金额','right_field':'成本','operator':'subtract','result_field':'差额'},
        {'type':'sort_limit','field':'差额','order':'desc','limit':2}]}
    task=AtomicTask(task_id='task-3',question='计算差额',depends_on=['q'],extraction={
        '结果操作':{'source_task_id':'q','operation':operation}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'q':'source'})
    assert result.status=='COMPLETED'
    assert [row['差额'] for row in result.evidence[0].payload['preview']]==[70,60]
    assert operation['operations'][-1]['order']=='desc'
