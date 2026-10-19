"""Every accepted business question keeps a deliverable or an explicit status."""
import pytest

from app.domain.models import AtomicTask, QuestionTaskGroup, TaskExecutionResult, TaskPlan
from app.presentation.root_report import collect_root_materials, render_root_report


def grouped(*, cross_dependency=False, statuses=None):
    tasks = [AtomicTask(task_id='a',question='查询甲基础数据'),
             AtomicTask(task_id='b',question='计算甲交付结果',depends_on=['a']),
             AtomicTask(task_id='c',question='查询乙明细',depends_on=['b'] if cross_dependency else [])]
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=tasks, question_groups=[
        QuestionTaskGroup(question='甲问题的完整业务目标',task_offset=0,task_count=2),
        QuestionTaskGroup(question='乙问题的完整业务目标',task_offset=2,task_count=1)])
    statuses = statuses or {}
    results = [TaskExecutionResult(task_id=task.task_id,question=task.question,
        status=statuses.get(task.task_id,'COMPLETED'),answer=f'{task.task_id}实际状态说明') for task in tasks]
    deferred = {task.task_id:{'presentation':{'table':f'| {task.task_id}数据 |\n| --- |\n| 真实结果 |'}}
                for task in tasks if statuses.get(task.task_id,'COMPLETED')=='COMPLETED'}
    return plan, collect_root_materials(plan,results,{},deferred)


def test_original_question_mapping_survives_collecting_task_results():
    _, items = grouped()
    assert items[0]['question_id'] == items[1]['question_id'] != items[2]['question_id']
    assert items[1]['original_question'] == '甲问题的完整业务目标'
    assert items[2]['original_question'] == '乙问题的完整业务目标'


@pytest.mark.parametrize('selection', [['b'], ['a'], ['c']])
def test_model_cannot_hide_a_successful_independent_question(selection):
    _, items = grouped()
    answer, selected = render_root_report('甲目标和乙目标',items,{'result_task_ids':selection})
    assert {'b','c'}.issubset(selected)
    assert '| b数据 |' in answer and '| c数据 |' in answer


@pytest.mark.parametrize('final', [None, {'result_task_ids':['c']}])
def test_cross_question_consumer_does_not_replace_upstream_question_deliverable(final):
    _, items = grouped(cross_dependency=True)
    answer, selected = render_root_report('分别交付甲与乙',items,final)
    assert set(selected) == {'b','c'}
    assert '| a数据 |' not in answer and '| b数据 |' in answer and '| c数据 |' in answer


def test_failed_question_keeps_original_business_goal_and_specific_reason():
    _, items = grouped(statuses={'c':'FAILED'})
    answer, selected = render_root_report('甲与乙',items,{'result_task_ids':['b']})
    assert selected == ['b']
    assert '乙问题的完整业务目标' in answer and 'c实际状态说明' in answer


def test_all_failed_cross_question_successor_keeps_its_undelivered_status():
    _, items = grouped(cross_dependency=True,statuses={'a':'FAILED','b':'SKIPPED','c':'SKIPPED'})
    answer, selected = render_root_report('分别交付甲与乙',items,None)
    assert not selected
    assert '甲问题的完整业务目标' in answer and '乙问题的完整业务目标' in answer
