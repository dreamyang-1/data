"""Natural follow-up wording must consume the declared predecessor dataset."""
import pytest

from app.domain.models import AtomicTask, TaskPlan
from app.services.orchestrator import DataAnalysisOrchestrator as Service


@pytest.mark.parametrize('reference', ['这家经销商', '这个经销商', '那家经销商', '这些经销商', '该经销商'])
def test_dealer_pronouns_keep_the_source_role_and_stable_key(reference):
    question = reference + '在2025年销售了哪些产品？'
    assert Service._dag_references_dependency_result(question)
    columns = ('经销商名称', '经销商（编码）', '含税销售总额')
    assert Service._select_dependency_column(question, columns,
        ({columns[0]: '测试对象', columns[1]: 'D001', columns[2]: 42},),
        {'维度': ['经销商']}) == columns[1]
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[
        AtomicTask(task_id='task-1', question='查询经销商排名'),
        AtomicTask(task_id='task-2', question=question, depends_on=['task-1'])])
    assert Service._dependency_key_families('task-1', plan) == ('supplier',)


@pytest.mark.parametrize('question', ['这位销售员销售了哪些产品？', '这三名销售员合作的医院有哪些？',
    '这个医院涉及哪些经销商？', '这款产品的厂家是谁？', '他们销售了哪些产品？', '它们涉及哪些医院？',
    '查询上述前3名销售员在2025年销售过的产品明细', '前述筛选出的10家经销商合作的医院有哪些？'])
def test_common_entity_and_person_pronouns_are_data_references(question):
    assert Service._dag_references_dependency_result(question)


@pytest.mark.parametrize('question', ['查询2025年各经销商销售额', '查询上海地区的区域全部医院总数',
    '查询销售员姓名和公司名称', '现在查询所有产品', '那年各经销商订单笔数'])
def test_ordering_and_ordinary_queries_are_not_object_references(question):
    assert not Service._dag_references_dependency_result(question)


@pytest.mark.parametrize('question', ['根据上述销售员查询该医院的产品', '根据上述经销商查询这个医院的销售产品'])
def test_existing_explicit_source_anchor_precedes_a_target_pronoun(question):
    assert Service._dependency_anchor_text(question).startswith('上述')
