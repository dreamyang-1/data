import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.planning.compound_question import AtomicTask, TaskPlan, validate_plan
from app.planning.task_dag import SingleQuestionPlanner, TaskPlanningError


@pytest.mark.asyncio
@pytest.mark.parametrize('source,default,accepted', [
    ('查询各经销商的医院数', '最近12个月', True),
    ('查询2025年各经销商的医院数', '最近12个月', False),
    ('查询各经销商的医院数', '最近14个月', False),
])
async def test_registered_default_stays_in_extraction_without_breaking_task_grounding(
        monkeypatch, source, default, accepted):
    spec = '用户查询指标、明细数据，未指定时间范围时，默认取最近 12 个月的数据。'
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source': 'semantic_model_81.md', 'available': True, 'content': spec}))

    async def model(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'intent': 'METRIC_QUERY', 'tasks': [
                {'question': f'查询{default}各经销商的医院数', 'extraction': {
                    '意图': '统计查询', '指标': [],
                    '时间粒度': {'time_range': default}, '是否查询数据库': '是'}},
                {'question': '根据上述结果显示各经销商的医院数，不再查询数据库',
                 'depends_on': [0], 'extraction': {'意图': '指标计算', '指标': [],
                    '时间粒度': {'time_range': default}, '是否查询数据库': '否'}},
            ]}, ensure_ascii=False)}}]})

    settings = Settings(_env_file=None, env='test', intent_model_api_key=SecretStr('fixture'),
                        multi_question_model_enabled=True)
    decomposition = await SingleQuestionPlanner(settings, transport=httpx.MockTransport(model)).plan_question(
        source, semantic_model_id=81)
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=[AtomicTask(task_id=f'task-{i+1}', question=task.question,
        depends_on=[f'task-{dep+1}' for dep in task.depends_on])
        for i, task in enumerate(decomposition.tasks)])
    assert decomposition.tasks[0].structured['时间粒度']['time_range'] == default
    defaults = getattr(decomposition, 'grounding_default_times', ())
    options = {'registered_default_times': defaults} if defaults else {}
    if accepted:
        validate_plan(plan, source_question=source, **options)
        assert '12' in decomposition.tasks[0].question
    else:
        with pytest.raises(TaskPlanningError):
            validate_plan(plan, source_question=source, **options)
