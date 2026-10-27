"""All document consumers resolve the current model like parameter extraction."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.domain import semantic_description as sd
from app.planning.task_dag import SingleQuestionPlanner
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.services.general_rules import render_rules


def settings(**overrides):
    return Settings(_env_file=None, env='test', intent_model_api_key='fixture',
                    multi_question_model_enabled=True, **overrides)


def install_reference(monkeypatch, content):
    async def load(config, model_id):
        text = content(config, model_id)
        return {'source': f'semantic_model_{model_id}.md', 'available': bool(text), 'content': text}
    mocked = AsyncMock(side_effect=load)
    monkeypatch.setattr(sd, 'load_semantic_description', mocked)
    return mocked


def model_transport(calls):
    async def model(request):
        body = json.loads(request.content)
        calls.append(body)
        system = body['messages'][0]['content']
        if '已规划任务JSON：' in system:
            result = {'intent': 'DETAIL_QUERY', 'tasks': [{'task_index': 0, 'extraction': {
                '意图': '明细查询', '实体': ['当前实体'], '展示字段': [], '过滤条件': [],
            }}]}
        elif '你是任务规划器' in system:
            result = {'tasks': [{'question': body['messages'][1]['content']}]}
        else:
            result = {'split': False, 'intent': '明细查询', 'sub_questions': []}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    return httpx.MockTransport(model)


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [True, False])
async def test_model_document_is_only_spec_but_rules_and_prior_tasks_survive(monkeypatch, available):
    load = install_reference(monkeypatch, lambda _, model_id: f'模型{model_id}专有文件' if available else '')
    calls = []
    config = settings()
    context = ('# 业务语义规范\n其他模型的旧指标和实体\n[上一轮任务表]\n保留前序任务结果'
               + render_rules([{'description': '保留通用规则'}]))
    await SingleQuestionPlanner(config, transport=model_transport(calls))._model_decompose(
        '列出当前实体', semantic_model_id=121, semantic_context=context)
    assert len(calls) == 2
    load.assert_awaited_once_with(config, 121)
    for call in calls:
        system = call['messages'][0]['content']
        assert '其他模型的旧指标和实体' not in system
        assert ('模型121专有文件' in system) == available
        assert '保留前序任务结果' in system and '保留通用规则' in system
        assert call['messages'][1]['content'] == '列出当前实体'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['single', 'combined', 'definition'])
@pytest.mark.parametrize('model_id', [121, 122])
async def test_synthesis_reads_current_model_and_preserves_only_nonsemantic_agent_settings(monkeypatch, kind, model_id):
    from test_analysis_synthesis import request, analysis, evidence
    load = install_reference(monkeypatch, lambda _, model: f'当前模型{model}的实体和口径')
    synth = QwenAnalysisSynthesizer(settings())
    captured = []
    async def generate(system, data, **kwargs):
        captured.append((system, data))
        return '回答', SimpleNamespace()
    async def answer(system, data, **kwargs):
        captured.append((system, data))
        return '{"answer":"口径回复"}'
    monkeypatch.setattr(synth, '_generate', generate)
    monkeypatch.setattr(synth, '_request_content', answer)
    prompt = ('# 角色\n回答简洁\n## 业务语义规范\n别的模型的过时规范\n### 实体\n旧实体'
              '\n## 表达风格\n使用中文' + render_rules([{'description': '保留通用规则'}]))
    if kind == 'single':
        await synth.synthesize(request(), analysis(), evidence(), agent_prompt=prompt, semantic_model_id=model_id)
    elif kind == 'combined':
        await synth.synthesize_combined('补全的问题', [], agent_prompt=prompt, semantic_model_id=model_id)
    else:
        assert await synth.synthesize_definition('补全的问题', semantic_model_id=model_id) == '口径回复'
    load.assert_awaited_once()
    assert load.await_args.args[1] == model_id
    system, payload = captured[0]
    assert payload['semantic_reference']['content'] == f'当前模型{model_id}的实体和口径'
    assert '别的模型的过时规范' not in system and '旧实体' not in system
    if kind != 'definition':
        assert all(value in system for value in ['回答简洁', '使用中文', '保留通用规则'])


@pytest.mark.parametrize('heading', ['# 业务语义规范', '## 业务语义规范', '### 业务语义规范'])
def test_embedded_spec_removal_preserves_following_peer_sections_and_general_rules(heading):
    rules = render_rules([{'description': '保留规则'}])
    text = f'角色说明\n{heading}\n旧模型内容\n{heading.split()[0]} 输出格式\n保留格式' + rules
    clean = sd.without_embedded_semantic_spec(text)
    assert '旧模型内容' not in clean
    assert all(value in clean for value in ['角色说明', '保留格式', '保留规则'])


def test_plain_agent_instructions_are_unchanged():
    text = '你是业务助手，解释要通俗。'
    assert sd.without_embedded_semantic_spec(text) == text


def test_nested_semantic_headings_do_not_remove_following_style():
    text = '# 业务语义规范\n旧内容\n## 业务语义规范\n旧子节\n# 表达风格\n保留表达要求'
    assert sd.without_embedded_semantic_spec(text) == '# 表达风格\n保留表达要求'
