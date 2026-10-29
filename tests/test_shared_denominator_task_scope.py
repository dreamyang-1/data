import pytest
from unittest.mock import AsyncMock
from pydantic import SecretStr

from app.config import Settings
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner


@pytest.mark.parametrize('enabled,explicit_region_group,repair', [
    (True, False, True), (False, False, False), (True, True, False),
])
@pytest.mark.asyncio
async def test_shared_denominator_keeps_region_filters_without_dealer_group_or_join(
        monkeypatch, enabled, explicit_region_group, repair):
    region = {'entity': '医院', 'field': '城市', 'op': '=', 'value': ['上海']}
    dealer = {'entity': '经销商', 'field': '经销商名称', 'op': '=', 'value': ['已确认经销商']}
    structured = {'指标': [{'name': '区域全部医院总数'}], '维度': ['省份', '城市'],
        '实体': ['医院', '经销商', '销售订单'], '过滤条件': [region, dealer],
        '展示字段': [], '时间粒度': {'time_range': '2025年'}, '是否查询数据库': '是'}
    original = QuestionDecomposition(shared_region_denominator=enabled, tasks=(
        DecomposedTask(question='根据上一任务查询各经销商所在省市的区域全部医院总数',
                       depends_on=(0,), structured=structured),))
    source = '查询2025年上海各经销商的区域医院覆盖率' + ('，按城市分别计算' if explicit_region_group else '')
    planner = SingleQuestionPlanner(Settings(_env_file=None, env='test',
        intent_model_api_key=SecretStr('fixture'), multi_question_model_enabled=True))
    monkeypatch.setattr(planner, '_model_decompose', AsyncMock(return_value=original))
    result = await planner.plan_question(source, semantic_model_id=81)
    task = result.tasks[0]
    assert task.depends_on == (0,)
    if repair:
        assert task.structured['时间粒度']['time_range'] == '全部历史'
        assert task.structured['维度'] == []
        assert task.structured['实体'] == ['医院']
        assert task.structured['过滤条件'] == [region]
        assert '各经销商所在省市' not in task.question
        assert '上海' in task.question and '区域全部医院总数' in task.question
    else:
        assert task.structured['时间粒度']['time_range'] == '2025年'
        assert result == original
    assert original.tasks[0].structured['过滤条件'] == [region, dealer]
