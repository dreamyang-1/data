"""Registered measure constraints are grounded without authorizing other numbers."""
import pytest
from app.domain.models import AtomicTask,TaskPlan
from app.planning.compound_question import CompoundJudgment,CompoundQuestionRouter,validate_plan
from app.planning.task_dag import TaskPlanningError
from app.planning.task_plan_stages import registered_metric_conditions
from test_task_planning_stages import SPEC,QUESTION,setup_planner


SCOPED_SPEC=SPEC.replace('先按订单号汇总，含税销售总额 ÷ 销售总数量',
    '先按订单号汇总，含税销售总额 ÷ 销售总数量。销量大于0')
CATALOG=(frozenset({'订单级均价','销售总数量','含税销售总额'}),{'销量':'销售总数量'})


@pytest.mark.parametrize('predicate',['销量大于0','销量>0','销售总数量>0'])
def test_catalog_predicate_is_accepted_without_user_repeating_it(predicate):
    rules=registered_metric_conditions(SCOPED_SPEC,['订单级均价'],CATALOG)
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='a',question='统计销售总数量，'+predicate),
        AtomicTask(task_id='b',question='按订单计算均价',depends_on=['a'])])
    validate_plan(plan,source_question=QUESTION,registered_conditions=rules)


@pytest.mark.parametrize('predicate',['销量>1','销量>=0','销量>0.5','销量>0，最近0个月','其他数量>0'])
def test_registered_zero_is_not_a_blanket_number_exemption(predicate):
    rules=registered_metric_conditions(SCOPED_SPEC,['订单级均价'],CATALOG)
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='a',question='统计销售总数量，'+predicate),
        AtomicTask(task_id='b',question='按订单计算均价',depends_on=['a'])])
    with pytest.raises(TaskPlanningError,match='数字或日期'):
        validate_plan(plan,source_question=QUESTION,registered_conditions=rules)


def test_unselected_metric_definition_cannot_supply_grounding_conditions():
    assert registered_metric_conditions(SCOPED_SPEC,['含税销售总额'],CATALOG)==()


@pytest.mark.asyncio
async def test_real_router_keeps_decomposed_tasks_with_registered_sample_rule(monkeypatch):
    planner,_=setup_planner(monkeypatch,{'tasks':[{'question':QUESTION+'，销量>0',
        'metric_targets':['订单级均价'],'expected_output':'逐订单均价分布'}]})
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=SCOPED_SPEC
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    result=await router.plan_with_judgment(CompoundJudgment(is_compound=False),QUESTION,semantic_model_id=130)
    assert len(result.plan.tasks)==3
    assert result.plan.tasks[-1].depends_on==['task-1','task-2']


@pytest.mark.asyncio
async def test_joint_formula_inputs_keep_order_grain_and_registered_sample_conditions(monkeypatch):
    plan={'tasks':[{'question':'按订单统计金额和数量','metric_targets':['含税销售总额','销售总数量'],
        'grain':['订单号'],'expected_output':'逐订单金额和数量'},
        {'question':'根据前序数据计算订单级均价并描述分布，不再查询数据库',
         'metric_targets':['订单级均价'],'depends_on':[0]}]}
    def extract(request):
        return {'tasks':[{'task_index':item['task_index'],'extraction':{'意图':'指标查询',
            '展示字段':[{'field':'订单号'},{'field':'含税金额'},{'field':'销售数量'},{'field':'所属销售公司'}],
            '指标':[],'限制':1000,'排序':[{'field':'含税销售总额','order':'desc'}]}}
            for item in request['tasks']]}
    planner,_=setup_planner(monkeypatch,plan,extract=extract)
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=SCOPED_SPEC.replace('销售总数量 | 销量 |','销售总数量 | 销量,销售数量 |')
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    source=result.tasks[0].structured
    assert source['维度']==['订单号'] and source['限制'] is None and source['排序']==[]
    assert source['展示字段']==[{'field':'订单号'},{'field':'所属销售公司'}]
    assert source['过滤条件']==[{'field':'销售总数量','op':'>','value':[0]}]


@pytest.mark.asyncio
async def test_parameter_extraction_cannot_add_raw_pipeline_beside_registered_calculation(monkeypatch):
    def extract(request):
        return {'tasks':[{'task_index':item['task_index'],'extraction':{'意图':'指标查询',
            '结果操作':{'source_task_index':0,'operation':{'type':'aggregate','group_by':['订单号'],
                'aggregations':[{'field':'含税金额','function':'sum','alias':'原始金额'}]}}}}
            for item in request['tasks']]}
    planner,_=setup_planner(monkeypatch,{'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]},extract=extract)
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert result.tasks[-1].structured['计算绑定']['operation']=='divide'
    assert all('结果操作' not in task.structured for task in result.tasks)


@pytest.mark.asyncio
async def test_separate_real_order_key_inputs_align_by_identity_not_row_position():
    from app.domain.models import AtomicTask
    from test_complex_execution_correctness import service,context,persist,IDENTITY
    agent=service();chat,_=context()
    await persist(agent,chat,['订单号','金额'],[{'订单号':'A','金额':100},{'订单号':'B','金额':900}],conversation='a',
        column_bindings={'订单号':'sales_order.order_key'})
    await persist(agent,chat,['订单标识','数量'],[{'订单标识':'B','数量':3},{'订单标识':'A','数量':1}],conversation='b',
        column_bindings={'订单标识':'sales_order.order_key'})
    task=AtomicTask(task_id='c',question='按订单计算均价',depends_on=['a','b'],extraction={'计算绑定':{
        'operation':'divide','numerator':{'source_task_id':'a','column':'金额'},
        'denominator':{'source_task_id':'b','column':'数量'},'result_field':'均价'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'a','b':'b'})
    assert result.status=='COMPLETED'
    rows=chat._dag_deferred_insight['facts']['query_data']['rows']
    assert {row['订单号']:row['均价'] for row in rows}=={'A':100,'B':300}


@pytest.mark.asyncio
async def test_narrative_task_with_no_query_metrics_does_not_survive_as_empty_execution(monkeypatch):
    plan={'tasks':[{'question':'查询订单级均价明细','metric_targets':['订单级均价']},
        {'question':'基于任务0的订单级均价明细描述分布，不再查询数据库','depends_on':[0],
         'metric_targets':[],'expected_output':'订单级均价的分布描述'}]}
    planner,_=setup_planner(monkeypatch,plan)
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    result=await router.plan_with_judgment(CompoundJudgment(is_compound=False),QUESTION,semantic_model_id=130)
    assert len(result.plan.tasks)==3
    assert '分布' in result.plan.tasks[-1].question


@pytest.mark.parametrize('reference',['任务0','任务1','第1个任务'])
def test_predecessor_task_reference_is_not_a_business_number(reference):
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='a',question='查询销售数据'),
        AtomicTask(task_id='b',question='根据'+reference+'返回的数据计算均价',depends_on=['a'])])
    validate_plan(plan,source_question='查询数据并计算均价')


def test_undeclared_task_reference_is_not_silently_grounded():
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='a',question='查询销售数据'),
        AtomicTask(task_id='b',question='根据任务8返回的数据计算均价',depends_on=['a'])])
    with pytest.raises(TaskPlanningError,match='数字或日期'):
        validate_plan(plan,source_question='查询数据并计算均价')


WHOLE_SPEC = SCOPED_SPEC.replace('先按订单号汇总，',
    '先按订单号汇总该订单下全部商品的含税金额总和、销售数量总和，') + '''
| 实体名称 | 属性名称 |
| --- | --- |
| 销售订单 | 订单号 |
| 商品 | 商品名称 |
| 医院 | 医院名称 |
'''


@pytest.mark.asyncio
async def test_whole_order_scope_is_planned_before_parameters_and_not_filtered_to_selected_product(monkeypatch):
    def extract(request):
        return {'tasks':[{'task_index':item['task_index'],'extraction':{'意图':'指标查询',
            '实体':['销售订单','商品','医院'],'维度':['商品名称'],
            '展示字段':[{'field':'商品名称'},{'field':'订单号'}],
            '过滤条件':[{'entity':'商品','field':'商品名称','op':'=','value':['B']}],
            '时间粒度':{'unit':None,'time_range':'最近12个月'}}} for item in request['tasks']]}
    planner,calls=setup_planner(monkeypatch,{'tasks':[{'question':QUESTION,
        'metric_targets':['订单级均价'],'expected_output':'逐订单均价完整分布'}]},extract=extract)
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=WHOLE_SPEC
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert len(calls)==2 and len(result.tasks)==3
    selection,*inputs,calculation=result.tasks
    assert '完整名单' in selection.question and selection.structured['指标']==[]
    assert selection.structured['过滤条件'][0]['value']==['B']
    assert selection.structured['展示字段']==[{'field':'订单号'}]
    assert selection.structured['维度']==['订单号'] and selection.structured['限制'] is None
    assert all(task.depends_on==(0,) for task in inputs)
    for task in inputs:
        assert '全部商品' in task.question and task.structured['是否查询数据库']=='是'
        assert task.structured['维度']==['订单号']
        assert task.structured['展示字段']==[{'field':'订单号'}]
        assert task.structured['过滤条件']==[{'field':'销售总数量','op':'>','value':[0]}]
        assert task.structured['时间粒度']['time_range'] is None
    assert calculation.depends_on==(1,) and '分布' in calculation.question


@pytest.mark.asyncio
async def test_existing_eligibility_query_is_reused_by_joint_whole_order_input(monkeypatch):
    plan={'tasks':[{'question':'查询医院A采购商品B的订单号完整名单','metric_targets':[],'grain':['订单号']},
        {'question':'根据上述结果按订单号统计含税销售总额和销售总数量','depends_on':[0],
         'metric_targets':['含税销售总额','销售总数量'],'grain':['订单号']},
        {'question':'根据前序数据计算订单级均价并分析分布，不再查询数据库',
         'depends_on':[1],'metric_targets':['订单级均价']}]}
    planner,_=setup_planner(monkeypatch,plan)
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=WHOLE_SPEC
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert len(result.tasks)==3
    assert result.tasks[1].depends_on==(0,) and '全部商品' in result.tasks[1].question
    binding=result.tasks[2].structured['计算绑定']
    assert binding['numerator']['source_task_index']==binding['denominator']['source_task_index']==1


@pytest.mark.asyncio
async def test_inserted_eligibility_task_cannot_leave_old_textual_task_reference(monkeypatch):
    plan={'tasks':[{'question':'查询医院A采购商品B的订单金额和数量',
        'metric_targets':['含税销售总额','销售总数量']},
        {'question':'基于任务0计算订单级均价并分析分布','depends_on':[0],
         'metric_targets':['订单级均价']}]}
    planner,_=setup_planner(monkeypatch,plan)
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=WHOLE_SPEC
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    result=await router.plan_with_judgment(CompoundJudgment(is_compound=False),QUESTION,semantic_model_id=130)
    assert len(result.plan.tasks)==3 and result.plan.tasks[-1].depends_on==['task-2']
    assert '任务0' not in result.plan.tasks[-1].question
    assert result.plan.tasks[-1].extraction['计算绑定']['numerator']['source_task_id']=='task-2'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',[{'pipeline':[{'derive':'金额/数量'}]},
    {'type':'derive','left_field':'含税销售总额','right_field':'销售总数量',
     'operator':'divide','result_field':'订单级均价'}])
async def test_key_list_cannot_pretend_to_supply_missing_registered_formula_inputs(monkeypatch,operation):
    plan={'tasks':[{'question':'查询医院A购买商品B的订单号完整列表','metric_targets':[],
                   'expected_output':'完整去重订单号名单'},
        {'question':'根据上述订单号名单计算订单级均价并分析分布，不再查询数据库',
         'metric_targets':['订单级均价'],'depends_on':[0],'component_input':True,
         'expected_output':'各订单均价与完整分布分析',
         'postprocess':{'source_task_index':0,'operation':operation}}]}
    planner,_=setup_planner(monkeypatch,plan)
    from app.domain import semantic_description
    semantic_description.load_semantic_description.return_value['content']=WHOLE_SPEC
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert len(result.tasks)==3 and result.tasks[-1].depends_on==(1,)
    assert all(task.depends_on==(0,) and task.structured['是否查询数据库']=='是' for task in result.tasks[1:-1])
    assert all('全部商品' in task.question for task in result.tasks[1:-1])
    assert {item['name'] for item in result.tasks[1].structured['指标']}=={'含税销售总额','销售总数量'}
    assert result.tasks[-1].structured['是否查询数据库']=='否' and '分布' in result.tasks[-1].question


def test_detail_list_with_actual_formula_inputs_keeps_its_valid_local_operation():
    from app.planning.task_plan_stages import TaskBoundary,complete_boundaries
    operation={'type':'pipeline','operations':[
        {'type':'aggregate','group_by':['订单号'],'aggregations':[
            {'field':'含税金额','function':'sum','alias':'含税销售总额'},
            {'field':'销售数量','function':'sum','alias':'销售总数量'}]},
        {'type':'derive','left_field':'含税销售总额','right_field':'销售总数量',
         'operator':'divide','result_field':'订单级均价'}]}
    tasks=[TaskBoundary(question='查询入选订单全部商品的订单号、含税金额、销售数量明细列表',metric_targets=[]),
        TaskBoundary(question='根据前序明细计算订单级均价，不再查询数据库',depends_on=[0],
            metric_targets=['订单级均价'],postprocess={'source_task_index':0,'operation':operation})]
    result=complete_boundaries(tasks,spec=WHOLE_SPEC,catalog=CATALOG,
        dependencies={'订单级均价':('销售总数量','含税销售总额')})
    assert len(result)==2 and result[-1].postprocess['operation']==operation


def test_unfiltered_whole_order_metric_does_not_add_unneeded_eligibility_query():
    from app.planning.task_plan_stages import TaskBoundary,complete_boundaries
    result=complete_boundaries([TaskBoundary(question='统计各订单的订单级均价分布',
        metric_targets=['订单级均价'])],spec=WHOLE_SPEC,catalog=CATALOG,
        dependencies={'订单级均价':('销售总数量','含税销售总额')})
    assert len(result)==3 and result[0].depends_on==result[1].depends_on==[]


def test_eligibility_rewrite_preserves_catalog_default_time_without_old_display_fields():
    from app.planning.task_plan_stages import TaskBoundary,complete_boundaries
    result=complete_boundaries([TaskBoundary(question=QUESTION+'，最近12个月，返回含税金额、销售数量',
        metric_targets=['订单级均价'])],spec=WHOLE_SPEC,catalog=CATALOG,
        dependencies={'订单级均价':('销售总数量','含税销售总额')},source_question=QUESTION,
        default_times=('最近12个月',))
    assert '最近12个月' in result[0].question and '返回含税金额' not in result[0].question


@pytest.mark.asyncio
async def test_whole_order_dependency_uses_all_order_keys_not_product_codes_or_preview():
    from test_complex_execution_correctness import service,context,persist,IDENTITY
    from app.domain.models import AgentResponse,PrimaryIntent
    from uuid import uuid4
    agent=service();chat,_=context()
    rows=[{'订单号':f'O-{index:02d}','商品编码':'P-001','商品名称':'样例商品'} for index in range(13)]
    ref=await persist(agent,chat,list(rows[0]),rows,conversation='orders',column_bindings={
        '订单号':'sales_order.order_key','商品编码':'product.product_code','商品名称':'product.product_name'})
    response=AgentResponse(request_id=uuid4(),conversation_id='orders',status='COMPLETED',
        intent=PrimaryIntent.DETAIL_QUERY,answer='仅展示前10条预览',dataset_id=ref.dataset_id)
    task=AtomicTask(task_id='totals',question='根据上述任务返回的订单号完整名单，按订单号汇总该订单下全部商品的含税销售总额',
        depends_on=['task-1'])
    constraints=await agent._compile_dependency_constraints(task,chat,IDENTITY,
        {'task-1':response},{'task-1':'orders'})
    assert constraints[0].source_column=='sales_order.order_key'
    assert constraints[0].values==[row['订单号'] for row in rows]
    assert agent._dependency_key_families('task-1',TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id='task-1',question='查询订单名单'),task]))==('order',)


def test_local_calculation_does_not_add_database_join_prompt_or_business_mentions():
    from test_complex_execution_correctness import service
    agent=service();question='按订单号汇总整单金额和数量'
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='task-1',question=question),
        AtomicTask(task_id='task-2',question='基于前序任务返回的订单结果计算均价，不再查询数据库',
            depends_on=['task-1'],extraction={'是否查询数据库':'否'})])
    assert agent._with_dependency_join_keys(question,'task-1',plan)==question
    plan.tasks[-1].question='根据上述任务返回的订单号完整名单查询整单金额'
    plan.tasks[-1].extraction={'是否查询数据库':'是'}
    prompt=agent._with_dependency_join_keys('查询订单号名单','task-1',plan)
    assert '订单号作为精确关联键' in prompt and 'display-name' not in prompt


@pytest.mark.parametrize('grouped',[True,False])
def test_explicit_order_projection_is_preserved_by_required_aggregate_grouping(grouped):
    from test_complex_execution_correctness import context
    from app.domain.models import PrimaryIntent
    from app.services.turn_admission import TurnAdmissionGate
    from app.services.intent_asl_contract import build_intent_asl_contract,validate_intent_asl_contract_completeness
    _,request=context()
    request.primary_intent=PrimaryIntent.METRIC_QUERY
    request.entity='销售订单';request.fields=['订单号'];request.dimensions=['订单号']
    request.turn_admission=TurnAdmissionGate().evaluate(question='按订单号统计金额，返回订单号',
        current=request,previous=None,message_id='grouping-projection')
    if not grouped:request.dimensions=[]
    errors=validate_intent_asl_contract_completeness(build_intent_asl_contract(request),request)
    assert any(item['code']=='EXPLICIT_PROJECTION_MISSING' for item in errors) is not grouped


def test_confirmed_scoped_grouping_is_not_rebound_through_static_field_name_heuristics():
    from test_complex_execution_correctness import context
    from app.adapters.http import HttpDataRetrievalAdapter
    from app.adapters.base import AdapterError
    from app.domain.models import PrimaryIntent
    _,request=context();request.primary_intent=PrimaryIntent.METRIC_QUERY
    request.dimensions=['订单号'];request.filters=[]
    asl={'dimensions':[{'name':'sales_order.order_key'}]}
    with pytest.raises(AdapterError):HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(asl,request)
    HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(asl,request,contract_confirmed=True)
    with pytest.raises(AdapterError):
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions({'dimensions':[]},request,contract_confirmed=True)
    request.assumptions.append('STRICT_GROUPING_DIMENSIONS')
    with pytest.raises(AdapterError):
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(asl,request,contract_confirmed=True)
