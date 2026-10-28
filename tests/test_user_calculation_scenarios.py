"""User phrasing through bounded numeric planning and dependent result transforms."""
import copy
import json
import re

import pytest

from app.domain.models import AtomicTask, PrimaryIntent
from app.numeric_operations import aggregate_numeric, NumericOperationError
from app.planning.literal_calculation import parse_literal_computation, constant_pipeline
from app.planning.calculation_bindings import normalize_result_operation, validate_result_operation
from app.services.calculation_execution import calculate, literal_computation
from minio_followup_store import apply_followup_operation
from test_complex_execution_correctness import service, context, persist, IDENTITY


@pytest.mark.parametrize('question,expected',[
    ('帮我算一下，125.5加上74.5，再除以4，结果保留两位小数。',[50]),
    ('计算（12+8）×3÷4，按先括号后乘除计算。',[15]),
    ('分别计算-8的立方根、2的10次方和2.675四舍五入保留两位小数。',[-2,1024,2.68]),
    ('2+3*4',[14]),('计算-2**2',[-4]),('计算2**3**2',[512]),('计算sqrt(16)+3',[7]),
])
def test_numeric_user_phrasing_keeps_order_and_all_requested_results(question,expected):
    structured=parse_literal_computation(question)
    assert literal_computation(structured)
    task=AtomicTask(task_id='math',question=question,extraction=structured)
    columns,rows,operation,_,_=calculate(task,{},lambda *args,**kwargs:None)
    assert [rows[0][column] for column in columns]==pytest.approx(expected)
    if '结果操作' in structured:assert operation['type']=='pipeline'


@pytest.mark.parametrize('question',[
    "__import__('os').system('bad')",'open(1)','sqrt.__call__(9)','计算上海销售额+3',
    '查询2025年订单笔数再除以4','分别查询订单和销售额','25','讲一个2+3的故事',
    '[x for x in range(3)]','2 if 1 else 3','sqrt(x)','(lambda: 1)()',
    '1+2+3+4+5+6+7+8+9','round(2.5, digits=2)',
])
def test_numeric_compiler_cannot_consume_business_queries_or_executable_syntax(question):
    assert parse_literal_computation(question) is None


def test_constant_pipeline_proves_field_lineage_and_cannot_read_business_data():
    value={'operation':{'type':'pipeline','operations':[
        {'type':'derive','operator':'square','field':'销售金额','result_field':'结果'}]}}
    assert constant_pipeline(value) is None
    assert not literal_computation({'结果操作':value})
    value=parse_literal_computation('2+3*4')
    assert not literal_computation({**value,'实体':['销售订单']})
    assert not literal_computation({**value,'指标':[{'name':'销售额'}]})


@pytest.mark.parametrize('question,expected,status',[
    ('帮我算一下，125.5加上74.5，再除以4，结果保留两位小数。','50.00','COMPLETED'),
    ('计算（12+8）×3÷4，按先括号后乘除计算。','15','COMPLETED'),
    ('分别计算-8的立方根、2的10次方和2.675四舍五入保留两位小数。','1024','COMPLETED'),
    ('计算10除以0，并说明为什么。','除数不能为零','FAILED'),
])
def test_real_entry_keeps_seven_nodes_without_query_or_model_planning(question,expected,status):
    from test_api import TestClient,build_test_app
    agent=service()
    async def forbidden(*args,**kwargs):raise AssertionError('numeric request must not query data')
    agent._handle=forbidden
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):return {'response':await agent.handle(state['chat'],state['identity'])}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'math-stage','message_id':'m1','question':question})
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(e.get('content','') for e in events if e.get('type')=='message_chunk')
    assert re.findall(r'#### ◉ ([^\r\n]+)',text)==['意图识别','任务拆分与规划','解析校验','调度执行','结果校验','数据洞察分析','最终输出']
    final=next(e for e in events if e.get('type')=='complete')
    assert final['status']==status and expected in final['answer'].replace(',','')
    if status=='FAILED':assert not final.get('dataset_id')


@pytest.mark.parametrize('spec',[
    {'field':'x','function':'percentile','params':{'p':.9}},
    {'field':'x','function':'percentile','percentile':90},
    {'field':'x','function':'quantile','params':{'q':.9}},
])
def test_explicit_ninetieth_percentile_is_never_replaced_by_default_median(spec):
    assert aggregate_numeric([{'x':i} for i in range(1,11)],spec)==pytest.approx(9.1)


@pytest.mark.parametrize('spec',[
    {'field':'x','function':'percentile','percentile':90,'params':{'p':.5}},
    {'field':'x','function':'percentile','params':{'percent':90}},
    {'field':'x','function':'percentile','params':{'p':90}},
    {'field':'x','function':'variance','params':{'unknown':1}},
])
def test_unknown_and_conflicting_statistic_parameters_do_not_get_silently_ignored(spec):
    with pytest.raises(NumericOperationError):aggregate_numeric([{'x':1},{'x':2}],spec)


def test_paired_field_spelling_is_normalized_before_boundary_overwrites_extraction():
    operation=normalize_result_operation({'type':'aggregate','aggregations':[
        {'field':'x','right_field':'y','function':'correlation','alias':'r'}]})
    validate_result_operation(operation)
    _,rows=apply_followup_operation(['x','y'],[{'x':1,'y':2},{'x':2,'y':4}],operation)
    assert rows==[{'r':1}]
    with pytest.raises(ValueError):normalize_result_operation({'type':'aggregate','aggregations':[
        {'field':'x','right_field':'y','other_field':'z','function':'correlation'}]})


def threshold_operation():
    return {'type':'pipeline','operations':[
        {'type':'aggregate','group_by':[],'aggregations':[{'field':'订单笔数','function':'avg','alias':'平均订单笔数'}]},
        {'type':'filter','condition':{'left_field':'订单笔数','operator':'>','right_field':'平均订单笔数'}},
        {'type':'select','fields':['业务员']}]}


def test_above_mean_selection_retains_original_identities_and_does_not_mutate_plan():
    original=threshold_operation();before=copy.deepcopy(original)
    normalized=normalize_result_operation(original);validate_result_operation(normalized)
    columns,rows=apply_followup_operation(['业务员','订单笔数'],[
        {'业务员':'A','订单笔数':1},{'业务员':'B','订单笔数':2},{'业务员':'C','订单笔数':9}],normalized)
    assert columns==('业务员',) and rows==[{'业务员':'C'}]
    assert original==before
    original['operations'][0]['retain_rows']=False
    with pytest.raises(ValueError):normalize_result_operation(original)


@pytest.mark.asyncio
async def test_filtered_above_mean_dataset_is_complete_and_reusable_as_query_input():
    agent=service();chat,_=context()
    await persist(agent,chat,['业务员','订单笔数'],[{'业务员':f'S{i}','订单笔数':i} for i in range(1,30)],
        conversation='query',column_bindings={'业务员':'salesperson.salesperson_code'})
    task=AtomicTask(task_id='selection',question='筛选高于均值的业务员，不再查询数据库',depends_on=['query'],
        extraction={'是否查询数据库':'否','结果操作':{'source_task_id':'query','operation':threshold_operation()}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'query':'query'})
    assert result.status=='COMPLETED' and result.evidence[0].payload['row_count']==14
    # The full result, rather than the answer text, carries codes to later queries.
    reference=await agent._latest_task_dataset_reference(IDENTITY,chat,result.conversation_id,dataset_id=result.dataset_id)
    assert reference is not None


def test_metric_display_field_is_retained_in_the_outbound_contract_without_weakening_guard():
    from app.intent.classifier import RuleBasedIntentClassifier
    from app.services.turn_admission import TurnAdmissionGate
    from app.services.intent_asl_contract import build_intent_asl_contract,validate_intent_asl_contract_completeness
    request=RuleBasedIntentClassifier().classify('查询2025年业务员含税销售总额，展示销售员姓名',IDENTITY,'display')
    request.primary_intent=PrimaryIntent.METRIC_QUERY;request.fields=['销售员姓名'];request.dimensions=['业务员']
    request.turn_admission=TurnAdmissionGate().evaluate(question=request.original_question,current=request,previous=None,message_id='m')
    contract=build_intent_asl_contract(request)
    assert '销售员姓名' in contract['required_projections']
    broken=copy.deepcopy(contract);broken['required_projections']=[]
    assert any(e['code']=='EXPLICIT_PROJECTION_MISSING' for e in validate_intent_asl_contract_completeness(broken,request))


def test_dependency_uses_verified_physical_code_even_when_display_alias_contains_spaces():
    from app.planning.query_dependency_bindings import select_dependency_column
    selected=select_dependency_column('salesperson.salesperson_code',
        ('业务员 ID','销售员姓名','订单笔数'),
        {'业务员 ID':'salesperson.salesperson_code','销售员姓名':'salesperson.salesperson_name'})
    assert selected=='业务员 ID'



def test_unproduced_average_is_rejected_during_planning_instead_of_after_sql():
    from app.planning.calculation_bindings import validate_computation_column_lineage
    from app.planning.task_dag import DecomposedTask,QuestionDecomposition,TaskInputBindingError
    query=DecomposedTask(question='查询每个业务员订单笔数，并计算均值',structured={
        '指标':[{'name':'订单笔数'}],'维度':['业务员'],'输出要求':'订单笔数和平均订单笔数'})
    compute=DecomposedTask(question='筛选高于平均值，不再查询数据库',depends_on=(0,),structured={
        '结果操作':{'source_task_index':0,'operation':{'type':'filter','field':'订单笔数','operator':'gt','value_field':'平均订单笔数'}}})
    with pytest.raises(TaskInputBindingError,match='未由前序查询或计算产生'):
        validate_computation_column_lineage(QuestionDecomposition(tasks=(query,compute)))
    compute.structured['结果操作']['operation']=threshold_operation()
    validate_computation_column_lineage(QuestionDecomposition(tasks=(query,compute)))


def test_pipeline_container_and_single_condition_spellings_keep_complete_execution():
    original=threshold_operation();steps=original['operations']
    steps[1]={'type':'filter','conditions':[{'field':'订单笔数','operator':'gt','value_field':'平均订单笔数'}]}
    operation=normalize_result_operation({'pipeline':steps})
    validate_result_operation(operation)
    _,rows=apply_followup_operation(['业务员','订单笔数'],[{'业务员':'A','订单笔数':1},{'业务员':'B','订单笔数':9}],operation)
    assert rows==[{'业务员':'B'}]
    with pytest.raises(ValueError):normalize_result_operation({'pipeline':steps,'operations':[]})
    with pytest.raises(ValueError):normalize_result_operation({'type':'filter','conditions':[]})


@pytest.mark.parametrize('question',[
    '仅对问题一选出的业务员，查询金额','查询问题1中的业务员金额','根据第一个问题的结果查询医院',
    '使用第2个任务返回的经销商名单','对第3问的客户查询订单','查询Q1选出的医院',
    '对前一个问题里的业务员查询金额','查询上一问的结果中的对象',
    '仅对任务二筛选出的业务员查总额','根据问题一得到的结果查询商品',
])
def test_numbered_reference_is_shared_by_join_key_planning_and_parameter_execution(question):
    from app.planning.dependency_contract import numbered_dependency_anchor
    from app.services.orchestrator import DataAnalysisOrchestrator
    assert numbered_dependency_anchor(question)


@pytest.mark.parametrize('question',[
    '问题一：统计所有业务员订单数','第2问：查询全体医院','等任务一完成后统计全年销售额',
    '业务员编号为1的订单数','这个任务有3个问题','任务二发生错误',
])
def test_numbered_headings_and_control_order_do_not_create_result_filters(question):
    from app.planning.dependency_contract import numbered_dependency_anchor
    assert numbered_dependency_anchor(question) is None


@pytest.mark.asyncio
async def test_numbered_three_question_chain_passes_the_actual_cohort_before_final_sum():
    from uuid import uuid4
    from app.domain.models import AgentResponse,EvidenceItem,TaskPlan
    from app.planning.compound_question import CompoundQuestionRouter
    agent=service();agent.task_planner=CompoundQuestionRouter(agent.settings);chat,_=context()
    common={'是否查询数据库':'是','维度':['业务员'],'时间粒度':{'time_range':'2025年'}}
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id='task-1',question='问题一：查询前2位业务员',extraction={**common,'指标':[{'name':'订单笔数'}]}),
        AtomicTask(task_id='task-2',question='仅对问题一选出的业务员，查询2025年含税销售总额',depends_on=['task-1'],
            dependency_bindings=[{'source_task_id':'task-1','source_column':'业务员'}], extraction={**common, '指标':[{'name':'含税销售总额'}]}),
        AtomicTask(task_id='task-3',question='对问题二的销售总额求合计，不再查询数据库',depends_on=['task-2'],
            extraction={'是否查询数据库':'否','结果操作':{'source_task_id':'task-2','operation':{
                'type':'aggregate','group_by':[],'aggregations':[{'field':'含税销售总额','function':'sum','alias':'合计'}]}}})])
    calls=[]
    async def query(child,identity):
        first=child._planner_extraction.structured['指标'][0]['name']=='订单笔数'
        constraints=child.dependency_constraints;calls.append(bool(constraints))
        if first:rows=[{'业务员':'S1','订单笔数':8},{'业务员':'S2','订单笔数':7}]
        else:
            rows=[{'业务员':'S1','含税销售总额':100},{'业务员':'S2','含税销售总额':200}]
            if not constraints:rows.append({'业务员':'S3','含税销售总额':999})
            else:assert constraints[0].values==['S1','S2']
        ref=await persist(agent,child,list(rows[0]),rows,conversation=child.conversation_id,
            column_bindings={'业务员':'salesperson.salesperson_code'})
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,status='COMPLETED',
            intent=PrimaryIntent.METRIC_QUERY,answer='synthetic result',dataset_id=ref.dataset_id,
            evidence=[EvidenceItem(evidence_id='query',kind='QUERY_RESULT',source_ref=ref.dataset_id,payload={
                'row_count':len(rows),'returned_row_count':len(rows),'total_row_count_confirmed':True,'truncated':False})])
    agent._handle=query
    result=await agent._handle_task_plan(chat,IDENTITY,plan)
    assert result.status=='COMPLETED' and calls==[False,True]
    output=next(e.payload['preview'] for e in result.evidence if e.kind=='ANALYSIS_RESULT' and e.payload.get('operation'))
    assert output==[{'合计':300}]
