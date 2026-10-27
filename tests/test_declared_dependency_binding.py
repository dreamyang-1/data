"""Task prose cannot choose or overwrite an explicitly declared source column."""
import copy
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse, AtomicTask, PrimaryIntent, TaskPlan
from app.planning.calculation_bindings import materialize_computation_binding
from app.planning.compound_question import deduplicate_plan
from app.planning.query_dependency_bindings import select_dependency_column
from app.planning.task_dag import SingleQuestionPlanner, TaskInputBindingError
from app.services.orchestrator import DependencyConstraintError
from test_complex_execution_correctness import service, context, persist, IDENTITY


QUESTIONS = [
    '根据前序任务选出的2025年复旦大学附属华山医院含税销售总额最高的销售员，查询该销售员在2025年向复旦大学附属华山医院销售的全部产品明细',
    '查询前序任务选出的2025年复旦大学附属华山医院含税销售总额最高的销售员在2025年向复旦大学附属华山医院销售的全部产品明细',
    '查询其所属销售公司',
    '查询产品',
]


@pytest.mark.asyncio
@pytest.mark.parametrize('question', QUESTIONS)
async def test_both_observed_wordings_pass_the_exact_declared_code(question):
    agent=service();chat,_=context()
    rows=[{'业务员':'SYNTH-01','销售员姓名':'同名','医院':'H-02','销售额':42}]
    ref=await persist(agent,chat,list(rows[0]),rows,column_bindings={
        '业务员':'salesperson.salesperson_code','销售员姓名':'salesperson.salesperson_name',
        '医院':'hospital.hospital_id'})
    response=AgentResponse(request_id=uuid4(),conversation_id=chat.conversation_id,
        status='COMPLETED',intent=PrimaryIntent.DETAIL_QUERY,answer='不提供任何对象线索',dataset_id=ref.dataset_id)
    extraction=materialize_computation_binding({'依赖绑定':[{'source_task_index':0,'source_column':'业务员'}]},
        task_index=1,dependencies=['task-1'])
    task=AtomicTask(task_id='task-2',question=question,depends_on=['task-1'],extraction=extraction)
    result=await agent._compile_dependency_constraints(task,chat,IDENTITY,
        {'task-1':response},{'task-1':chat.conversation_id})
    assert result[0].source_column=='salesperson.salesperson_code'
    assert result[0].values==['SYNTH-01']


@pytest.mark.asyncio
async def test_order_only_dependency_is_explicit_and_missing_binding_cannot_drop_filter():
    agent=service();chat,_=context()
    task=AtomicTask(task_id='task-2',question='任何描述',depends_on=['task-1'],extraction={'依赖绑定':[]})
    assert await agent._compile_dependency_constraints(task,chat,IDENTITY,{}, {})==[]
    task.extraction={}
    with pytest.raises(DependencyConstraintError,match='BINDING_MISSING'):
        await agent._compile_dependency_constraints(task,chat,IDENTITY,{}, {})


def test_identical_values_or_similar_labels_do_not_merge_distinct_field_identities():
    columns=('业务员ID','销售员编码','医院编码')
    fields={'业务员ID':'salesperson.id','销售员编码':'salesperson.salesperson_code','医院编码':'hospital.hospital_id'}
    assert select_dependency_column('salesperson.id',columns,fields)=='业务员ID'
    assert select_dependency_column('salesperson.salesperson_code',columns,fields)=='销售员编码'
    with pytest.raises(ValueError,match='DECLARED_COLUMN_MISSING'):
        select_dependency_column('hospital.salesperson_code',columns,fields)
    with pytest.raises(ValueError,match='DECLARED_COLUMN_MISSING'):
        select_dependency_column('业务员',columns,fields)


def test_indices_resolve_once_and_deduplication_remaps_declared_query_sources():
    extraction={'依赖绑定':[{'source_task_index':0,'source_column':'编码'}]}
    before=copy.deepcopy(extraction)
    resolved=materialize_computation_binding(extraction,task_index=1,task_offset=3,dependencies=['task-4'])
    assert extraction==before
    assert resolved=={'依赖绑定':[{'source_task_id':'task-4','source_column':'编码'}]}
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[
        AtomicTask(task_id='task-1',question='查询对象'),
        AtomicTask(task_id='task-2',question='查询对象'),
        AtomicTask(task_id='task-3',question='查询公司',depends_on=['task-2'],extraction={
            '依赖绑定':[{'source_task_id':'task-2','source_column':'编码'}]})])
    new,aliases=deduplicate_plan(plan)
    assert aliases=={'task-2':'task-1'}
    assert new.tasks[-1].extraction['依赖绑定'][0]['source_task_id']=='task-1'
    assert plan.tasks[-1].extraction['依赖绑定'][0]['source_task_id']=='task-2'


@pytest.mark.parametrize('index',[True,2,-1,'0'])
def test_binding_never_creates_a_new_dependency(index):
    with pytest.raises(TaskInputBindingError):
        materialize_computation_binding({'依赖绑定':[{'source_task_index':index,'source_column':'编码'}]},
            task_index=1,dependencies=['task-1'])


def test_parameter_sanitization_preserves_binding_and_explicit_empty_list():
    for bindings in [[],[{'source_task_index':0,'source_column':'业务员'}]]:
        result=SingleQuestionPlanner._sanitize_extraction({'依赖绑定':bindings},intent=PrimaryIntent.DETAIL_QUERY)
        assert result['依赖绑定']==bindings


def test_display_label_resolves_only_through_executed_physical_field():
    from app.planning.query_dependency_bindings import executed_projection_bindings, inherited_projection_bindings
    from types import SimpleNamespace
    from app.adapters.asl_notices import attach_binding_notices
    columns = {'业务员': 'salesperson.code', '销售员ID': 'salesperson.id'}
    result = SimpleNamespace(execution_transforms=[])
    attach_binding_notices(result, [{'type': 'STRUCTURED_PROJECTION_BINDING',
        'source': 'STRUCTURED_EXTRACTION', 'field': '销售员编码', 'entity': '业务员',
        'resolved_field': 'salesperson.code'}])
    aliases = executed_projection_bindings(result.execution_transforms, columns)
    assert select_dependency_column('销售员编码', tuple(columns), columns, aliases) == '业务员'
    assert select_dependency_column('销售员ID', tuple(columns), columns, aliases) == '销售员ID'
    assert executed_projection_bindings(result.execution_transforms, {'业务员':'salesperson.id'}) == {}
    reference = SimpleNamespace(transformation_log=[{'type':'query_provenance','projection_bindings':aliases}])
    assert inherited_projection_bindings(columns, {'task-1':(reference, [])}) == aliases
    assert inherited_projection_bindings({'业务员':'salesperson.id'}, {'task-1':(reference, [])}) == {}


def test_same_display_name_on_distinct_fields_stays_ambiguous():
    from app.planning.query_dependency_bindings import executed_projection_bindings
    records = [{'type':'STRUCTURED_PROJECTION_BINDING','entity':owner,'field':'编码','resolved_field':field}
        for owner,field in [('业务员','salesperson.id'),('销售员','salesperson.code')]]
    bindings = {'业务员':'salesperson.id','销售员':'salesperson.code'}
    aliases = executed_projection_bindings(records, bindings)
    assert '编码' not in aliases
    assert select_dependency_column('业务员.编码', tuple(bindings), bindings, aliases) == '业务员'
    assert select_dependency_column('销售员.编码', tuple(bindings), bindings, aliases) == '销售员'


def test_outer_question_binding_is_resolved_after_group_dependencies_are_attached():
    from app.planning.calculation_bindings import validate_computation_column_lineage
    from app.planning.task_dag import DecomposedTask,QuestionDecomposition
    structured={'依赖绑定':[{'source_column':'经销商编码'}], '展示字段':[{'entity':'商品','field':'商品名称'}]}
    validate_computation_column_lineage(QuestionDecomposition(tasks=(DecomposedTask(
        question='查询前一个子问题选出的经销商销售的产品',structured=structured),)))
    resolved=materialize_computation_binding(structured,task_index=0,task_offset=2,dependencies=['task-2'])
    assert resolved['依赖绑定']==[{'source_task_id':'task-2','source_column':'经销商编码'}]
