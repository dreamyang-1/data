"""Live scoped rules reach model system prompts without replacing old contracts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import AgentPromptConfig, ChatRequest
from app.services.agent_prompt_store import AgentPromptStore
from app.services.general_rules import attach_general_rules, render_rules, rule_section
from app.services.orchestrator import DataAnalysisOrchestrator
from app.planning.task_dag import SingleQuestionPlanner, extract_semantic_spec_section
from app.planning.compound_question import CompoundQuestionJudge


def row(**changes):
    return dict(id=10, semantic_model_id=81, business_domain_ids='205', start_status=1,
                description='业务说明：标准单位使用元。') | changes


@pytest.fixture
def registry():
    rows, queries, closed = [row()], [], []
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, params): queries.append((sql, params))
        def fetchall(self): return deepcopy(rows)
        def close(self): closed.append(True)
    return AgentPromptStore(Connection), rows, queries, closed


@pytest.mark.asyncio
async def test_enabled_rule_add_edit_disable_delete_is_visible_without_ttl(registry):
    store, rows, queries, closed = registry
    assert len(await store.resolve_general_rules(81, [205])) == 1
    rows.append(row(id=11, description='第二条'))
    assert len(await store.resolve_general_rules(81, [205])) == 2
    rows[0]['description'] = '编辑后的规则'
    assert (await store.resolve_general_rules(81, [205]))[0]['description'] == '编辑后的规则'
    rows[0]['start_status'] = 0
    assert [r['id'] for r in await store.resolve_general_rules(81, [205])] == ['11']
    rows.clear()
    assert await store.resolve_general_rules(81, [205]) == []
    assert len(queries) == len(closed) == 5
    assert all(sql.startswith('SELECT') and 'r.start_status=1' in sql and 'COALESCE(r.is_deleted,0)=0' in sql for sql, _ in queries)
    assert all(params == (81, 205) and 'b.id=%s' in sql and 'FIND_IN_SET' in sql and 'LIKE' not in sql for sql, params in queries)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [dict(semantic_model_id=82), dict(business_domain_ids='206'),
    dict(business_domain_ids='1205'), dict(business_domain_ids='20'), dict(business_domain_ids='-1'),
    dict(business_domain_ids=None), dict(business_domain_ids=''), dict(business_domain_ids='bad'),
    dict(business_domain_ids='[true]'), dict(start_status=0), dict(description=' ')])
async def test_foreign_disabled_or_invalid_rules_are_never_rendered(registry, changes):
    store, rows, *_ = registry
    rows[:] = [row(**changes)]
    assert await store.resolve_general_rules(81, [205]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('domains', ['205', '205,206', '[205,206]'])
async def test_exact_supported_membership_formats(registry, domains):
    store, rows, *_ = registry
    rows[:] = [row(business_domain_ids=domains)]
    assert len(await store.resolve_general_rules(81, [205])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('model,domains', [(None, []), (True, []), (0, []), (81, [205, 206]), (81, [True])])
async def test_invalid_or_unsupported_scope_never_queries(registry, model, domains):
    store, _, queries, _ = registry
    assert await store.resolve_general_rules(model, domains) == []
    assert not queries


@pytest.mark.asyncio
async def test_model_wide_rules_remain_bound_to_model(registry):
    store, _, queries, _ = registry
    assert await store.resolve_general_rules(81, [])
    sql, params = queries[0]
    assert params == (81,) and 'b.id=%s' not in sql and 'r.semantic_model_id=%s' in sql


def chat(prompt=None, message='one'):
    return ChatRequest(application_id='data-analysis', conversation_id='rules', message_id=message,
                       question='查询销售额', semantic_model_id=81, business_domain_ids=[205], prompt=prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', [None, AgentPromptConfig(user='已有指令'),
                                  AgentPromptConfig(concise_instruct='已有简洁指令', user='被简洁替代的指令')])
async def test_one_turn_snapshot_with_transport_prompt_and_fresh_next_turn(registry, prompt):
    store, rows, queries, _ = registry
    original = chat(prompt)
    resolved = await attach_general_rules(original, store)
    repeated = await attach_general_rules(resolved, store)
    assert repeated is resolved and len(queries) == 1
    text = resolved.prompt.render()
    assert '标准单位使用元' in text and '冲突部分不执行' in text
    assert not original._general_rules_loaded
    assert '_general_rule_section' not in resolved.model_dump_json()
    assert '标准单位使用元' not in resolved.model_dump_json()
    if prompt:
        assert prompt.render() in text and '标准单位使用元' not in prompt.render()
    rows.clear()
    fresh = await attach_general_rules(chat(prompt, 'two'), store)
    assert '标准单位使用元' not in fresh.prompt.render() and len(queries) == 2


@pytest.mark.asyncio
async def test_store_outage_never_reuses_old_rules_or_logs_credentials(caplog):
    class Store:
        async def resolve_general_rules(self, *args): raise OSError('fixture-private-secret')
    resolved = await attach_general_rules(chat(AgentPromptConfig(user='已有指令')), Store())
    assert '已有指令' in resolved.prompt.render()
    assert not rule_section(resolved.prompt.render())
    assert 'fixture-private-secret' not in caplog.text


@pytest.mark.asyncio
async def test_orchestrator_uses_rules_even_with_provided_prompt(registry):
    store, *_ = registry
    owner = SimpleNamespace(agent_prompt_store=store)
    resolved = await DataAnalysisOrchestrator._with_platform_agent_prompt(owner, chat(AgentPromptConfig(user='已有指令')))
    assert '标准单位使用元' in resolved.prompt.render()


@pytest.mark.asyncio
async def test_narrowed_scope_never_reuses_another_domain_rule(registry):
    store, _, queries, _ = registry
    original = await attach_general_rules(chat(AgentPromptConfig(user='已有指令')), store)
    changed = original.model_copy(deep=True, update={'business_domain_ids': [206]})
    resolved = await attach_general_rules(changed, store)
    assert '标准单位使用元' not in resolved.prompt.render()
    assert queries[-1][1] == (81, 206)


@pytest.mark.asyncio
async def test_v2_bridge_attaches_rules_before_context_resolution(registry):
    from app.semantic_v2.context_v1_execution import V2ContextV1ExecutionBridge
    captured = []
    class StopBeforeContext(Exception): pass
    class ContextStore:
        def key(self, chat, identity):
            captured.append(chat)
            raise StopBeforeContext()
    owner = object.__new__(V2ContextV1ExecutionBridge)
    owner.agent_prompt_store = registry[0]
    owner.store = ContextStore()
    with pytest.raises(StopBeforeContext):
        await owner.handle(chat(AgentPromptConfig(user='已有指令')), None)
    assert '标准单位使用元' in captured[0].prompt.render()


def test_rules_survive_missing_or_truncated_semantic_description_without_becoming_catalog():
    rules = render_rules([{'id': '10', 'description': '标准单位使用元'}])
    assert extract_semantic_spec_section('普通角色设定'+rules) == rules
    text = extract_semantic_spec_section('# 业务语义规范\n'+'x'*14000+rules)
    assert text.endswith(rules) and text.count('semantic_general_rules>') == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['single', 'compound'])
async def test_model_system_prompt_includes_rules_even_with_remote_spec(monkeypatch, kind):
    from app.domain import semantic_description
    async def remote(*args):
        return dict(source='semantic_model_81.md', available=True, content='# 业务语义规范\n指标：销售额')
    monkeypatch.setattr(semantic_description, 'load_semantic_description', remote)
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        content = json.dumps({'intent': 'METRIC_QUERY', 'extraction': {'指标': [{'name': '销售额'}]}}) if kind=='single' else json.dumps({'split':False,'sub_questions':[]})
        return httpx.Response(200, json={'choices':[{'message':{'content':content}}]})
    settings=Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('test'),multi_question_model_enabled=True)
    rules=render_rules([{'id':'10','description':'标准单位使用元'}])
    context='# 业务语义规范\n旧说明'+rules
    if kind=='single':
        await SingleQuestionPlanner(settings,transport=httpx.MockTransport(handler))._model_decompose('查询销售额',semantic_context=context,semantic_model_id=81)
    else:
        await CompoundQuestionJudge(settings,transport=httpx.MockTransport(handler))._model_judge('查询销售额',semantic_context=context,semantic_model_id=81)
    system=captured[0]['messages'][0]['content']
    assert '标准单位使用元' in system and '冲突部分不执行' in system
    assert '旧说明' not in system and '指标：销售额' in system
    assert captured[0]['messages'][1]['content']=='查询销售额'


def test_explicit_conflict_policy_does_not_replace_existing_core_contract():
    prompt=AgentPromptConfig(concise_instruct='禁止擅自补时间。')
    prompt._general_rule_section=render_rules([{'id':'10','description':'未指定时间默认最近12个月。'}])
    text=prompt.render()
    assert text.index('禁止擅自补时间') < text.index('默认最近12个月')
    assert '若已有规则禁止补充默认时间' in text and '已有指令' in text
