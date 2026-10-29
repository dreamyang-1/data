import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.planning.task_dag import SingleQuestionPlanner


REGISTERED = '''
| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |
| --- | --- | --- | --- | --- | --- |
| 已合作医院数 | 无 | 经销商有效销售订单内的去重医院数 | 家 | 原子指标 | 无 |
| 区域全部医院总数 | 无 | 与医院是否产生销售订单无关；同一区域全部医院数，不接受经销商条件。 | 家 | 原子指标 | 无 |
| 区域医院覆盖率 | 无 | 所有经销商共用同一分母 | % | 复合口径指标 | 已合作医院数, 区域全部医院总数 |
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('form,shared', [
    ('regenerated', True), ('legacy_marker', True), ('different_definition', False),
    ('missing_dependency', False), ('unregistered_prose', False),
])
async def test_current_registered_definition_keeps_component_scope_after_regeneration(monkeypatch, form, shared):
    spec = REGISTERED
    if form == 'legacy_marker':
        spec = '[dealer_shared_region_denominator:v1]'
    elif form == 'different_definition':
        spec = spec.replace('所有经销商共用同一分母', '按经销商分别计算自己的分母')
    elif form == 'missing_dependency':
        spec = spec.replace('已合作医院数, 区域全部医院总数', '无')
    elif form == 'unregistered_prose':
        spec = '所有经销商共用同一分母；与销售订单无关；不接受经销商条件'
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source':'semantic_model_81.md', 'available':True, 'content':spec}))

    def extraction(metric, dimensions, query='是'):
        return {'意图':'统计查询' if query == '是' else '指标计算', '实体':['医院'],
            '指标':[{'name':metric}], '维度':dimensions, '过滤条件':[], '是否查询数据库':query,
            '时间粒度':{'time_range':'2025年'}}

    async def model(request):
        tasks = [
            {'question':'查询2025年各经销商的已合作医院数', 'depends_on':[],
             'extraction':extraction('已合作医院数', ['经销商'])},
            {'question':'查询2025年区域全部医院总数', 'depends_on':[],
             'extraction':extraction('区域全部医院总数', [])},
            {'question':'根据上述任务结果计算各经销商的区域医院覆盖率，不再查询数据库', 'depends_on':[0,1],
             'extraction':extraction('区域医院覆盖率', ['经销商'], '否')},
        ]
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({'tasks':tasks},ensure_ascii=False)}}]})

    planner = SingleQuestionPlanner(Settings(_env_file=None, env='test',
        intent_model_api_key=SecretStr('fixture'), multi_question_model_enabled=True), transport=httpx.MockTransport(model))
    result = await planner.plan_question('查询2025年各经销商的区域医院覆盖率', semantic_model_id=81)
    assert result.shared_region_denominator == shared
    assert result.tasks[0].structured['时间粒度']['time_range'] == '2025年'
    assert result.tasks[1].structured['时间粒度']['time_range'] == ('全部历史' if shared else '2025年')
    assert result.tasks[2].depends_on == (0, 1)
    assert result.tasks[2].structured['是否查询数据库'] == '否'
