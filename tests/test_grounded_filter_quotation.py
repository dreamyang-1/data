import pytest

from app.planning.compound_question import AtomicTask, TaskPlan, validate_plan
from app.planning.task_dag import TaskPlanningError


@pytest.mark.parametrize('source_value,task_value,quoted,accepted', [
    ('消化内科', '消化内科', False, True),
    ('消化内科', '心血管内科', False, False),
    ('消化内科', '心血管内科', True, False),
])
def test_task_quoting_does_not_invent_a_literal_already_in_the_user_question(
        source_value, task_value, quoted, accepted):
    department = f'“{source_value}”' if quoted else source_value
    source = f'上海地区哪个医院{department}2025年9月份的订单数量最多？该医院当月销售了哪些产品？'
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=[
        AtomicTask(task_id='task-1', question=f'统计2025年9月上海地区适用科室为“{task_value}”的医院订单笔数并取最高医院',
                   extraction={'限制':1,'维度':['医院']}),
        AtomicTask(task_id='task-2', question='根据上述结果查询该医院2025年9月销售的产品',depends_on=['task-1']),
    ])
    if accepted:
        validate_plan(plan, source_question=source)
    else:
        with pytest.raises(TaskPlanningError):
            validate_plan(plan, source_question=source)
