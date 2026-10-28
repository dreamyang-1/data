"""Two-stage catalog integration. LLM/SQL fixtures are explicitly synthetic."""
import copy
import json
from datetime import date,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.analysis import AnalysisEngine
from app.analysis.tools.registry import (CHAINS, TOOLS, recognition_directory,
    planning_contract, intent_for_chain, CATALOG_VERSION)
from app.analysis.tools.plans import AnalysisToolPlan, PLAN_KEY
from app.analysis.tools.compiler import compile_decomposition
from app.analysis.tools.execution import capability_errors
from app.config import Settings
from app.domain import semantic_description
from app.domain.models import PrimaryIntent as I
from app.planning.compound_question import (CompoundQuestionJudge, CompoundJudgment,
    CompoundJudgmentEntry, _validate_model_routes)
from app.planning.task_dag import SingleQuestionPlanner, TaskPlanningError, TaskInputBindingError
from app.services.progress import progress_scope
from test_task_planning_stages import SPEC
from test_complex_execution_correctness import service, context, IDENTITY

METRIC='含税销售总额'
CASES=[
 ('T1','trend','分析2025年10月至12月含税销售总额的月度趋势，并展示折线图。','month','2025-10-01','2026-01-01',['业务日期']),
 ('C1','comparison','对2025年11月与12月各渠道业态的含税销售总额进行对比分析，按增减额降序展示。','month','2025-11-01','2026-01-01',['渠道业态']),
 ('S1','composition','对2025年第四季度各渠道业态的含税销售总额进行占比分析，按占比降序展示。',None,'2025-10-01','2026-01-01',['渠道业态']),
 ('A6','anomaly','检测2025年10月1日至12月30日每日含税销售总额是否存在异常波动，列出异常日期及判定依据，并说明缺少记录日期如何处理。','day','2025-10-01','2025-12-31',['业务日期']),
 ('R1','attribution','对2025年12月相对于11月的含税销售总额变化做归因分析，按渠道业态拆解各渠道的增减贡献。','month','2025-11-01','2026-01-01',['渠道业态']),
 ('F1','forecast','基于2025年7月至12月的月度含税销售总额进行预测分析，预测下一月销售额，并说明回测依据和适用范围。','month','2025-07-01','2026-01-01',['业务日期']),
 ('Q2','trend','按季度统计2025年销售总金额并进行分析','quarter','2025-01-01','2026-01-01',['业务日期']),
]


def proposal(case, **updates):
    code,chain,question,grain,start,end,dimensions=case
    value={'chain':chain,'coverage_mode':'full_profile','capabilities':[],
        'grain':grain,'target_window':{'start':start,'end_exclusive':end},'user_evidence':question}
    if chain in {'comparison','attribution'}:
        value['comparison_kind']='periods'
        value['comparison_periods']=[{'role':'BASE','start':'2025-11-01','end_exclusive':'2025-12-01'},
            {'role':'CURRENT','start':'2025-12-01','end_exclusive':'2026-01-01'}]
    return {**value,**updates}


def parameters(case):
    code,chain,question,grain,start,end,dimensions=case
    return {'意图':CHAINS[chain][0],'实体':['销售订单'],'指标':[{'name':METRIC}],
        '维度':dimensions,'过滤条件':[],'排序':[],'是否查询数据库':'是',
        '时间粒度':{'unit':grain,'time_range':f'{start}至{date.fromisoformat(end)-timedelta(days=1)}','anchor':'业务日期'}}


async def planned_case(monkeypatch,case,tool_plan=None):
    monkeypatch.setattr(semantic_description,'load_semantic_description',AsyncMock(return_value={
        'source':'semantic_model_fixture','content':SPEC}))
    seen=[]
    async def respond(request):
        body=json.loads(request.content);seen.append(body)
        instruction=body['messages'][0]['content']
        assert '本轮分析工具目录' in instruction
        assert '"chain_id":"'+case[1]+'"' in instruction
        if '你是任务规划器' in instruction:
            result={'analysis_plan':tool_plan or proposal(case), 'tasks':[{'question':case[2],
                'expected_output':METRIC+'按指定粒度与分组的完整数据'}]}
        else:
            fixed=json.loads(instruction.split('已规划任务JSON：\n',1)[1])
            assert fixed['analysis_plan']==AnalysisToolPlan.model_validate(tool_plan or proposal(case)).model_dump(mode='json')
            result={'tasks':[{'task_index':i,'extraction':parameters(case)} for i,_ in enumerate(fixed['tasks'])]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
    settings=Settings(_env_file=None,env='test',intent_model_api_key='fixture',intent_model_enabled=True,multi_question_model_enabled=True)
    planner=SingleQuestionPlanner(settings,transport=httpx.MockTransport(respond))
    decomposition=await planner.plan_question(case[2],semantic_model_id=81,
        root_intent=intent_for_chain(case[1]),analysis_chain=case[1])
    assert len(seen)==2
    return compile_decomposition(case[2],decomposition),seen


def test_recognition_only_receives_six_chains_and_names():
    rows=recognition_directory()
    assert len(rows)==6 and sum(len(row['tools']) for row in rows)==30
    assert all(set(row)=={'chain_id','name','tools'} for row in rows)
    assert all(set(tool)=={'id','name'} for row in rows for tool in row['tools'])
    assert all(key not in json.dumps(rows) for key in ['required_data','parameter_schema','preconditions'])


@pytest.mark.parametrize('chain',list(CHAINS))
def test_planning_only_sees_selected_chain_complete_contract(chain):
    contract=planning_contract(chain)
    assert {tool['capability_id'] for tool in contract['tools']}=={tool['capability_id'] for tool in CHAINS[chain][2]}
    assert all(tool['required_data'] and tool['output_fields'] and 'parameter_schema' in tool for tool in contract['tools'])
    Draft202012Validator.check_schema({'$defs':contract['$defs']})


@pytest.mark.parametrize('change',[
    {'capabilities':['invented_tool']}, {'capabilities':['MAD']},
    {'capabilities':[],'coverage_mode':'explicit'},
    {'parameters':{'continuous_runs':{'min_changes':1}}},
    {'parameters':{'continuous_runs':{'python':'eval()'}}},
    {'target_window':{'start':'2026-01-01','end_exclusive':'2025-01-01'}},
])
def test_bad_tool_proposals_fail_before_queries(change):
    with pytest.raises((ValueError,ValidationError)):
        AnalysisToolPlan.model_validate(proposal(CASES[0],**change))


def test_comparison_cannot_omit_periods_even_when_grain_and_target_are_null():
    with pytest.raises(ValueError,match='COMPARISON_KIND'):
        AnalysisToolPlan.model_validate({'chain':'comparison','coverage_mode':'full_profile','user_evidence':'11月和12月'})


def test_upstream_multiple_routes_are_authoritative_without_keyword_override():
    result=_validate_model_routes(CompoundJudgment(is_compound=True,entries=(
        CompoundJudgmentEntry(question='走势',intent='趋势分析'),
        CompoundJudgmentEntry(question='哪些项目拖累',intent='归因分析'))),['trend','attribution'])
    assert result.analysis_routes==('trend','attribution')
    # 单链路冲突：判断模型按字面把意图标成另一条链路时，改回已确认链路的标签
    relabeled=_validate_model_routes(CompoundJudgment(is_compound=False,intent='对比分析'),['composition'])
    assert relabeled.intent=='占比分析'
    assert relabeled.analysis_routes==('composition',)
    mixed=_validate_model_routes(CompoundJudgment(is_compound=True,entries=(
        CompoundJudgmentEntry(question='取数',intent='指标查询'),
        CompoundJudgmentEntry(question='份额变化',intent='对比分析'))),['composition'])
    assert [entry.intent for entry in mixed.entries]==['指标查询','占比分析']
    # 观察集是确认路由的非空子集：判断模型按更少链路组织任务，放行且不改标签
    subset=_validate_model_routes(CompoundJudgment(is_compound=False,intent='趋势分析'),['trend','comparison'])
    assert subset.intent=='趋势分析'
    assert subset.analysis_routes==('trend','comparison')
    # 补全模型把普通排名/极值查询误路由进分析链路：按判断模型的普通条目放行
    plain=_validate_model_routes(CompoundJudgment(is_compound=False,intent='指标查询'),['comparison'])
    assert plain.intent=='指标查询'
    assert plain.analysis_routes==('comparison',)
    plain_multi=_validate_model_routes(CompoundJudgment(is_compound=False,intent='指标查询'),['trend','comparison'])
    assert plain_multi.analysis_routes==('trend','comparison')
    # 越权引入未确认链路仍按合同失败
    with pytest.raises(TaskPlanningError):
        _validate_model_routes(CompoundJudgment(is_compound=False,intent='趋势分析'),['anomaly','forecast'])


@pytest.mark.asyncio
@pytest.mark.parametrize('case',CASES,ids=[case[0] for case in CASES])
async def test_user_questions_plan_with_model_contract_not_keyword_selector(monkeypatch,case):
    import app.analysis.tool_selection as legacy
    monkeypatch.setattr(legacy,'select_tools',lambda *args: (_ for _ in ()).throw(AssertionError('keyword selector called')))
    plan,_=await planned_case(monkeypatch,case)
    assert plan is not None
    binding=plan.tasks[-1].extraction['分析绑定'];requirements=binding['requirements']
    assert requirements['tool_selection']['source']=='MODEL_CATALOG'
    assert plan.tasks[-1].primary_intent==intent_for_chain(case[1])
    assert requirements['target_window']=={'start':case[4],'end_exclusive':case[5]}
    if case[0]=='T1':
        assert [task.extraction['时间粒度']['time_range'] for task in plan.tasks[:-1]]==[
            '2025-09-01至2025-12-31','2024-10-01至2024-12-31']
    if case[0]=='C1':
        assert {p['role'] for p in binding['periods']}=={'基期','当前期'}
        assert [t.extraction['时间粒度']['time_range'] for t in plan.tasks[:-1]]==[
            '2025-11-01至2025-11-30','2025-12-01至2025-12-31']
        assert all('2025年2025-' not in t.question for t in plan.tasks)
    if case[0]=='R1':
        assert len(plan.tasks)==3
        assert all(task.extraction['时间粒度']['time_range']=='2025-11-01至2025-12-31' for task in plan.tasks[:-1])


def refs_for(case,plan):
    code=case[0]
    if code=='C1':
        return {task.task_id:(SimpleNamespace(columns=['渠道业态（编码）','渠道业态',METRIC]),[
            {'渠道业态（编码）':'A','渠道业态':'甲',METRIC:80 if i==0 else 60},
            {'渠道业态（编码）':'B','渠道业态':'乙',METRIC:20 if i==0 else 84}])
            for i,task in enumerate(plan.tasks[:-1])}
    if code=='S1':return {'task-1':(SimpleNamespace(columns=['渠道业态',METRIC]),[
        {'渠道业态':'甲',METRIC:80},{'渠道业态':'乙',METRIC:20}])}
    if code=='R1':
        return {'task-1':(SimpleNamespace(columns=['月份',METRIC]),[{'月份':'2025-11',METRIC:100},{'月份':'2025-12',METRIC:144}]),
            'task-2':(SimpleNamespace(columns=['月份','渠道业态',METRIC]),[
                {'月份':m,'渠道业态':name,METRIC:value} for m,name,value in [
                    ('2025-11','甲',80),('2025-12','甲',60),('2025-11','乙',20),('2025-12','乙',84)]])}
    if code=='A6':
        rows=[{'日期':(date(2025,10,1)+timedelta(days=i)).isoformat(),METRIC:1000 if i==40 else 100+i%5} for i in range(91)]
        return {'task-1':(SimpleNamespace(columns=['日期',METRIC]),rows)}
    if code=='Q2':
        return {'task-1':(SimpleNamespace(columns=['季度',METRIC]),[
            {'季度':label,METRIC:value} for label,value in [('2024-Q4',80),('2025-Q1',100),('2025-Q2',120),('2025-Q3',110),('2025-Q4',140)]])}
    values=[(f'2025-{i:02}',i*10) for i in range(9 if code=='T1' else 7,13)]
    refs={'task-1':(SimpleNamespace(columns=['月份',METRIC]),[{'月份':label,METRIC:value} for label,value in values])}
    if code=='T1':refs['task-2']=(SimpleNamespace(columns=['月份',METRIC]),[{'月份':f'2024-{i:02}',METRIC:50} for i in range(10,13)])
    return refs


@pytest.mark.asyncio
@pytest.mark.parametrize('case',CASES,ids=[case[0] for case in CASES])
async def test_user_analysis_questions_execute_real_algorithms_with_synthetic_inputs(monkeypatch,case):
    plan,_=await planned_case(monkeypatch,case)
    agent=service();agent.analysis_engine=AnalysisEngine()
    chat,_=context();chat.question=case[2]
    task=plan.tasks[-1];binding=task.extraction['分析绑定']
    monkeypatch.setattr(agent,'_load_task_result_references',AsyncMock(return_value=refs_for(case,plan)))
    monkeypatch.setattr(agent,'_save_analysis_derived_dataset',AsyncMock(return_value=SimpleNamespace(dataset_id='synthetic-derived')))
    events=[]
    with progress_scope(events.append):
        response=await agent._respond_analysis_task(chat,IDENTITY,task,{}, {},binding)
    assert response.status=='COMPLETED',response.answer
    facts=response.evidence[0].payload['facts']
    assert facts['tool_selection_source']=='MODEL_CATALOG'
    assert facts['tool_executions']
    assert not capability_errors(task.primary_intent,binding['requirements']['tool_selection'],facts)
    if case[0]=='T1':
        assert facts['result_scope']['labels']==['2025-10','2025-11','2025-12']
        assert len(facts['period_over_period']['changes'])==3
    if case[0]=='R1':assert facts['contribution_sum']==44 and facts['residual']==0
    if case[0]=='F1':
        assert set(facts['candidate_scores'])=={'naive_last','recent_mean_3','linear_drift'}
        assert facts['history_points']==6 and facts['forecast'] is not None
    if case[0]=='A6':assert {'MAD','IQR','Z_SCORE','ADJACENT_JUMP'}<={d['name'] for d in facts['detectors']}


@pytest.mark.asyncio
async def test_implicit_independent_mom_has_no_keyword_gate_or_extra_tools(monkeypatch):
    case=('MOM','trend','这半年上海市每个月比前一个月多卖了多少，只回答这件事。','month','2025-07-01','2026-01-01',['业务日期'])
    selected=proposal(case,coverage_mode='explicit',capabilities=['period_over_period_change'])
    plan,_=await planned_case(monkeypatch,case,selected)
    assert len(plan.tasks)==2
    assert plan.tasks[-1].extraction['分析绑定']['requirements']['tool_selection']['capabilities']==['period_over_period_change']
    assert plan.tasks[0].extraction['时间粒度']['time_range']=='2025-06-01至2025-12-31'


@pytest.mark.asyncio
async def test_no_extra_fetch_preserves_first_period_as_not_computable(monkeypatch):
    case=CASES[0]
    plan,_=await planned_case(monkeypatch,case,proposal(case,coverage_mode='explicit',capabilities=['period_over_period_change'],auxiliary_fetch_allowed=False))
    assert plan.tasks[0].extraction['时间粒度']['time_range']=='2025-10-01至2025-12-31'


@pytest.mark.asyncio
async def test_average_per_order_preserves_registered_formula_and_uses_real_calculator():
    from app.domain.models import AtomicTask
    from app.services.calculation_execution import calculate
    from test_complex_execution_correctness import persist
    agent=service();chat,_=context()
    rows=[{METRIC:1000,'订单笔数':4}]
    ref=await persist(agent,chat,[METRIC,'订单笔数'],rows)
    task=AtomicTask(task_id='task-2',question='2025 年上海市平均每单销售金额是多少？',
        primary_intent=I.METRIC_QUERY,depends_on=['task-1'],extraction={'计算绑定':{
            'operation':'divide','left':{'column':METRIC,'source_task_id':'task-1'},
            'right':{'column':'订单笔数','source_task_id':'task-1'},'result_field':'平均每单销售金额','format':'number'}})
    result=calculate(task,{'task-1':(ref,rows)},lambda *a,**kw:None)
    assert len(result[1])==1 and result[1][0]['平均每单销售金额']==250


def test_incomplete_target_samples_are_not_claimed_as_completed():
    from app.domain.models import CanonicalAnalysisRequest,MetricRef,KnowledgeContext
    from app.analysis.tools.requirements import build_model_requirements
    case=CASES[0]
    p=AnalysisToolPlan.model_validate(proposal(case,coverage_mode='explicit',capabilities=['turning_points','volatility']))
    request=CanonicalAnalysisRequest(conversation_id='synthetic',tenant_id='t',user_id='u',application_id='a',
        primary_intent=I.TREND_ANALYSIS,original_question='只识别转折和波动',metrics=[MetricRef(input=METRIC)],semantic_model_id=81)
    request._analysis_contract=build_model_requirements(p,parameters(case),today=date(2026,10,26))
    output=AnalysisEngine().analyze(request,['月份',METRIC],[{'月份':'2025-10',METRIC:100}],KnowledgeContext(query='synthetic'))
    assert all(record['status']=='NOT_COMPUTABLE' for record in output.facts['tool_executions'])


@pytest.mark.asyncio
async def test_object_comparison_has_explicit_roles_and_separate_filters(monkeypatch):
    case=CASES[1]
    value=proposal(case,comparison_kind='objects',comparison_periods=[],comparison_objects=[
        {'role':'BASE','entity':'销售订单','field':'关联城市','value':'上海市'},
        {'role':'CURRENT','entity':'销售订单','field':'关联城市','value':'北京市'}])
    plan,_=await planned_case(monkeypatch,case,value)
    assert len(plan.tasks)==3
    assert [t.extraction['过滤条件'][-1]['value'] for t in plan.tasks[:-1]]==['上海市','北京市']
    assert [p['role'] for p in plan.tasks[-1].extraction['分析绑定']['periods']]==['基期','当前期']


def test_object_comparison_replaces_stale_filters_bound_via_alias_entities():
    """种子提取里同一字段以主实体与枚举实体双别名出现时，对象过滤按字段名整体替换。

    复现背景：判断模型输出 销售订单.业态名称=['直销'] 与 业态.业态名称=直销 两条
    谓词；旧逻辑只按 (entity,field) 精确匹配清理，CURRENT 对象任务残留
    销售订单.业态名称=直销，与新增的 业态.业态名称=分销 绑定到同一物理列，
    生成 business_type_code=3 AND =5 的恒假筛选。
    """
    from app.planning.task_dag import QuestionDecomposition,DecomposedTask
    case=CASES[1]
    value=proposal(case,comparison_kind='objects',comparison_periods=[],comparison_objects=[
        {'role':'BASE','entity':'业态','field':'业态名称','value':'直销'},
        {'role':'CURRENT','entity':'业态','field':'业态名称','value':'分销'}])
    structured={**parameters(case),'过滤条件':[
        {'entity':'销售订单','field':'业态名称','op':'=','value':['直销'],'stage':'where'},
        {'entity':'业态','field':'业态名称','op':'=','value':'直销'}],
        PLAN_KEY:value}
    decomposition=QuestionDecomposition(intent=I.COMPARISON_ANALYSIS,tasks=(
        DecomposedTask(question=case[2],intent=I.METRIC_QUERY,structured=structured,depends_on=()),))
    plan=compile_decomposition(case[2],decomposition)
    assert len(plan.tasks)==3
    assert [task.extraction['过滤条件'] for task in plan.tasks[:-1]]==[
        [{'entity':'业态','field':'业态名称','op':'=','value':'直销'}],
        [{'entity':'业态','field':'业态名称','op':'=','value':'分销'}]]


def test_object_comparison_replaces_unrecognized_same_field_variants():
    """同字段未识别的等值写法（如 op=equals）也由对象值覆盖，只保留显式不等条件。"""
    from app.planning.task_dag import QuestionDecomposition,DecomposedTask
    case=CASES[1]
    value=proposal(case,comparison_kind='objects',comparison_periods=[],comparison_objects=[
        {'role':'BASE','entity':'业态','field':'业态名称','value':'直销'},
        {'role':'CURRENT','entity':'业态','field':'业态名称','value':'分销'}])
    structured={**parameters(case),'过滤条件':[
        {'entity':'销售订单','field':'业态名称','op':'equals','value':'直销'},
        {'entity':'业态','field':'业态名称','op':'!=','value':'零售'}],
        PLAN_KEY:value}
    decomposition=QuestionDecomposition(intent=I.COMPARISON_ANALYSIS,tasks=(
        DecomposedTask(question=case[2],intent=I.METRIC_QUERY,structured=structured,depends_on=()),))
    plan=compile_decomposition(case[2],decomposition)
    assert [task.extraction['过滤条件'] for task in plan.tasks[:-1]]==[
        [{'entity':'业态','field':'业态名称','op':'!=','value':'零售'},
            {'entity':'业态','field':'业态名称','op':'=','value':'直销'}],
        [{'entity':'业态','field':'业态名称','op':'!=','value':'零售'},
            {'entity':'业态','field':'业态名称','op':'=','value':'分销'}]]


def test_object_comparison_keeps_two_row_object_route_not_grouped_period_contract():
    """对象间对比不能带 comparison_dimensions，否则会误入分组两期对比并索要稳定编码。

    复现背景：对比上海与江苏的订单笔数时，规划把“省份”留在维度里；对象产物
    契约若继续登记 comparison_dimensions，引擎会按分组两期对比对齐，而按对象
    角色合并出的两行没有“省份（编码）”列，直接报“需要每个维度的稳定编码”。
    """
    from app.domain.models import CanonicalAnalysisRequest,MetricRef,KnowledgeContext
    from app.planning.task_dag import QuestionDecomposition,DecomposedTask
    case=CASES[1]
    value=proposal(case,comparison_kind='objects',comparison_periods=[],comparison_objects=[
        {'role':'BASE','entity':'省份','field':'省份名称','value':'上海'},
        {'role':'CURRENT','entity':'省份','field':'省份名称','value':'江苏'}])
    structured={**parameters(case),'维度':['省份'],PLAN_KEY:value}
    decomposition=QuestionDecomposition(intent=I.COMPARISON_ANALYSIS,tasks=(
        DecomposedTask(question=case[2],intent=I.METRIC_QUERY,structured=structured,depends_on=()),))
    plan=compile_decomposition(case[2],decomposition)
    requirements=plan.tasks[-1].extraction['分析绑定']['requirements']
    assert 'comparison_dimensions' not in requirements
    assert requirements['comparison_periods']==[]
    request=CanonicalAnalysisRequest(conversation_id='synthetic',tenant_id='t',user_id='u',application_id='a',
        primary_intent=I.COMPARISON_ANALYSIS,original_question=case[2],
        metrics=[MetricRef(input=METRIC,canonical_name=METRIC)],dimensions=['省份'],semantic_model_id=81)
    request._analysis_contract=requirements
    output=AnalysisEngine().analyze(request,['期间','省份','省份名称',METRIC,'period_role'],[
        {'期间':'上海市','省份':'310000','省份名称':'上海市',METRIC:35201,'period_role':'基期'},
        {'期间':'江苏省','省份':'320000','省份名称':'江苏省',METRIC:3762,'period_role':'当前期'}],
        KnowledgeContext(query='synthetic'))
    assert output.method=='two_group_comparison'
    assert (output.facts['base_label'],output.facts['current_label'])==('上海市','江苏省')
    assert (output.facts['base'],output.facts['current'])==(35201,3762)
    assert output.facts['absolute_change']==-31439


def test_object_comparison_plan_is_rebuilt_by_the_catalog_compiler_not_the_period_playbook():
    """对象计划无论从哪个规划入口进来，都展开为两次对象取数 + 按对象角色对比。

    复现背景：根意图与链路不一致或 compile_decomposition 未接管时，计划会
    改由 build_analysis_task_plan 展开；对象计划的 comparison_periods 是空列表，
    旧逻辑仍会登记 comparison_dimensions 与空 periods，只发一次不带对象过滤的
    取数，执行期报“需要每个维度的稳定编码”。
    """
    from app.domain.models import PlannerExtraction
    from app.planning.analysis_task_planner import build_analysis_task_plan, normalize_analysis_task_plan
    from test_analysis_delivery_regressions import TODAY
    case=CASES[1]
    value=proposal(case,comparison_kind='objects',comparison_periods=[],comparison_objects=[
        {'role':'BASE','entity':'省份','field':'省份名称','value':'上海'},
        {'role':'CURRENT','entity':'省份','field':'省份名称','value':'江苏'}])
    structured={**parameters(case),'维度':['省份'],PLAN_KEY:value}
    built=build_analysis_task_plan(case[2],PlannerExtraction(intent=I.COMPARISON_ANALYSIS,
        parameters=[],structured=copy.deepcopy(structured)),today=TODAY)
    assert [task.task_id for task in built.tasks]==['task-1','task-2','task-3']
    assert [task.extraction['过滤条件'][-1]['value'] for task in built.tasks[:-1]]==['上海','江苏']
    binding=built.tasks[-1].extraction['分析绑定']
    assert 'comparison_dimensions' not in binding['requirements']
    assert [item['role'] for item in binding['periods']]==['基期','当前期']
    # 根意图与链路不一致时走重建路径，产物仍是同一份对象对比契约
    rebuilt=normalize_analysis_task_plan(case[2],built,I.METRIC_QUERY)
    assert [task.extraction.get('过滤条件') for task in rebuilt.tasks[:-1]]==[
        task.extraction['过滤条件'] for task in built.tasks[:-1]]
    assert 'comparison_dimensions' not in rebuilt.tasks[-1].extraction['分析绑定']['requirements']


def test_undeclared_period_contract_never_enters_grouped_two_period_alignment():
    """只带分组维度、未声明基期/本期的契约不能进入分组两期对齐（旧检查点兼容）。"""
    from app.domain.models import CanonicalAnalysisRequest,MetricRef,KnowledgeContext
    request=CanonicalAnalysisRequest(conversation_id='synthetic',tenant_id='t',user_id='u',application_id='a',
        primary_intent=I.COMPARISON_ANALYSIS,original_question='对比上海与江苏的订单笔数',
        metrics=[MetricRef(input=METRIC,canonical_name=METRIC)],dimensions=['省份'],semantic_model_id=81)
    request._analysis_contract={'comparison_dimensions':['省份'],'tool_selection':{
        'mode':'SELECTED','capabilities':[],'source':'MODEL_CATALOG','parameters':{}}}
    output=AnalysisEngine()._comparison(request,['期间','省份名称',METRIC,'period_role'],[
        {'期间':'上海市','省份名称':'上海市',METRIC:35201,'period_role':'基期'},
        {'期间':'江苏省','省份名称':'江苏省',METRIC:3762,'period_role':'当前期'}],KnowledgeContext(query='synthetic'))
    assert output.method=='two_group_comparison'
    assert (output.facts['base'],output.facts['current'])==(35201,3762)


def test_output_contract_rejects_unregistered_capability_and_nonfinite_results():
    from app.analysis.tools.results import ToolExecutionRecord
    from app.analysis.tools.execution import validate_finite_results
    with pytest.raises(ValueError):validate_finite_results({'amount':float('inf')})
    with pytest.raises(ValidationError):ToolExecutionRecord.model_validate({'capability_id':'invented'})


@pytest.mark.asyncio
@pytest.mark.parametrize('case',CASES,ids=[case[0] for case in CASES])
async def test_recompiling_a_saved_model_plan_preserves_scope_and_grouping(monkeypatch,case):
    from app.planning.task_dag import QuestionDecomposition,DecomposedTask
    first,_=await planned_case(monkeypatch,case)
    ids={task.task_id:index for index,task in enumerate(first.tasks)}
    resolved=QuestionDecomposition(intent=first.tasks[-1].primary_intent,tasks=tuple(
        DecomposedTask(question=task.question,intent=task.primary_intent,structured=task.extraction,
            depends_on=tuple(ids[dep] for dep in task.depends_on)) for task in first.tasks))
    second=compile_decomposition(case[2],resolved)
    assert len(first.tasks)==len(second.tasks)
    assert [(task.extraction.get('时间粒度'),task.extraction.get('维度')) for task in first.tasks]==[
        (task.extraction.get('时间粒度'),task.extraction.get('维度')) for task in second.tasks]
