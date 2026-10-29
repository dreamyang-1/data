"""Natural follow-up wording must consume the declared predecessor dataset."""
import pytest

from app.domain.models import AtomicTask, AgentResponse, PrimaryIntent
from uuid import uuid4
from test_complex_execution_correctness import service, context, persist, IDENTITY


@pytest.mark.parametrize('reference', ['这家经销商', '这个经销商', '那家经销商', '这些经销商', '该经销商'])
@pytest.mark.asyncio
async def test_dealer_pronouns_keep_the_source_role_and_stable_key(reference):
    await consume(reference + '在2025年销售了哪些产品？')


@pytest.mark.parametrize('question', ['这位销售员销售了哪些产品？', '这三名销售员合作的医院有哪些？',
    '这个医院涉及哪些经销商？', '这款产品的厂家是谁？', '他们销售了哪些产品？', '它们涉及哪些医院？',
    '查询上述前3名销售员在2025年销售过的产品明细', '前述筛选出的10家经销商合作的医院有哪些？'])
@pytest.mark.asyncio
async def test_common_entity_and_person_pronouns_are_data_references(question):
    await consume(question)


@pytest.mark.parametrize('question', ['查询2025年各经销商销售额', '查询上海地区的区域全部医院总数',
    '查询销售员姓名和公司名称', '现在查询所有产品', '那年各经销商订单笔数'])
@pytest.mark.asyncio
async def test_ordering_and_ordinary_queries_are_not_object_references(question):
    await consume(question, consume_values=False)


@pytest.mark.parametrize('question', ['根据上述销售员查询该医院的产品', '根据上述经销商查询这个医院的销售产品'])
@pytest.mark.asyncio
async def test_existing_explicit_source_anchor_precedes_a_target_pronoun(question):
    await consume(question)


async def consume(question, *, consume_values=True):
    agent = service()
    chat, _ = context()
    reference = await persist(agent, chat, ['对象编码'], [{'对象编码':'TEST-1'}],
                              column_bindings={'对象编码':'dealer.code'})
    source = AgentResponse(request_id=uuid4(), conversation_id=chat.conversation_id,
        status='COMPLETED', intent=PrimaryIntent.DETAIL_QUERY, answer='', dataset_id=reference.dataset_id)
    task = AtomicTask(task_id='task-2', question=question, depends_on=['task-1'], dependency_bindings=[{'source_task_id':'task-1','source_column':'对象编码'}] if consume_values else [], extraction={})
    constraints = await agent._compile_dependency_constraints(task,chat,IDENTITY,
        {'task-1':source}, {'task-1':chat.conversation_id})
    if consume_values:
        assert constraints[0].source_column == 'dealer.code'
        assert constraints[0].values == ['TEST-1']
    else:
        assert constraints == []
