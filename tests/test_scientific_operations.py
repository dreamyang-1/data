"""Calculator and statistical operations through contracts and persisted tasks."""
import math
import statistics
import subprocess
import sys
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AtomicTask, TaskPlan
from app.planning.calculation_bindings import materialize_computation_binding, registered_arithmetic_binding, validate_result_operation
from app.planning.compound_question import deduplicate_plan
from app.planning.task_dag import TaskInputBindingError
from app.numeric_operations import evaluate, aggregate_numeric, validate_aggregation
from minio_followup_store import apply_followup_operation, InvalidFollowupOperation
from test_complex_execution_correctness import service, context, persist, IDENTITY


@pytest.mark.parametrize('operation,a,b,expected',[
    ('square',-3,None,9),('cube',-3,None,-27),('sqrt',9,None,3),('cbrt',-8,None,-2),
    ('abs',-2,None,2),('negate',3,None,-3),('reciprocal',4,None,.25),('sign',-3,None,-1),
    ('exp',0,None,1),('ln',1,None,0),('log10',100,None,2),('log2',8,None,3),
    ('floor',-1.2,None,-2),('ceil',-1.2,None,-1),('trunc',-1.8,None,-1),('round',2.5,None,3),
    ('factorial',5,None,120),('sin',math.pi/2,None,1),('cos',0,None,1),('tan',0,None,0),
    ('asin',1,None,math.pi/2),('acos',1,None,0),('atan',1,None,math.pi/4),
    ('sinh',0,None,0),('cosh',0,None,1),('tanh',0,None,0),
    ('asinh',0,None,0),('acosh',1,None,0),('atanh',0,None,0),
    ('exp2',3,None,8),('expm1',1e-20,None,1e-20),('log1p',1e-20,None,1e-20),
    ('degrees',math.pi,None,180),('radians',180,None,math.pi),
    ('power',2,10,1024),('power',-2,3,-8),('root',-32,5,-2),('root',16,-2,.25),
    ('modulo',-5,3,1),('modulo',5,-3,-1),('floor_divide',-5,3,-2),('log',8,2,3),
    ('hypot',3,4,5),('atan2',1,0,math.pi/2),('minimum',2,3,2),('maximum',2,3,3),
    ('gcd',12,18,6),('lcm',4,6,12),('combination',5,2,10),('permutation',5,2,20),
    ('bit_not',2,None,-3),('bit_and',6,3,2),('bit_or',6,3,7),('bit_xor',6,3,5),
    ('shift_left',3,2,12),('shift_right',-8,2,-2),
    ('add',.1,.2,.3),('subtract',3,5,-2),('multiply',-3,0,0),('divide',10,-4,-2.5),
])
def test_supported_calculator_operations(operation,a,b,expected):
    assert evaluate(operation,a,b)==pytest.approx(expected)


@pytest.mark.parametrize('operation,a,b',[
    ('sqrt',-1,None),('root',-2,2),('root',1,0),('root',0,-3),('power',0,0),
    ('power',0,-1),('power',-1,.5),('ln',0,None),('log',2,1),('log',2,-1),
    ('reciprocal',0,None),('divide',1,0),('modulo',1,0),('floor_divide',1,0),
    ('asin',2,None),('acos',-2,None),('tan',math.pi/2,None),('factorial',-1,None),
    ('factorial',171,None),('factorial',1.5,None),('exp',10000,None),('square',1e308,None),
    ('power',2,10001),('shift_left',1,53),('bit_and',1.5,2),('gcd',2.5,2),('combination',2,3),
    ('acosh',0,None),('atanh',1,None),('log1p',-1,None),
])
def test_invalid_domains_and_overflow_do_not_emit_nan_or_infinity(operation,a,b):
    with pytest.raises(ValueError):evaluate(operation,a,b)


@pytest.mark.parametrize('value',[None,True,'NaN','Infinity','not numeric'])
def test_non_numeric_inputs_are_rejected(value):
    with pytest.raises(ValueError):evaluate('square',value)


def test_rounding_is_half_up_and_supports_decimal_places_and_negative_places():
    assert evaluate('round',2.675,digits=2)==2.68
    assert evaluate('round',-2.5)==-3
    assert evaluate('round',1250,digits=-2)==1300
    with pytest.raises(ValueError):evaluate('round',1,digits=True)


@pytest.mark.parametrize('function,expected',[
    ('median',2.5),('quantile',2.5),('percentile',2.5),('variance',1.25),
    ('variance_population',1.25),('variance_sample',5/3),
    ('stddev',math.sqrt(1.25)),('stddev_population',math.sqrt(1.25)),('stddev_sample',math.sqrt(5/3)),
    ('range',3),('product',24),('sum_squares',30),('rms',math.sqrt(7.5)),
    ('geometric_mean',24**.25),('harmonic_mean',4/(1+1/2+1/3+1/4)),('count_distinct',4),
])
def test_statistics_match_independent_definitions(function,expected):
    assert aggregate_numeric([{'x':value} for value in [1,2,3,4,None]],{'field':'x','function':function})==pytest.approx(expected)


def test_stable_variance_and_explicit_sample_ddof():
    values=[str(10**25+i) for i in range(4)]
    assert aggregate_numeric([{'x':value} for value in values],{'field':'x','function':'variance'})==1.25
    rows=[{'x':1},{'x':2},{'x':3}]
    assert aggregate_numeric(rows,{'field':'x','function':'variance','ddof':1})==statistics.variance([1,2,3])
    assert aggregate_numeric(rows,{'field':'x','function':'variance','ddof':0})==statistics.pvariance([1,2,3])
    with pytest.raises(ValueError):validate_aggregation({'field':'x','function':'variance_sample','ddof':0})


@pytest.mark.parametrize('q,expected',[(0,1),(.25,1.75),(.75,3.25),(1,4)])
def test_quantiles_interpolate_and_include_endpoints(q,expected):
    assert aggregate_numeric([{'x':i} for i in [1,2,3,4]],{'function':'quantile','field':'x','q':q})==expected


@pytest.mark.parametrize('function,expected',[
    ('correlation',1),('covariance_population',4/3),('covariance_sample',2)])
def test_pairwise_statistics_use_complete_pairs(function,expected):
    rows=[{'x':1,'y':2},{'x':2,'y':4},{'x':3,'y':6},{'x':None,'y':100},{'x':100,'y':None}]
    assert aggregate_numeric(rows,{'function':function,'field':'x','other_field':'y'})==pytest.approx(expected)


def test_empty_singleton_and_constant_statistical_domains():
    with pytest.raises(ValueError):aggregate_numeric([{'x':None}],{'field':'x','function':'variance_population'})
    assert aggregate_numeric([{'x':7}],{'field':'x','function':'variance_population'})==0
    with pytest.raises(ValueError):aggregate_numeric([{'x':7}],{'field':'x','function':'variance_sample'})
    with pytest.raises(ValueError):aggregate_numeric([{'x':1,'y':2},{'x':1,'y':3}],{'field':'x','other_field':'y','function':'correlation'})
    for function in ['geometric_mean','harmonic_mean']:
        with pytest.raises(ValueError):aggregate_numeric([{'x':0}],{'field':'x','function':function})


def test_grouped_aggregates_preserve_group_keys_and_input_rows():
    rows=[{'code':'A','x':1},{'code':'B','x':10},{'code':'A','x':3}]
    operation={'type':'aggregate','group_by':['code'],'aggregations':[{'field':'x','function':'variance_population','alias':'variance'}]}
    columns,result=apply_followup_operation(['code','x'],rows,operation)
    assert result==[{'code':'A','variance':1},{'code':'B','variance':0}]
    assert rows[0]=={'code':'A','x':1}


def test_legacy_null_division_and_new_overflow_protection():
    for value in [None,0]:
        _,rows=apply_followup_operation(['a','b'],[{'a':1,'b':value}],{'type':'derive','operator':'divide','left_field':'a','right_field':'b','result_field':'c'})
        assert rows[0]['c'] is None
    with pytest.raises(InvalidFollowupOperation):
        apply_followup_operation(['a','b'],[{'a':1e308,'b':1e308}],{'type':'derive','operator':'multiply','left_field':'a','right_field':'b','result_field':'c'})
    _,rows=apply_followup_operation(['a','b'],[{'a':'1,200.25','b':'0.75'}],{'type':'derive','operator':'add','left_field':'a','right_field':'b','result_field':'c'})
    assert rows[0]['c']==1201


def test_legacy_store_still_runs_standalone_without_agent_modules(tmp_path):
    source=Path(__file__).resolve().parents[1]/'minio_followup_store.py'
    (tmp_path/source.name).write_bytes(source.read_bytes())
    program="import sys;sys.path.insert(0,sys.argv[1]);from minio_followup_store import apply_followup_operation;cols,rows=apply_followup_operation(['x','y'],[{'x':6,'y':3}],{'type':'derive','operator':'divide','left_field':'x','right_field':'y','result_field':'z'});assert rows[0]['z']==2;cols,rows=apply_followup_operation(cols,rows,{'type':'aggregate','group_by':[],'aggregations':[{'function':'sum','field':'z','alias':'total'}]});assert rows[0]['total']==2;assert 'app' not in sys.modules"
    result=subprocess.run([sys.executable,'-I','-c',program,str(tmp_path)],cwd=tmp_path,capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors='replace')


def test_planner_cold_import_does_not_import_the_orchestrator_in_a_cycle(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script="import sys;sys.path.insert(0,sys.argv[1]);from app.planning.task_dag import SingleQuestionPlanner;from app.numeric_operations import evaluate;assert evaluate('sqrt',9)==3"
    result=subprocess.run([sys.executable,'-I','-c',script,str(root)],cwd=tmp_path,capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors='replace')


def test_unary_and_constant_bindings_materialize_and_follow_deduplication():
    data={'计算绑定':{'operation':'sqrt','input':{'source_task_index':0,'column':'x'},'result_field':'root'}}
    result=materialize_computation_binding(data,task_index=1,task_offset=3,dependencies=['task-4'])
    assert result['计算绑定']['input']['source_task_id']=='task-4'
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='a',question='查询指标'),AtomicTask(task_id='b',question='查询指标'),
        AtomicTask(task_id='c',question='开根',depends_on=['a','b'],extraction={'计算绑定':{'operation':'sqrt','input':{'source_task_id':'b','column':'x'},'result_field':'root'}})])
    reduced,_=deduplicate_plan(plan)
    assert reduced.tasks[-1].extraction['计算绑定']['input']['source_task_id']=='a'
    bad={'计算绑定':{'operation':'power','left':{'value':2,'source_task_index':0},'right':{'value':3}}}
    with pytest.raises(TaskInputBindingError):materialize_computation_binding(bad,task_index=1,dependencies=['task-1'])
    with pytest.raises(ValueError):validate_result_operation({'type':'derive','operator':'square','field':'x','left_field':'x','result_field':'z'})
    with pytest.raises(ValueError):validate_result_operation({'type':'derive','operator':'power','left_field':'x','right_value':'2','result_field':'z'})
    with pytest.raises(TaskInputBindingError):materialize_computation_binding({'计算绑定':{'operation':[]}},task_index=0,dependencies=[])


@pytest.mark.parametrize('definition,operation',[
    ('付款金额的平方','square'),('付款金额的平方根','sqrt'),('付款金额的立方根','cbrt'),
    ('sqrt(付款金额)','sqrt'),('ln(付款金额)','ln'),('付款金额 ^ 2','power')])
def test_registered_unary_and_constant_formulas(definition,operation):
    spec=f'| 新指标 | 无 | {definition} | 元 |'
    assert registered_arithmetic_binding(spec,'新指标',('付款金额',))['operation']==operation


def test_formula_binding_does_not_truncate_a_multi_step_expression():
    assert registered_arithmetic_binding('| 均额 | 无 | 金额 / 数量 + 1 | 元 |','均额',('金额','数量')) is None


@pytest.mark.asyncio
async def test_role_resolver_cannot_accept_a_truncated_multi_step_formula():
    from app.planning.calculation_bindings import resolve_compound_formulas
    with pytest.raises(ValueError,match='多步'):
        await resolve_compound_formulas(service().settings,None,'| 均额 | 无 | 金额 / 数量 + 1 | 元 |',
                                        {'均额':('金额','数量')},['均额'])


@pytest.mark.asyncio
async def test_saved_multi_step_statistics_and_constants_can_be_reused():
    agent=service();chat,_=context()
    await persist(agent,chat,['编码','x'],[{'编码':'A','x':2},{'编码':'B','x':4},{'编码':'C','x':6}],conversation='query')
    square=AtomicTask(task_id='square',question='计算平方',depends_on=['query'],extraction={'是否查询数据库':'否','计算绑定':{
        'operation':'power','left':{'source_task_id':'query','column':'x'},'right':{'value':2},'result_field':'squared'}})
    squared=await agent._respond_pure_computation(chat,IDENTITY,square,{'query':'query'})
    assert squared.status=='COMPLETED'
    task=AtomicTask(task_id='summary',question='计算均方根和平方值样本方差',depends_on=['square'],extraction={'是否查询数据库':'否','结果操作':{
        'source_task_id':'square','operation':{'type':'pipeline','operations':[
            {'type':'aggregate','group_by':[],'aggregations':[{'field':'squared','function':'avg','alias':'mean_square'},
                {'field':'squared','function':'variance_sample','alias':'sample_variance'}]},
            {'type':'derive','operator':'sqrt','field':'mean_square','result_field':'rms'}]}}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'square':squared.conversation_id},{'square':squared})
    assert result.status=='COMPLETED'
    row=result.evidence[0].payload['preview'][0]
    assert row['rms']==pytest.approx(math.sqrt(56/3))
    assert row['sample_variance']==pytest.approx(statistics.variance([4,16,36]))
    assert 'n-1' in result.answer


@pytest.mark.asyncio
async def test_literal_only_calculation_needs_no_database_or_predecessor():
    agent=service();chat,_=context()
    task=AtomicTask(task_id='constant',question='计算2的10次方',depends_on=[],extraction={'是否查询数据库':'否','计算绑定':{
        'operation':'power','left':{'value':2},'right':{'value':10},'result_field':'结果'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{})
    assert result.status=='COMPLETED'
    assert result.evidence[0].payload['preview']==[{'结果':1024}]


def test_single_calculator_request_keeps_seven_stages_and_never_queries():
    import json,re
    from app.domain.models import PlannerExtraction,PrimaryIntent
    from app.planning.compat import PlanningResult
    from test_api import TestClient,build_test_app
    agent=service()
    class Planner:
        async def plan(self,question,**kwargs):
            return PlanningResult(plan=None,single_extraction=PlannerExtraction(intent=PrimaryIntent.CHAT,
                structured={'是否查询数据库':'否','计算绑定':{'operation':'power','left':{'value':2},'right':{'value':10},'result_field':'结果'}}))
    agent.task_planner=Planner()
    async def unexpected(*args,**kwargs):raise AssertionError('calculator must not enter database handling')
    agent._handle=unexpected
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):return {'response':await agent.handle(state['chat'],state['identity'])}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'literal-computation-stages','message_id':'m1','question':'计算2的10次方'})
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    assert re.findall(r'#### ◉ ([^\r\n]+)',text)==['意图识别','任务拆分与规划','解析校验','调度执行','结果校验','数据洞察分析','最终输出']
    final=next(e for e in events if e.get('type')=='complete')
    assert final['status']=='COMPLETED' and '1024' in final['answer'].replace(',','')


def test_literal_shortcut_cannot_discard_business_data_conditions():
    from app.services.calculation_execution import literal_computation
    data={'计算绑定':{'operation':'square','input':{'value':2},'result_field':'结果'}}
    assert literal_computation(data)
    assert not literal_computation({**data,'过滤条件':[{'field':'地区','value':['上海']}]})
    assert not literal_computation({**data,'指标':[{'name':'销售总额'}]})
    assert not literal_computation({**data,'时间粒度':{'time_range':'2025年'}})


@pytest.mark.parametrize('question,operation',[
    ('计算2的10次方','power'),('求9的平方根','sqrt'),('pow(2,10)','power'),
    ('round(2.675,2)','round'),('5的阶乘','factorial'),('1+2','add'),('10/-4','divide')])
def test_unambiguous_literal_math_is_recognized_without_guessing_business_fields(question,operation):
    from app.numeric_operations import parse_literal_binding
    result=parse_literal_binding(question)
    assert result is not None and result['operation']==operation


@pytest.mark.parametrize('question',[
    '查询2025年各业务员订单笔数平方','计算上海销售额的平方','2+3*4','-2**2',
    "__import__('os').system('echo wrong')",'生日是2月10日','讲一个2的10次方的故事'])
def test_calculator_recognition_does_not_consume_data_requests_or_code(question):
    from app.numeric_operations import parse_literal_binding
    assert parse_literal_binding(question) is None


def test_pipeline_steps_alias_is_normalized_before_analysis_can_drop_it():
    from app.planning.calculation_bindings import normalize_result_operation
    step={'type':'derive','operator':'sqrt','field':'x','result_field':'root'}
    operation=normalize_result_operation({'type':'pipeline','steps':[step]})
    assert operation=={'type':'pipeline','operations':[step]}
    assert validate_result_operation(operation)==[step]
    with pytest.raises(ValueError):normalize_result_operation({'type':'pipeline','steps':[step],'operations':[]})


@pytest.mark.parametrize('valid,queries,expected,math_goal',[(True,1,2,False),(False,1,1,False),(False,2,3,False),(False,1,2,True)])
def test_analysis_fallback_preserves_valid_math_and_all_retrieval_dependencies(valid,queries,expected,math_goal):
    from app.domain.models import PrimaryIntent
    from app.planning.task_dag import DecomposedTask,QuestionDecomposition,_collapse_analysis_computation_tasks
    query=[DecomposedTask(question=f'查询分组指标{i}',structured={'指标':[{'name':'订单笔数'}]}) for i in range(queries)]
    operation={'type':'derive','operator':'sqrt','field':'订单笔数','result_field':'平方根'} if valid else {'type':'invented_trend'}
    compute=DecomposedTask(question=('计算样本方差' if math_goal else '根据前序结果计算')+'，不再查询数据库',depends_on=tuple(range(queries)),structured={
        '是否查询数据库':'否','结果操作':{'source_task_index':0,'operation':operation}})
    plan=QuestionDecomposition(intent=PrimaryIntent.COMPARISON_ANALYSIS,tasks=tuple(query+[compute]))
    result=_collapse_analysis_computation_tasks(plan)
    assert len(result.tasks)==expected


@pytest.mark.asyncio
async def test_undefined_operation_is_explained_without_pretending_execution_completed():
    agent=service();chat,_=context()
    task=AtomicTask(task_id='undefined',question='负数开平方',depends_on=[],extraction={'是否查询数据库':'否','计算绑定':{
        'operation':'sqrt','input':{'value':-1},'result_field':'结果'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{})
    assert result.status=='FAILED' and not result.dataset_id
    assert result.evidence[0].payload.get('reason')


@pytest.mark.asyncio
async def test_planning_uses_full_group_samples_then_statistical_postprocess(monkeypatch):
    from test_task_planning_stages import setup_planner
    from app.planning.compound_question import CompoundJudgment,CompoundQuestionRouter
    question='查询2025年各业务员订单笔数，计算这些订单笔数的总体方差和标准差'
    operation={'type':'aggregate','group_by':[],'aggregations':[
        {'field':'订单笔数','function':'variance_population','alias':'总体方差'},
        {'field':'订单笔数','function':'stddev_population','alias':'总体标准差'}]}
    planner,calls=setup_planner(monkeypatch,{'tasks':[
        {'question':'查询2025年各业务员订单笔数完整分组数据','metric_targets':['订单笔数'],'grain':['业务员']},
        {'question':'根据前序各业务员的订单笔数计算总体方差和总体标准差，不再查询数据库',
            'depends_on':[0],'postprocess':{'source_task_index':0,'operation':operation}}]})
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    result=await router.plan_with_judgment(CompoundJudgment(is_compound=False),question,semantic_model_id=130)
    assert result.plan is not None and len(result.plan.tasks)==2 and len(calls)==2
    assert result.plan.tasks[0].extraction['指标']==[{'name':'订单笔数'}]
    assert result.plan.tasks[0].extraction['维度']==['业务员']
    assert result.plan.tasks[1].extraction['结果操作']['operation']==operation
