import pytest

from app.planning.compound_question import AtomicTask, TaskPlan, validate_plan
from app.planning.task_dag import TaskPlanningError


@pytest.mark.parametrize('preserved', [True, False])
def test_constraint_phrase_is_compared_as_phrase_not_characters(preserved):
    source = '查询2025年经销商订单笔数，只看上海地区'
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=[
        AtomicTask(task_id='task-1', question='查询2025年经销商订单笔数' + ('，只看上海地区' if preserved else '')),
        AtomicTask(task_id='task-2', question='展示经销商订单笔数', depends_on=['task-1'])])
    if preserved:
        validate_plan(plan, source_question=source)
    else:
        with pytest.raises(TaskPlanningError):
            validate_plan(plan, source_question=source)
