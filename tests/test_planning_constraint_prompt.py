import pytest

from app.domain.models import PrimaryIntent
from test_task_planning_stages import setup_planner


@pytest.mark.asyncio
@pytest.mark.parametrize('independent', [False, True])
async def test_completed_question_intent_and_scope_reach_both_model_stages(monkeypatch, independent):
    first = '统计2025年9月到12月医院A各销售员含税销售总额，降序取第1名'
    second = ('查询2024年医院B销售的全部产品' if independent else
              '查询前序任务选出的销售员在2025年9月到12月向医院A销售的全部产品')
    question = first + '；' + second
    plan = {'tasks': [
        {'question': first, 'metric_targets': ['含税销售总额'], 'grain': ['销售员']},
        {'question': second, 'depends_on': [] if independent else [0]},
    ]}
    planner, calls = setup_planner(monkeypatch, plan)
    result = await planner.plan_question(question, semantic_model_id=130,
                                         root_intent=PrimaryIntent.DETAIL_QUERY)
    assert len(calls) == 2
    for call in calls:
        system = call['messages'][0]['content']
        assert '已识别任务意图：DETAIL_QUERY' in system
        assert '含税金额合计' in system  # authoritative semantic file
        assert call['messages'][1]['content'] == question
    assert '每项条件必须出现在适用任务的question中' in calls[0]['messages'][0]['content']
    assert '不能只在输出要求中描述这些约束' in calls[1]['messages'][0]['content']
    assert result.tasks[1].question == second
    assert result.tasks[1].depends_on == (() if independent else (0,))
