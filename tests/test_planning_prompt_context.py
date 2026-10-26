"""Inspect the actual two-stage model messages, without calling a live model."""
import json

import pytest

from app.domain.models import PrimaryIntent
from app.planning.task_dag import SingleQuestionPlanner
from test_semantic_file_unification import install_reference, model_transport, settings


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [True, False])
async def test_current_model_document_and_intent_reach_both_stages_without_cross_model_reuse(
    monkeypatch, available,
):
    load = install_reference(
        monkeypatch, lambda _, model: f'当前模型{model}独有语义正文' if available else '')
    calls = []
    planner = SingleQuestionPlanner(settings(), transport=model_transport(calls))
    for model_id in (121, 122):
        question = f'列出模型{model_id}的当前实体'
        await planner._model_decompose(
            question, semantic_model_id=model_id,
            semantic_context='# 业务语义规范\n过期模型的实体与指标',
            root_intent=PrimaryIntent.DETAIL_QUERY,
        )
        for call in calls[-2:]:
            system = call['messages'][0]['content']
            assert f'语义模型ID：{model_id}' in system
            assert f'来源标识：semantic_model_{model_id}.md' in system
            assert '已识别任务意图：DETAIL_QUERY' in system
            assert '过期模型的实体与指标' not in system
            assert f'当前模型{243-model_id}独有语义正文' not in system
            assert system.count(f'当前模型{model_id}独有语义正文') == int(available)
            assert ('状态：正文已提供' in system) == available
            assert ('状态：正文未取得' in system) != available
            assert call['messages'][1]['content'] == question
            assert call['temperature'] == 0
            assert call['enable_thinking'] is False
        planning_system = calls[-2]['messages'][0]['content']
        schema = json.loads(planning_system.split('JSON Schema：', 1)[1])
        assert set(schema['$defs']['TaskOutline']['properties']) == {
            'question', 'name', 'depends_on', 'expected_output', 'definition_only'}
        assert schema['properties']['tasks']['minItems'] == 1
        assert schema['properties']['tasks']['maxItems'] == 12
        parameters_system = calls[-1]['messages'][0]['content']
        planned = json.loads(parameters_system.split('已规划任务JSON：\n', 1)[1])
        assert planned['original_question'] == question
    assert [args.args[1] for args in load.await_args_list] == [121, 122]
