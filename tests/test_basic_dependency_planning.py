import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.planning import task_dag
from app.domain import semantic_description


@pytest.mark.asyncio
@pytest.mark.parametrize('local', [False, True])
async def test_existing_planner_handoff_keeps_real_dependency_and_query_choice(monkeypatch, local):
    seen = []
    question = '先查询2025年销售额最高的前3名销售员，再返回他们的销售产品' if not local else '先查询2025年前3名销售员，再仅保留姓名，不再查询数据库'
    second = '他们在2025年销售过哪些产品？' if not local else '根据上述任务结果仅保留销售员姓名，不再查询数据库'
    async def model(request):
        seen.append(json.loads(request.content))
        result = {'intent': 'METRIC_QUERY', 'tasks': [
            {'question': '查询2025年销售额最高的前3名销售员', 'depends_on': [],
             'extraction': {'意图': '统计查询', '指标': [{'name': '含税销售总额'}], '维度': ['业务员'],
               '展示字段': [{'entity': '业务员', 'field': '销售员姓名'}],
               '排序': [{'field': '含税销售总额', 'order': 'desc'}], '限制': 3,
               '时间粒度': {'time_range': '2025年'}, '是否查询数据库': '是'}},
            {'question': second, 'depends_on': [0], 'extraction': {
               '意图': '指标计算' if local else '明细查询', '指标': [], '维度': [],
               '展示字段': [{'entity': '业务员' if local else '商品', 'field': '销售员姓名' if local else '商品名称'}],
               '时间粒度': {'time_range': '2025年'}, '是否查询数据库': '否' if local else '是'}}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)}}]})
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source': 'semantic_model_81.md', 'available': False, 'content': ''}))
    settings = Settings(_env_file=None, env='test', intent_model_api_key=SecretStr('fixture'), multi_question_model_enabled=True)
    result = await task_dag.SingleQuestionPlanner(settings, transport=httpx.MockTransport(model)).plan_question(question, semantic_model_id=81)
    assert len(result.tasks) == 2 and result.tasks[1].depends_on == (0,)
    assert result.tasks[1].structured['限制'] is None
    assert result.tasks[1].structured['时间粒度']['time_range'] == '2025年'
    assert result.tasks[1].structured['是否查询数据库'] == ('否' if local else '是')
    prompt = seen[0]['messages'][0]['content']
    assert '不得把后续返回对象加入前置排名的分组' in prompt
    assert '不为展示字段强制拆任务' in prompt
    assert '不关联的独立任务不消费彼此结果' in prompt


@pytest.mark.parametrize('explicit,existing,metrics', [(False, False, []), (True, False, []),
    (False, True, []), (False, False, [{'name': '既有结果派生计算'}])])
def test_unavailable_local_projection_repairs_only_planner_invented_no_query(explicit, existing, metrics):
    source = {'展示字段': [{'entity': '业务员', 'field': '销售员姓名'}]}
    target = {'展示字段': [{'entity': '业务员' if existing else '商品', 'field': '销售员姓名' if existing else '商品名称'}],
              '指标': metrics, '是否查询数据库': '否', '时间粒度': {'time_range': '2025年'}}
    original = task_dag.QuestionDecomposition(tasks=(
        task_dag.DecomposedTask(question='查询前3名销售员', structured=source),
        task_dag.DecomposedTask(question='查询他们销售的产品，不再查询数据库', depends_on=(0,),
            intent=task_dag.PrimaryIntent.COMPARISON_ANALYSIS, structured=target)))
    result = task_dag._repair_unavailable_local_projection(original,
        '查询前3名销售员及其产品' + ('，不再查询数据库' if explicit else ''))
    repaired = not explicit and not existing and not metrics
    assert result.tasks[1].structured['是否查询数据库'] == ('是' if repaired else '否')
    assert result.tasks[1].depends_on == (0,)
    assert result.tasks[1].structured['时间粒度']['time_range'] == '2025年'
    assert original.tasks[1].structured['是否查询数据库'] == '否'
    if repaired:
        assert result.tasks[1].question.startswith('根据上述任务的查询结果')
        assert '不再查询数据库' not in result.tasks[1].question
        assert result.tasks[1].intent == task_dag.PrimaryIntent.DETAIL_QUERY
