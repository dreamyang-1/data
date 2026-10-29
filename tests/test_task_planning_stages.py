"""Task boundaries and dependencies must survive later intent/parameter labels."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.domain.models import PrimaryIntent
from app.planning.compound_question import CompoundJudgment, CompoundJudgmentEntry, CompoundQuestionRouter, CompoundTaskPlanner
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner, TaskInputBindingError
from app.planning.task_plan_stages import TaskBoundary, complete_boundaries


SPEC = '''| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |
| --- | --- | --- | --- | --- | --- |
| 订单级均价 | 整单平均售价 | 先按订单号汇总，含税销售总额 ÷ 销售总数量 | 元 | 复合口径指标 | 销售总数量, 含税销售总额 |
| 平均每单销售金额 | 每单平均金额 | 含税销售总额 ÷ 订单笔数 | 元 | 复合口径指标 | 含税销售总额, 订单笔数 |
| 含税销售总额 | 含税销售额 | 含税金额合计 | 元 | 原子指标 | 无 |
| 销售总数量 | 销量 | 数量净额合计 | 件 | 原子指标 | 无 |
| 订单笔数 | 订单数 | 订单号去重数 | 笔 | 原子指标 | 无 |
'''
QUESTION = '分析医院A采购商品B的订单级均价分布'


def setup_planner(monkeypatch, plan, *, extract=None, calculate=None):
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'available': True, 'source': 'semantic_model_130.md', 'content': SPEC}))
    calls = []

    async def model(request):
        body = json.loads(request.content)
        calls.append(body)
        if '计算规划输入JSON：\n' in body['messages'][0]['content']:
            fixed = json.loads(body['messages'][0]['content'].split('计算规划输入JSON：\n', 1)[1])
            if calculate:
                payload = calculate(fixed)
            else:
                # Existing scenarios provide their calculator fixture through
                # extract; deliver it at the planner stage under the new contract.
                supplied = extract(fixed)['tasks'] if extract else []
                by_index = {item.get('task_index', i): item.get('extraction') or {}
                            for i, item in enumerate(supplied)}
                payload = {'tasks': [{'task_index': index, 'calculation': {
                    key: value for key, value in by_index.get(index, {}).items()
                    if key in {'计算绑定', '结果操作', '排序', '限制'}}}
                    for index in fixed['calculation_task_indices']]}
        elif '你是任务规划器' in body['messages'][0]['content']:
            # Planning has no query parameter schema or intent classification.
            schema = json.loads(body['messages'][0]['content'].split('JSON Schema：', 1)[1])
            assert 'extraction' not in str(schema) and 'intent' not in str(schema)
            payload = plan
        else:
            assert len(calls) >= 2
            fixed = json.loads(body['messages'][0]['content'].split('已规划任务JSON：\n',1)[1])
            if extract:
                payload = extract(fixed)
            else:
                payload = {'tasks': [{'task_index': task['task_index'], 'extraction': {
                    '意图': '指标计算' if '不再查询数据库' in task['question'] else '指标查询',
                    '指标': [{'name': name} for name in task['metric_targets']],
                    '维度': task['grain'], '展示字段': [], '过滤条件': [],
                    '是否查询数据库': '否' if '不再查询数据库' in task['question'] else '是',
                    '输出要求': task.get('expected_output')}} for task in fixed['tasks']]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}}]})

    settings = Settings(_env_file=None, env='test', intent_model_enabled=True,
        multi_question_model_enabled=True, intent_model_api_key=SecretStr('fixture'))
    return SingleQuestionPlanner(settings, transport=httpx.MockTransport(model)), calls


@pytest.mark.asyncio
async def test_component_plan_is_expanded_before_parameters_are_extracted(monkeypatch):
    planner, calls = setup_planner(monkeypatch, {'tasks': [{'question': QUESTION,
        'metric_targets': ['订单级均价'], 'expected_output': '逐订单均价样本及价格分布'}]})
    result = await planner.plan_question(QUESTION, semantic_model_id=130)
    assert len(calls) == 3  # boundaries, calculator plan, query extraction
    fixed = json.loads(calls[-1]['messages'][0]['content'].split('已规划任务JSON：\n',1)[1])['tasks']
    assert len(fixed) == len(result.tasks) == 3
    assert [task['metric_targets'] for task in fixed] == [['销售总数量'], ['含税销售总额'], ['订单级均价']]
    assert all(task['grain'] == ['订单号'] for task in fixed)
    assert [task.structured['指标'] for task in result.tasks] == [[{'name':'销售总数量'}],
        [{'name':'含税销售总额'}], [{'name':'订单级均价'}]]
    assert all(task.structured['维度'] == ['订单号'] for task in result.tasks)
    assert result.tasks[-1].depends_on == (0, 1)
    assert all(task.intent==PrimaryIntent.METRIC_QUERY for task in result.tasks[:2])
    binding = result.tasks[-1].structured['计算绑定']
    assert binding['numerator']['source_task_index'] == 1
    assert binding['denominator']['source_task_index'] == 0
    assert '分布' in result.tasks[-1].expected_output
    assert '分布' in result.tasks[-1].structured['输出要求']


@pytest.mark.asyncio
async def test_parameter_stage_cannot_rewrite_the_planned_question_or_dependency(monkeypatch):
    plan = {'tasks': [{'question':'查询销售额最高的经销商'},
        {'question':'根据上述结果查询这些经销商合作的医院', 'depends_on':[0]}]}
    def extract(request):
        return {'tasks':[{'task_index':index, 'question':'替换成其他问题', 'depends_on':[],
            'extraction':{'意图':'明细查询','展示字段':[], '是否查询数据库':'是'}}
            for index in reversed(range(len(request['tasks'])))]}
    planner, calls = setup_planner(monkeypatch, plan, extract=extract)
    result = await planner.plan_question('查询销售额最高的经销商及其合作医院', semantic_model_id=130)
    assert [task.question for task in result.tasks] == [task['question'] for task in plan['tasks']]
    assert result.tasks[1].depends_on == (0,) and len(calls) == 2


@pytest.mark.asyncio
async def test_missing_parameter_task_cannot_downgrade_a_plan_to_single_query(monkeypatch):
    planner, _ = setup_planner(monkeypatch, {'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]},
        extract=lambda request:{'tasks':[{'task_index':0,'extraction':{'意图':'明细查询'}}]})
    with pytest.raises(TaskInputBindingError, match='遗漏'):
        await planner.plan_question(QUESTION, semantic_model_id=130)


@pytest.mark.asyncio
async def test_multiple_requested_measures_survive_pre_extraction_expansion(monkeypatch):
    question='统计各经销商订单级均价、平均每单销售金额和订单笔数'
    planner, calls = setup_planner(monkeypatch, {'tasks':[{'question':question,
        'metric_targets':['订单级均价','平均每单销售金额','订单笔数'],'grain':['经销商编码']}]})
    result = await planner.plan_question(question, semantic_model_id=130)
    assert len(calls) == 3 and len(result.tasks) == 6
    outputs = [task.structured['指标'][0]['name'] for task in result.tasks]
    assert set(outputs) == {'订单级均价','平均每单销售金额','订单笔数','含税销售总额','销售总数量'}
    assert outputs.count('含税销售总额') == 2  # distinct registered sample grains
    assert result.tasks[-2].depends_on == (0,1)
    assert result.tasks[-1].depends_on == (2,3)


@pytest.mark.asyncio
async def test_ordinary_atomic_query_stays_single_and_needs_no_intent_only_call(monkeypatch):
    planner, calls = setup_planner(monkeypatch, {'tasks':[{'question':'统计上海订单笔数',
        'metric_targets':['订单笔数'],'expected_output':'上海订单笔数'}]})
    result = await planner.plan_question('统计上海订单笔数', semantic_model_id=130)
    assert len(result.tasks) == 1 and len(calls) == 2
    assert result.tasks[0].structured['指标'] == [{'name':'订单笔数'}]


@pytest.mark.asyncio
async def test_component_titles_keep_year_grain_and_related_display_before_final_ranking(monkeypatch):
    question='查询2025年订单平均金额最高的前五位销售人员及其所在销售公司'
    def extract(request):
        return {'tasks':[{'task_index':task['task_index'],'extraction':{
            '意图':'指标计算' if '不再查询数据库' in task['question'] else '指标查询',
            '指标':[{'name': name} for name in task['metric_targets']], '维度':['业务员'],
            '展示字段':[{'entity':'业务员','field':'销售员姓名'}, {'entity':'销售公司','field':'国药公司名称'}],
            '排序':[{'field':'平均每单销售金额','order':'desc'}] if task['depends_on'] else [],
            '限制':5 if task['depends_on'] else None,
            '时间粒度':{'unit':'年','time_range':'2025年'},
            '过滤条件':[], '输出要求':'按平均每单销售金额降序取前5位并返回所属公司',
            '是否查询数据库':'否' if '不再查询数据库' in task['question'] else '是',
        }} for task in request['tasks']]}
    planner, calls = setup_planner(monkeypatch, {'tasks':[{'question':question + '，交付平均每单销售金额排名',
        'metric_targets':['平均每单销售金额'], 'grain':['业务员']}]}, extract=extract)
    result = await planner.plan_question(question, semantic_model_id=130)
    assert len(calls) == 3 and len(result.tasks) == 3
    for task, metric in zip(result.tasks[:2], ['含税销售总额','订单笔数']):
        assert all(word in task.name for word in ['2025年','业务员',metric,'国药公司名称'])
        assert task.structured['限制'] is None and task.structured['排序'] == []
        assert '不提前' in task.question
        assert f'按{metric}降序' not in task.question
    final = result.tasks[-1]
    assert final.depends_on == (0,1)
    assert final.structured['计算绑定']['numerator']['source_task_index'] == 0
    assert final.structured['计算绑定']['denominator']['source_task_index'] == 1
    assert '前五位' in final.question and '销售公司' in final.name


def test_merged_relative_period_plan_does_not_turn_period_order_into_a_year():
    from app.domain.models import PlannerExtraction
    from app.planning.analysis_task_planner import build_analysis_task_plan
    plan = build_analysis_task_plan('对比本月和上月的销售额', PlannerExtraction(
        intent=PrimaryIntent.COMPARISON_ANALYSIS, structured={
            '意图':'对比分析', '指标':[{'name':'销售额'}], '维度':[],
            '时间粒度':{'time_range':'本月和上月'}, '是否查询数据库':'是'}))
    assert plan is not None and len(plan.tasks) == 3
    assert [task.extraction['时间粒度']['time_range'] for task in plan.tasks[:2]] == ['上月','本月']
    assert plan.tasks[-1].depends_on == ['task-1','task-2']
    assert plan.tasks[-1].extraction['是否查询数据库'] == '否'


@pytest.mark.asyncio
async def test_planned_computation_bindings_reach_existing_router_contract(monkeypatch):
    planner, _ = setup_planner(monkeypatch, {'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]})
    router = CompoundQuestionRouter(planner.settings)
    router._single = planner
    result = await router.plan_with_judgment(CompoundJudgment(is_compound=False, intent='趋势分析'),
        QUESTION, semantic_model_id=130)
    assert result.judgment.intent == '趋势分析'  # preserve question-level display
    assert len(result.plan.tasks) == 3
    binding = result.plan.tasks[-1].extraction['计算绑定']
    assert binding['numerator']['source_task_id'] == 'task-2'
    assert binding['denominator']['source_task_id'] == 'task-1'


@pytest.mark.asyncio
async def test_group_dependency_targets_calculation_and_waits_for_expanded_group(monkeypatch):
    planner, _ = setup_planner(monkeypatch, {'tasks':[{'question':'统计订单级均价','metric_targets':['订单级均价']}]})
    expanded = await planner.plan_question('统计订单级均价', semantic_model_id=130)
    async def plan(question, **kwargs):
        return expanded if question == '统计订单级均价' else QuestionDecomposition(tasks=(
            DecomposedTask(question='根据结果查询医院名单', intent=PrimaryIntent.DETAIL_QUERY),))
    judgment = CompoundJudgment(is_compound=True, entries=(CompoundJudgmentEntry(question='统计订单级均价'),
        CompoundJudgmentEntry(question='根据结果查询医院名单', depends_on=(0,))))
    result = await CompoundTaskPlanner(planner.settings, SimpleNamespace(plan_question=plan)).plan(judgment,
        source_question='统计订单级均价，再根据结果查询医院名单', semantic_model_id=130)
    assert result.tasks[-1].depends_on == ['task-3']


def test_expansion_preserves_multiple_terminal_dependencies_and_source_objects():
    source = [TaskBoundary(question='查询两个复合指标',metric_targets=['A','B']),
              TaskBoundary(question='根据上一步结果查询医院',depends_on=[0])]
    original = copy.deepcopy(source)
    result = complete_boundaries(source,spec='',catalog=(frozenset({'A','B','x','y','z'}),{}),
        dependencies={'A':('x','y'),'B':('x','z')})
    assert result[-1].depends_on == [3,4]
    assert source == original


@pytest.mark.asyncio
async def test_narrative_distribution_is_delivered_with_final_calculation_not_invented_operation(monkeypatch):
    plan = {'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']},
        {'question':'根据前序订单级均价分析分布，不再查询数据库','depends_on':[0],
         'metric_targets':['订单级均价'],'expected_output':'逐订单均价的完整分布分析',
         'postprocess':{'source_task_index':0,'operation':{'type':'aggregate','metrics':['median']}}}]}
    planner, calls = setup_planner(monkeypatch, plan)
    result = await planner.plan_question(QUESTION, semantic_model_id=130)
    assert len(calls) == 3 and len(result.tasks) == 3
    assert '分布分析' in result.tasks[-1].structured['输出要求']
    assert all(not task.structured.get('结果操作') for task in result.tasks)


def test_executable_dependent_filter_is_not_folded_into_narrative():
    source=[TaskBoundary(question='查询订单级均价',metric_targets=['订单级均价']),
        TaskBoundary(question='筛选并描述分布，不再查询数据库',depends_on=[0],metric_targets=['订单级均价'],
            postprocess={'source_task_index':0,'operation':{'type':'limit','count':10}})]
    result=complete_boundaries(source,spec=SPEC,catalog=(frozenset({'订单级均价','含税销售总额','销售总数量'}),{}),
        dependencies={'订单级均价':('含税销售总额','销售总数量')})
    assert len(result)==4 and result[-1].postprocess['operation']['type']=='limit'
    assert result[-1].postprocess['source_task_index']==2


@pytest.mark.asyncio
async def test_local_result_operation_is_planned_before_query_extraction(monkeypatch):
    plan={'tasks':[{'question':'统计各经销商订单笔数'},
        {'question':'根据前序结果按订单笔数降序取前3，不再查询数据库', 'depends_on':[0]}]}
    def extract(request):
        return {'tasks':[{'task_index':0,'extraction':{'意图':'指标查询',
            '指标':[{'name':'订单笔数'}], '维度':['经销商编码'], '是否查询数据库':'是'}},
            {'task_index':1,'extraction':{'意图':'指标计算','是否查询数据库':'否',
             '结果操作':{'source_task_index':0,'operation':{'type':'sort_limit','field':'订单笔数','descending':True,'count':3}}}}]}
    planner,calls=setup_planner(monkeypatch,plan,extract=extract)
    result=await planner.plan_question(plan['tasks'][0]['question'],semantic_model_id=130)
    fixed=json.loads(calls[-1]['messages'][0]['content'].split('已规划任务JSON：\n',1)[1])['tasks']
    assert len(fixed)==len(result.tasks)==2
    assert result.tasks[-1].depends_on==(0,)
    assert result.tasks[-1].structured['结果操作']['source_task_index']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('joint_input',[False,True])
async def test_planning_through_scheduled_computation_uses_order_keys_not_row_positions(monkeypatch,joint_input):
    from uuid import uuid4
    from app.domain.models import AgentResponse, ChatRequest
    from test_complex_execution_correctness import service, persist, IDENTITY
    boundaries=({'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]} if not joint_input else
        {'tasks':[{'question':'按订单汇总含税销售总额和销售总数量','metric_targets':['含税销售总额','销售总数量'],
            'expected_output':'订单号及金额和数量合计','grain':['订单号']},
            {'question':'计算订单级均价并分析分布','metric_targets':['订单级均价'],'depends_on':[0],
             'postprocess':{'source_task_index':0,'operation':{'type':'pipeline','steps':[]}}}]})
    planner, _ = setup_planner(monkeypatch,boundaries)
    router = CompoundQuestionRouter(planner.settings)
    router._single = planner
    plan = (await router.plan_with_judgment(CompoundJudgment(is_compound=False),QUESTION,semantic_model_id=130)).plan
    agent = service()
    agent.task_planner = router
    chat = ChatRequest(semantic_model_id=130,application_id='staged-planner-test',conversation_id='staged-root',
        message_id='staged-message',question=QUESTION)
    chat._dag_root_conversation_id = chat.conversation_id
    async def query(child, identity):
        metrics = [item['name'] for item in child._planner_extraction.structured['指标']]
        if len(metrics)==2:
            rows=[{'订单号':'A','含税销售总额':100,'销售总数量':1},
                  {'订单号':'B','含税销售总额':900,'销售总数量':3}]
        else:
            metric=metrics[0]
            values = [('A',100),('B',900)] if metric=='含税销售总额' else [('B',3),('A',1)]
            rows = [{'订单号':key,metric:value} for key,value in values]
        ref = await persist(agent,child,['订单号',*metrics],rows,conversation=child.conversation_id,
            column_bindings={'订单号':'sales_order.order_no'})
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,status='COMPLETED',
            intent=PrimaryIntent.METRIC_QUERY,answer='合成取数完成',dataset_id=ref.dataset_id)
    agent._handle = query
    response = await agent._handle_task_plan(chat,IDENTITY,plan)
    assert [task.status for task in response.task_results]==['COMPLETED']*len(plan.tasks)
    calculation = next(item.payload for item in response.evidence if item.kind=='ANALYSIS_RESULT' and 'input_binding' in item.payload)
    assert {row['订单号']:row['订单级均价'] for row in calculation['preview']}=={'A':100,'B':300}


@pytest.mark.asyncio
@pytest.mark.parametrize('right_rows', [[{'订单号':'B','数量':2}], [{'订单号':'A','数量':2},{'订单号':'B','数量':3}]])
async def test_one_order_sample_is_not_broadcast_as_a_global_scalar(right_rows):
    from app.domain.models import AtomicTask
    from test_complex_execution_correctness import service, context, persist, IDENTITY
    agent=service();chat,_=context()
    await persist(agent,chat,['订单号','金额'],[{'订单号':'A','金额':100}],conversation='a',
        column_bindings={'订单号':'sales_order.order_no'})
    await persist(agent,chat,['订单号','数量'],right_rows,conversation='b',
        column_bindings={'订单号':'sales_order.order_no'})
    task=AtomicTask(task_id='c',question='按订单计算均价',depends_on=['a','b'],extraction={'计算绑定':{
        'operation':'divide','numerator':{'source_task_id':'a','column':'金额'},
        'denominator':{'source_task_id':'b','column':'数量'},'result_field':'均价'}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'a':'a','b':'b'})
    assert result.status=='FAILED' and result.dataset_id is None


@pytest.mark.asyncio
async def test_registered_sample_scope_reaches_each_component_before_parameter_extraction(monkeypatch):
    planner,calls=setup_planner(monkeypatch,{'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]})
    scoped=SPEC.replace('先按订单号汇总，含税销售总额 ÷ 销售总数量',
        '先按订单号汇总，含税销售总额 ÷ 销售总数量。销售总数量大于0')
    semantic_description.load_semantic_description.return_value['content']=scoped
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    fixed=json.loads(calls[-1]['messages'][0]['content'].split('已规划任务JSON：\n',1)[1])['tasks']
    assert all('销售总数量大于0' in item['sample_requirements'][0] for item in fixed[:2])
    assert result.boundary_plan_complete


@pytest.mark.asyncio
async def test_clarification_keeps_tentative_operation_out_of_executable_plan(monkeypatch):
    plan={'tasks':[{'question':'计算订单笔数的平均值','metric_targets':['订单笔数'],
        'clarification':{'question':'采用哪个分组口径？','candidates':['按年','按月']},
        'postprocess':{'operation':{'type':'aggregate','group_by':[],
            'aggregations':[{'field':'订单笔数','function':'avg','alias':'均值'}]}}}]}
    def extract(request):
        return {'tasks':[{'task_index':0, 'extraction':{'意图':'指标计算',
            '计算待确认':{'question':'采用哪个分组口径？','candidates':['按年','按月']}}}]}
    planner,calls=setup_planner(monkeypatch,plan,extract=extract)
    result=await planner.plan_question('计算订单笔数的平均值',semantic_model_id=130)
    assert len(result.tasks)==1 and len(calls)==2
    assert result.tasks[0].structured['计算待确认']['candidates']==['按年','按月']
    assert '结果操作' not in result.tasks[0].structured


@pytest.mark.asyncio
async def test_component_queries_do_not_take_top_n_before_final_ratio(monkeypatch):
    question='查询2025年平均每单销售金额最高的前3位销售员'
    def extract(request):
        return {'tasks':[{'task_index':item['task_index'],'extraction':{
            '意图':'指标计算' if item['depends_on'] else '指标查询',
            '指标':[{'name':name} for name in item['metric_targets']],
            '排序':[{'field':item['metric_targets'][0],'order':'desc'}] if item['depends_on'] else [],
            '限制':3 if item['depends_on'] else None,
            '是否查询数据库':'否' if '不再查询数据库' in item['question'] else '是'}}
            for item in request['tasks']]}
    planner,_=setup_planner(monkeypatch,{'tasks':[{'question':question,
        'metric_targets':['平均每单销售金额'],'grain':['业务员编码']}]},extract=extract)
    result=await planner.plan_question(question,semantic_model_id=130)
    assert all(not task.structured['排序'] and task.structured['限制'] is None for task in result.tasks[:2])
    assert result.tasks[-1].structured['限制']==3


@pytest.mark.asyncio
async def test_component_names_cannot_replace_a_composite_delivery_goal(monkeypatch):
    question='统计2025年上海市平均每单销售金额'
    plan={'tasks':[{'question':question,'metric_targets':['含税销售总额','订单笔数'],
        'expected_output':'2025年上海市平均每单销售金额数值','grain':['上海市']}]}
    planner,calls=setup_planner(monkeypatch,plan)
    semantic_description.load_semantic_description.return_value['content'] += (
        '\n| 实体 | 明细查询输出属性 |\n| --- | --- |\n| 经销商 | 经销商名称, 经销商编码 |\n'
        '\n| 维度 | 说明 |\n| --- | --- |\n| 城市 | 医院所在地 |')
    result=await planner.plan_question(question,semantic_model_id=130)
    assert len(result.tasks)==3 and len(calls)==3
    assert result.tasks[-1].structured['指标']==[{'name':'平均每单销售金额'}]
    assert result.tasks[-1].depends_on==(0,1)
    assert all('上海市' not in task.structured['维度'] for task in result.tasks)


def test_a_component_query_can_mention_its_later_composite_purpose_without_expansion():
    task=TaskBoundary(question='查询含税销售总额，用于计算平均每单销售金额',
        metric_targets=['含税销售总额'],expected_output='含税销售总额，供后续计算平均每单销售金额')
    result=complete_boundaries([task],spec=SPEC,
        catalog=(frozenset({'平均每单销售金额','含税销售总额','订单笔数'}),{}),
        dependencies={'平均每单销售金额':('含税销售总额','订单笔数')})
    assert len(result)==1 and result[0].metric_targets==['含税销售总额']


@pytest.mark.asyncio
async def test_extracted_sample_predicate_is_left_for_authorized_asl_binding(monkeypatch):
    def extract(request):
        return {'tasks':[{'task_index':item['task_index'],'extraction':{'意图':'明细查询',
            '过滤条件':[{'field':'销量','op':'>','value':[0]}]}} for item in request['tasks']]}
    planner,_=setup_planner(monkeypatch,{'tasks':[{'question':QUESTION,'metric_targets':['订单级均价']}]},extract=extract)
    semantic_description.load_semantic_description.return_value['content'] = SPEC.replace(
        '先按订单号汇总，含税销售总额 ÷ 销售总数量','先按订单号汇总，含税销售总额 ÷ 销售总数量。销量大于0')
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert all(task.structured['过滤条件'][0]['field']=='销量' for task in result.tasks[:2])


@pytest.mark.asyncio
async def test_joint_component_input_is_followed_by_local_calculation_without_a_magic_phrase(monkeypatch):
    plan={'tasks':[{'question':'按订单汇总含税销售总额和销售总数量，用于计算订单级均价',
        'metric_targets':['含税销售总额','销售总数量'],'grain':['销售订单'],
        'expected_output':'订单号及金额和数量合计'},
        {'question':'根据前序汇总计算订单级均价并分析分布','depends_on':[0],
         'metric_targets':['订单级均价'],'expected_output':'订单级均价及价格分布',
         'postprocess':{'source_task_index':0,'operation':{'type':'pipeline','steps':[
             {'op':'derive','expression':'amount / quantity'}]}}}]}
    planner,calls=setup_planner(monkeypatch,plan)
    result=await planner.plan_question(QUESTION,semantic_model_id=130)
    assert len(result.tasks)==2 and len(calls)==3
    assert result.tasks[0].intent==PrimaryIntent.METRIC_QUERY
    assert '订单号' in result.tasks[0].structured['维度']
    assert result.tasks[-1].depends_on==(0,) and result.tasks[-1].structured['是否查询数据库']=='否'
    binding=result.tasks[-1].structured['计算绑定']
    assert binding['numerator']['source_task_index']==binding['denominator']['source_task_index']==0
    assert '结果操作' not in result.tasks[-1].structured


@pytest.mark.parametrize('intent', ['指标查询','统计查询','趋势分析','明细查询','解释口径'])
def test_existing_chinese_intent_values_are_normalized_without_an_extra_model(intent):
    assert SingleQuestionPlanner._sanitize_intent(intent) is not None


@pytest.mark.asyncio
async def test_related_detail_lookup_needs_database_when_parent_only_has_ranking(monkeypatch):
    plan={'tasks':[{'question':'查询订单笔数最多的医院','metric_targets':['订单笔数']},
        {'question':'查询前序任务入选医院销售过哪些商品','depends_on':[0],
         'metric_targets':[],'expected_output':'商品编码和商品名称去重名单'}]}
    def extract(request):
        return {'tasks':[{'task_index':task['task_index'],'extraction':{'意图':'明细查询',
            '指标':[{'name':'订单笔数'}] if task['task_index']==0 else [],
            '是否查询数据库':'是'}} for task in request['tasks']]}
    planner,calls=setup_planner(monkeypatch,plan,extract=extract)
    result=await planner.plan_question('先查询订单最多的医院，再查该医院卖过的商品',semantic_model_id=130)
    assert len(result.tasks)==2 and len(calls)==2
    child=result.tasks[-1]
    assert child.depends_on==(0,) and child.intent==PrimaryIntent.DETAIL_QUERY
    assert child.structured['指标']==[] and child.structured['是否查询数据库']=='是'
    assert '不再查询数据库' not in child.question


@pytest.mark.parametrize('shape',['SINGLE','COMPOSITE'])
def test_staged_planning_keeps_seven_visible_nodes_and_precedes_asl_sql(shape):
    import re
    from uuid import uuid4
    from app.domain.models import AgentResponse
    from app.services.progress import emit_progress
    from test_api import TestClient, build_test_app
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):
            for stage,message in [('INTENT_RECOGNITION','问题级意图'),('TASK_PLANNING','完整任务及依赖'),
                ('ASL_GENERATION','任务绑定ASL'),('SQL_EXECUTION','执行SQL'),
                ('RELIABILITY_CHECK','检查结果'),('INSIGHT_ANALYSIS','分析最终交付物'),
                ('FINAL_OUTPUT','准备最终回答')]:
                await emit_progress(stage,'COMPLETED',message)
            return {'response':AgentResponse(request_id=uuid4(),conversation_id=state['chat'].conversation_id,
                status='COMPLETED',execution_shape=shape,intent=PrimaryIntent.METRIC_QUERY,answer='合成业务回答')}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'staged-nodes-'+shape,'message_id':'m1','question':'查询并分析业务指标'})
    assert response.status_code==200
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    assert [name.strip() for name in re.findall(r'#### ◉ ([^\r\n]+)',text)]==[
        '意图识别','任务拆分与规划','解析校验','调度执行','结果校验','数据洞察分析','最终输出']
    assert text.index('完整任务及依赖') < text.index('任务绑定ASL') < text.index('执行SQL')
