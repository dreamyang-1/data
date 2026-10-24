"""Basic arithmetic must survive planning, task binding and persisted DAG inputs."""
import copy
from unittest.mock import AsyncMock

import pytest

from app.domain.models import AtomicTask, PrimaryIntent, TaskPlan
from app.planning.calculation_bindings import materialize_computation_binding, resolve_compound_formulas
from app.planning.compound_question import deduplicate_plan
from app.services.calculation_execution import calculate
from minio_followup_store import DatasetReference, DatasetScope
from test_complex_execution_correctness import service, context, persist, IDENTITY


def reference(name, columns, rows, **provenance):
    return DatasetReference(dataset_id=name, bucket='test', object_name=name,
        scope=DatasetScope('test','test','test','test'), columns=tuple(columns), row_count=len(rows),
        byte_size=0,snapshot_id='',data_as_of='',created_at='',expires_at='',source_type='TEST',source_ref='',
        transformation_log=({
        'type':'query_provenance', 'source_truncated':False,
        'source_total_row_count':len(rows), 'source_quality_status':'PASS', **provenance},))


def binding(operator, left='付款金额', right='核销金额', sources=('q', 'q'), label='结果'):
    return {'operation':operator, 'left':{'source_task_id':sources[0], 'column':left},
            'right':{'source_task_id':sources[1], 'column':right}, 'result_field':label, 'format':'number'}


def execute(contract, inputs):
    task=AtomicTask(task_id='compute', question='按已登记公式计算', depends_on=list(inputs),
                    extraction={'是否查询数据库':'否','计算绑定':contract})
    return calculate(task, inputs, lambda *args, **kwargs: None)


@pytest.mark.parametrize('operator,left,right,expected', [
    ('add',10,3,13), ('subtract',10,3,7), ('multiply',10,3,30), ('divide',10,4,2.5),
    ('add',-3,3,0), ('subtract',3,10,-7), ('multiply',10,0,0), ('divide',10,-4,-2.5),
])
def test_all_four_operations_on_one_query(operator,left,right,expected):
    rows=[{'部门编码':'D1','付款金额':left,'核销金额':right}]
    source=reference('query',list(rows[0]),rows)
    before=copy.deepcopy(rows)
    columns,result,operation,evidence,quality=execute(binding(operator),{'q':(source,rows)})
    assert result[0]['结果']==expected and result[0]['部门编码']=='D1'
    assert operation['operator']==operator and quality=='PASS'
    assert evidence['left']['dataset_id']==evidence['right']['dataset_id']=='query'
    assert not any(name.startswith('_') for name in columns)
    assert rows==before


@pytest.mark.parametrize('operator,expected',[
    ('add',{'D1':13,'D2':25}), ('subtract',{'D1':7,'D2':15}),
    ('multiply',{'D1':30,'D2':100}), ('divide',{'D1':10/3,'D2':4}),
])
def test_same_named_measures_align_by_code_across_reordered_queries(operator,expected):
    left=[{'部门编码':'D1','金额':10},{'部门编码':'D2','金额':20}]
    right=[{'部门编码':'D2','金额':5},{'部门编码':'D1','金额':3}]
    a,b=reference('a',list(left[0]),left),reference('b',list(right[0]),right)
    original=copy.deepcopy(right)
    _,rows,_,evidence,_=execute(binding(operator,'金额','金额',('a','b')),{'a':(a,left),'b':(b,right)})
    assert {row['部门编码']:row['结果'] for row in rows}==pytest.approx(expected)
    assert evidence['right']['column']=='金额' and right==original and b.columns==tuple(right[0])


@pytest.mark.parametrize('operator,left_scalar,expected',[
    ('subtract',True,{'D1':8,'D2':5}), ('subtract',False,{'D1':-8,'D2':-5}),
    ('multiply',True,{'D1':20,'D2':50}), ('divide',False,{'D1':0.2,'D2':0.5}),
])
def test_scalar_broadcast_preserves_operand_direction(operator,left_scalar,expected):
    grouped=[{'部门编码':'D1','金额':2},{'部门编码':'D2','金额':5}]; scalar=[{'金额':10}]
    a=(reference('groups',list(grouped[0]),grouped),grouped)
    b=(reference('scalar',['金额'],scalar),scalar)
    inputs={'a':b if left_scalar else a,'b':a if left_scalar else b}
    _,rows,*_=execute(binding(operator,'金额','金额',('a','b')),inputs)
    assert {row['部门编码']:row['结果'] for row in rows}==pytest.approx(expected)


@pytest.mark.parametrize('value',[None,True,'NaN','Infinity','unknown'])
def test_invalid_numbers_are_not_silently_replaced_with_zero(value):
    rows=[{'付款金额':value,'核销金额':2}]
    with pytest.raises(ValueError):
        execute(binding('subtract'),{'q':(reference('q',list(rows[0]),rows),rows)})


def test_zero_divisor_and_old_positive_ratio_contract_remain_protected():
    for number in (0,-2):
        rows=[{'付款金额':10,'核销金额':number}]; source=reference('q',list(rows[0]),rows)
        contract=binding('divide')
        contract['numerator']=contract.pop('left');contract['denominator']=contract.pop('right')
        with pytest.raises(ValueError,match='分母'):
            execute(contract,{'q':(source,rows)})
        if number==0:
            with pytest.raises(ValueError,match='分母'):
                execute(binding('divide'),{'q':(source,rows)})


@pytest.mark.parametrize('mode',['mismatched','duplicate','truncated','failed_quality'])
def test_arithmetic_cannot_hide_incomplete_or_incompatible_inputs(mode):
    left=[{'部门编码':'D1','付款金额':10}]
    right=[{'部门编码':'D2' if mode=='mismatched' else 'D1','核销金额':3}]
    if mode=='duplicate':right*=2
    a=reference('a',list(left[0]),left)
    b=reference('b',list(right[0]),right,source_truncated=mode=='truncated',
                source_quality_status='FAIL' if mode=='failed_quality' else 'PASS')
    if mode in {'truncated','failed_quality'}:
        # 2026-10-21 用户裁决：任务只要能输出就输出，校验尽可能宽松。
        # 输入截断/质量失败只告警继续计算（溯源保留质量标记），不再拒绝。
        columns,rows,operation,evidence,quality=execute(
            binding('subtract',sources=('a','b')),{'a':(a,left),'b':(b,right)})
        assert rows
        return
    with pytest.raises(ValueError):
        execute(binding('subtract',sources=('a','b')),{'a':(a,left),'b':(b,right)})


@pytest.mark.parametrize('operator',['add','subtract','multiply','divide'])
def test_binding_and_deduplication_remap_both_operand_roles(operator):
    contract=binding(operator,sources=('task-2','task-1'))
    raw={'计算绑定':copy.deepcopy(contract)}
    for role,index in [('left',1),('right',0)]:
        raw['计算绑定'][role].pop('source_task_id');raw['计算绑定'][role]['source_task_index']=index
    normalized=materialize_computation_binding(raw,task_index=2,task_offset=3,dependencies=['task-4','task-5'])
    assert normalized['计算绑定']['left']['source_task_id']=='task-5'
    assert normalized['计算绑定']['right']['source_task_id']=='task-4'
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id='task-1',question='查询付款金额和核销金额'),
        AtomicTask(task_id='task-2',question='查询付款金额和核销金额'),
        AtomicTask(task_id='task-3',question='计算，不再查询数据库',depends_on=['task-1','task-2'],
                   extraction={'计算绑定':contract})])
    reduced,aliases=deduplicate_plan(plan)
    assert aliases=={'task-2':'task-1'}
    assert reduced.tasks[-1].extraction['计算绑定']['left']['source_task_id']=='task-1'
    assert contract['left']['source_task_id']=='task-2'


@pytest.mark.asyncio
@pytest.mark.parametrize('operator,symbol',[
    ('add','+'),('subtract','减去'),('multiply','乘以'),('divide','÷')])
async def test_registered_formulas_determine_operand_order(operator,symbol):
    spec=f'| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n| 差额 | 无 | 付款金额 {symbol} 核销金额 | 元 | 核销金额, 付款金额 |'
    result=await resolve_compound_formulas(service().settings,None,spec,{'差额':('核销金额','付款金额')},['差额'])
    roles=('numerator','denominator') if operator=='divide' else ('left','right')
    assert result['差额']['operation']==operator
    assert result['差额'][roles[0]]['column']=='付款金额'
    assert result['差额'][roles[1]]['column']=='核销金额'


@pytest.mark.parametrize('operator,symbol,count',[
    ('add','+',2),('subtract','减去',2),('multiply','×',2),('divide','÷',3)])
def test_auto_expansion_keeps_new_arithmetic_components_in_one_population(operator,symbol,count):
    from app.planning.task_plan_stages import TaskBoundary, complete_boundaries
    spec=f'| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n| 公式结果 | 无 | 付款金额 {symbol} 核销金额 | 元 | 核销金额, 付款金额 |'
    tasks=complete_boundaries([TaskBoundary(question='查询2026年各部门公式结果',
        metric_targets=['公式结果'],grain=['部门'])],spec=spec,
        catalog=(frozenset({'公式结果','付款金额','核销金额'}),{}),
        dependencies={'公式结果':('核销金额','付款金额')})
    assert len(tasks)==count
    if operator!='divide':
        assert tasks[0].metric_targets==['核销金额','付款金额']
        assert tasks[-1].depends_on==[0]
    else:
        assert tasks[-1].depends_on==[0,1]  # Preserve the published ratio split.


@pytest.mark.asyncio
async def test_original_receivable_question_is_planned_before_bound_subtraction(monkeypatch):
    from app.domain import semantic_description
    from test_task_planning_stages import setup_planner
    from app.planning.compound_question import CompoundQuestionRouter, CompoundJudgment
    question='查询2026年各部门开票应收金额，结果需要展示部门名称'
    planner,calls=setup_planner(monkeypatch,{'tasks':[{'question':question,
        'metric_targets':['开票应收金额'],'grain':['部门']}]})
    spec=('| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |\n'
          '| 开票应收金额 | 无 | 付款金额减去核销金额的差值总和 | 元 | 复合口径指标 | 核销金额, 付款金额 |\n'
          '| 付款金额 | 无 | 付款金额合计 | 元 | 原子指标 | 无 |\n'
          '| 核销金额 | 无 | 核销金额合计 | 元 | 原子指标 | 无 |')
    monkeypatch.setattr(semantic_description,'load_semantic_description',AsyncMock(return_value={
        'available':True,'source':'semantic_model_133.md','content':spec}))
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    outcome=await router.plan_with_judgment(CompoundJudgment(is_compound=False),question,semantic_model_id=133)
    assert outcome.plan is not None and len(outcome.plan.tasks)==2
    assert len(calls)==2
    contract=outcome.plan.tasks[-1].extraction['计算绑定']
    assert contract['operation']=='subtract'
    assert contract['left']['source_task_id']=='task-1'
    assert contract['right']['source_task_id']=='task-1'
    assert outcome.plan.tasks[0].extraction['指标']==[{'name':'核销金额'},{'name':'付款金额'}]
    assert all(task.extraction['维度']==['部门'] for task in outcome.plan.tasks)


def _ratio_extract(fixed):
    items = []
    for task in fixed['tasks']:
        compute = '不再查询数据库' in task['question']
        value = {'意图': '指标计算' if compute else '明细查询', '指标': [],
            '维度': ['经销商'], '展示字段': [], '过滤条件': [],
            '是否查询数据库': '否' if compute else '是', '输出要求': '默认输出表格'}
        if compute:
            # 模型把公式绑定成了退化形态：分子分母同名（X÷X）。
            value['计算绑定'] = {'operation': 'divide', 'left': {'column': '付款金额'},
                'right': {'column': '付款金额'}, 'result_field': '付款核销比', 'format': 'number'}
        items.append({'task_index': task['task_index'], 'extraction': value})
    return {'tasks': items}


@pytest.mark.asyncio
async def test_published_formula_overrides_model_binding_for_registered_compound_metric(monkeypatch):
    # 2026-10-21 用户要求：语义层已发布的复合指标公式是计算任务的公式来源。
    # 模型推断的退化计算绑定（X÷X）在发布公式能唯一绑定成分任务时被覆盖。
    from app.domain import semantic_description
    from test_task_planning_stages import setup_planner
    from app.planning.compound_question import CompoundQuestionRouter, CompoundJudgment
    question = '查询2026年各经销商付款核销比'
    planner, calls = setup_planner(monkeypatch, {'tasks': [{'question': question,
        'metric_targets': ['付款核销比'], 'grain': ['经销商']}]}, extract=_ratio_extract)
    spec = ('| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n'
            '| 付款核销比 | 无 | 付款金额÷核销金额 | 元 | 核销金额, 付款金额 |\n'
            '| 付款金额 | 无 | 付款金额合计 | 元 | 无 |\n'
            '| 核销金额 | 无 | 核销金额合计 | 元 | 无 |')
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'available': True, 'source': 'semantic_model_133.md', 'content': spec}))
    router = CompoundQuestionRouter(planner.settings); router._single = planner
    outcome = await router.plan_with_judgment(CompoundJudgment(is_compound=False), question, semantic_model_id=133)
    assert outcome.plan is not None and len(outcome.plan.tasks) == 3
    assert len(calls) == 2  # 发布公式精确解析，无重试、无公式绑定兜底调用
    contract = outcome.plan.tasks[-1].extraction['计算绑定']
    assert contract['operation'] == 'divide'
    assert contract['numerator']['column'] == '付款金额'
    assert contract['denominator']['column'] == '核销金额'
    assert contract['numerator']['source_task_id'] != contract['denominator']['source_task_id']


@pytest.mark.asyncio
async def test_unparsable_published_formula_keeps_lenient_plan_without_override(monkeypatch):
    # 发布公式含多步运算无法解析为单一计算绑定时，宽松化不拦停规划：
    # 退化绑定在重试耗尽后按既有契约丢弃，任务留待计算执行器兜底。
    from app.domain import semantic_description
    from test_task_planning_stages import setup_planner
    from app.planning.compound_question import CompoundQuestionRouter, CompoundJudgment
    question = '查询2026年各经销商付款核销比'
    planner, calls = setup_planner(monkeypatch, {'tasks': [{'question': question,
        'metric_targets': ['付款核销比'], 'grain': ['经销商']}]}, extract=_ratio_extract)
    spec = ('| 指标 | 同义词 | 业务口径 | 单位 | 依赖指标 |\n'
            '| 付款核销比 | 无 | 付款金额÷核销金额×100 | 元 | 核销金额, 付款金额 |\n'
            '| 付款金额 | 无 | 付款金额合计 | 元 | 无 |\n'
            '| 核销金额 | 无 | 核销金额合计 | 元 | 无 |')
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'available': True, 'source': 'semantic_model_133.md', 'content': spec}))
    router = CompoundQuestionRouter(planner.settings); router._single = planner
    outcome = await router.plan_with_judgment(CompoundJudgment(is_compound=False), question, semantic_model_id=133)
    assert outcome.plan is not None and len(outcome.plan.tasks) == 3
    contract = outcome.plan.tasks[-1].extraction.get('计算绑定')
    assert not contract or contract['left']['column'] == contract['right']['column']


@pytest.mark.asyncio
async def test_persisted_calculation_results_can_feed_later_arithmetic_tasks():
    agent=service();chat,_=context()
    await persist(agent,chat,['部门','付款金额','核销金额','系数'],[
        {'部门':'D1','付款金额':100,'核销金额':30,'系数':2},
        {'部门':'D2','付款金额':40,'核销金额':50,'系数':3}],conversation='q',
        column_bindings={'部门':'department.department_code'})
    sources={'q':'q'}
    for name,contract,expected in [
        ('difference',binding('subtract'),[70,-10]),
        ('product',binding('multiply','结果','系数',('difference','difference'),label='乘积'),[140,-30]),
        ('sum',binding('add','乘积','付款金额',('product','product'),label='合计'),[240,10]),
        ('quotient',binding('divide','合计','系数',('sum','sum'),label='商'),[120,10/3]),
    ]:
        dependencies=list(dict.fromkeys(contract[role]['source_task_id'] for role in ('left','right')))
        result=await agent._respond_pure_computation(chat,IDENTITY,AtomicTask(task_id=name,
            question='根据上一步结果计算，不再查询数据库',depends_on=dependencies,
            extraction={'是否查询数据库':'否','计算绑定':contract}),sources)
        assert result.status=='COMPLETED'
        assert [row[contract['result_field']] for row in result.evidence[0].payload['preview']]==pytest.approx(expected)
        sources[name]=result.conversation_id


@pytest.mark.asyncio
async def test_derived_single_group_is_not_mistaken_for_a_global_scalar():
    agent=service();chat,_=context()
    await persist(agent,chat,['部门','付款金额','核销金额'],[
        {'部门':'D1','付款金额':100,'核销金额':30}],conversation='q',
        column_bindings={'部门':'department.department_code'})
    result=await agent._respond_pure_computation(chat,IDENTITY,AtomicTask(task_id='difference',
        question='计算差额',depends_on=['q'],extraction={'计算绑定':binding('subtract')}),{'q':'q'})
    assert result.status=='COMPLETED'
    await persist(agent,chat,['部门','税额'],[{'部门':'D2','税额':5},{'部门':'D1','税额':2}],
        conversation='tax',column_bindings={'部门':'department.department_code'})
    next_task=AtomicTask(task_id='net',question='按部门扣除税额',depends_on=['difference','tax'],
        extraction={'计算绑定':binding('subtract','结果','税额',('difference','tax'),label='净额')})
    next_result=await agent._respond_pure_computation(chat,IDENTITY,next_task,
        {'difference':result.conversation_id,'tax':'tax'})
    assert next_result.status=='FAILED' and not next_result.dataset_id


def test_cross_task_divide_with_single_bound_spec_still_computes():
    # 2026-10-21 用户裁决宽松放行：规划器把跨任务除法写成绑定了单一依赖的
    # 结果操作（依赖实际有两个）时，按题面显式除法措辞解析继续计算。
    sales = reference('sales', ('经销商（编码）', '含税销售总额'),
                      [{'经销商（编码）': 'D1', '含税销售总额': 100},
                       {'经销商（编码）': 'D2', '含税销售总额': 60}])
    hospitals = reference('hospitals', ('经销商（编码）', '有效订单去重医院数'),
                          [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                           {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])
    task = AtomicTask(task_id='compute',
        question='计算单院产出，含税销售总额/有效订单去重医院数',
        depends_on=['sales', 'hospitals'],
        extraction={'是否查询数据库': '否',
                    '结果操作': {'source_task_id': 'sales', 'operation': {'type': 'select'}}})
    columns, result, operation, evidence, quality = calculate(
        task, {'sales': (sales, [{'经销商（编码）': 'D1', '含税销售总额': 100},
                                 {'经销商（编码）': 'D2', '含税销售总额': 60}]),
               'hospitals': (hospitals, [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                                         {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])},
        lambda *args, **kwargs: None)
    values = {row['经销商（编码）']: row['单院产出'] for row in result}
    assert values == {'D1': 25.0, 'D2': 20.0} and quality == 'PASS'


def test_unbound_result_operation_passthrough_instead_of_failure():
    # 2026-10-21 用户裁决：结果操作解析不出公式时透传依赖数据集并告警，
    # 不再把计算任务打成执行失败。
    sales = reference('sales', ('经销商（编码）', '含税销售总额'),
                      [{'经销商（编码）': 'D1', '含税销售总额': 100}])
    hospitals = reference('hospitals', ('经销商（编码）', '有效订单去重医院数'),
                          [{'经销商（编码）': 'D1', '有效订单去重医院数': 4}])
    task = AtomicTask(task_id='compute', question='按既定口径整理这些商户',
        depends_on=['sales', 'hospitals'],
        extraction={'是否查询数据库': '否',
                    '结果操作': {'source_task_id': 'ghost', 'operation': {'type': 'select'}}})
    columns, result, operation, evidence, quality = calculate(
        task, {'sales': (sales, [{'经销商（编码）': 'D1', '含税销售总额': 100}]),
               'hospitals': (hospitals, [{'经销商（编码）': 'D1', '有效订单去重医院数': 4}])},
        lambda *args, **kwargs: None)
    assert result and quality == 'PASS'
    assert operation['type'] == 'select'


def test_degenerate_divide_binding_is_rederived_not_output_as_one():
    # 2026-10-21 线上缺陷：规划器把除法两个操作数绑成同一列（医院数÷医院数
    # 恒为1）。按宽松裁决回退：两个依赖各恰有一个数值指标时确定性重导除法。
    sales = reference('sales', ('经销商（编码）', '含税销售总额'),
                      [{'经销商（编码）': 'D1', '含税销售总额': 100},
                       {'经销商（编码）': 'D2', '含税销售总额': 60}])
    hospitals = reference('hospitals', ('经销商（编码）', '有效订单去重医院数'),
                          [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                           {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])
    task = AtomicTask(task_id='compute', question='计算这些商户的单院产出并按降序排列',
        depends_on=['sales', 'hospitals'],
        extraction={'是否查询数据库': '否', '计算绑定': binding(
            'divide', '有效订单去重医院数', '有效订单去重医院数', ('hospitals', 'hospitals'),
            label='单院产出')})
    columns, result, operation, evidence, quality = calculate(
        task, {'sales': (sales, [{'经销商（编码）': 'D1', '含税销售总额': 100},
                                 {'经销商（编码）': 'D2', '含税销售总额': 60}]),
               'hospitals': (hospitals, [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                                         {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])},
        lambda *args, **kwargs: None)
    values = {row['经销商（编码）']: row['单院产出'] for row in result}
    assert values == {'D1': 25.0, 'D2': 20.0} and quality == 'PASS'
    assert operation['operator'] == 'divide'


def test_dimension_sort_field_falls_back_to_metric_value_ordering():
    # 2026-10-21 用户要求恢复默认：计算任务的排序字段被规划器写成维度名称
    # （如"经销商名称"）时，回退到计算结果数值列按声明方向排序。
    sales = reference('sales', ('经销商（编码）', '含税销售总额'),
                      [{'经销商（编码）': 'D1', '含税销售总额': 100},
                       {'经销商（编码）': 'D2', '含税销售总额': 60}])
    hospitals = reference('hospitals', ('经销商（编码）', '有效订单去重医院数'),
                          [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                           {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])
    task = AtomicTask(task_id='compute', question='计算这些商户的单院产出并排序',
        depends_on=['sales', 'hospitals'],
        extraction={'是否查询数据库': '否', '计算绑定': binding(
            'divide', '含税销售总额', '有效订单去重医院数', ('sales', 'hospitals'),
            label='单院产出'),
            '排序': [{'field': '经销商名称', 'order': 'desc'}]})
    columns, result, operation, evidence, quality = calculate(
        task, {'sales': (sales, [{'经销商（编码）': 'D1', '含税销售总额': 100},
                                 {'经销商（编码）': 'D2', '含税销售总额': 60}]),
               'hospitals': (hospitals, [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                                         {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])},
        lambda *args, **kwargs: None)
    assert [row['单院产出'] for row in result] == [25.0, 20.0] and quality == 'PASS'


def test_computation_without_sort_slot_defaults_to_result_descending():
    # 2026-10-21 用户要求恢复默认：计算任务未携带排序槽位时按计算结果列
    # 默认降序（与指标类查询默认降序一致）。
    sales = reference('sales', ('经销商（编码）', '含税销售总额'),
                      [{'经销商（编码）': 'D1', '含税销售总额': 100},
                       {'经销商（编码）': 'D2', '含税销售总额': 60}])
    hospitals = reference('hospitals', ('经销商（编码）', '有效订单去重医院数'),
                          [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                           {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])
    task = AtomicTask(task_id='compute', question='计算这些商户的单院产出',
        depends_on=['sales', 'hospitals'],
        extraction={'是否查询数据库': '否', '计算绑定': binding(
            'divide', '含税销售总额', '有效订单去重医院数', ('sales', 'hospitals'),
            label='单院产出')})
    columns, result, operation, evidence, quality = calculate(
        task, {'sales': (sales, [{'经销商（编码）': 'D1', '含税销售总额': 100},
                                 {'经销商（编码）': 'D2', '含税销售总额': 60}]),
               'hospitals': (hospitals, [{'经销商（编码）': 'D1', '有效订单去重医院数': 4},
                                         {'经销商（编码）': 'D2', '有效订单去重医院数': 3}])},
        lambda *args, **kwargs: None)
    assert [row['单院产出'] for row in result] == [25.0, 20.0] and quality == 'PASS'
