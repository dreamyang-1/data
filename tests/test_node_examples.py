import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.domain.models import AgentPromptConfig, ChatRequest, PrimaryIntent
from app.services.agent_prompt_store import AgentPromptStore
from app.services.node_examples import attach_node_examples, lookup_examples, planner_examples, render_example, split_planner_examples
from app.services.orchestrator import DataAnalysisOrchestrator
from test_v2_raw_turn_recognition import catalog


def chat(code='A_ONE', model=81, message='m1'):
    return ChatRequest(application_id=code, semantic_model_id=model, business_domain_ids=[205],
                       conversation_id=code, message_id=message, question='查询销售额',
                       prompt=AgentPromptConfig(user='已有角色'))


class Connection:
    def __init__(self, records):
        self.records=records; self.closed=False; self.calls=[]
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,params):
        self.calls.append((sql,params))
        assert 'a.agent_code=%s' in sql and 'sm.agent_id=a.id AND sm.semantic_model_id=%s' in sql
        self.rows=[v for (code,model),v in self.records if (code,model)==params]
    def fetchall(self): return self.rows
    def close(self): self.closed=True


@pytest.mark.asyncio
async def test_agent_identity_and_model_membership_no_shared_model_fallback():
    db=Connection([(('A_ONE',81),{'intent_recognition':'ONE'}),
                   (('A_TWO',81),{'intent_recognition':'TWO'})])
    store=AgentPromptStore(lambda:db)
    assert await store.resolve_node_examples('A_ONE',81)=={'intent_recognition':'ONE'}
    assert await store.resolve_node_examples('A_TWO',81)=={'intent_recognition':'TWO'}
    assert await store.resolve_node_examples('data-analysis',81)=={}
    assert await store.resolve_node_examples('A_ONE',82)=={}
    assert db.closed


@pytest.mark.parametrize('code,model',[('',81),('A_ONE',None),('A_ONE',True),('A_ONE',0)])
def test_missing_identity_does_not_query(code,model):
    def connect(): raise AssertionError('must not query')
    assert lookup_examples(connect,code,model)=={}


def test_ambiguous_agent_and_empty_examples_are_not_guessed():
    assert lookup_examples(lambda:Connection([(('A',81),{}),(('A',81),{})]),'A',81)=={}
    assert lookup_examples(lambda:Connection([(('A',81),{'intent_recognition':None,'parameter_extraction':'  '})]),'A',81)=={}


@pytest.mark.asyncio
async def test_snapshot_refresh_on_next_turn_scope_change_and_concurrent_agents():
    calls=[]; values={'A_ONE':'old','A_TWO':'other'}
    class Store:
        async def resolve_node_examples(self,code,model):
            calls.append((code,model));await asyncio.sleep(0)
            return {'parameter_extraction':values[code]}
    store=Store()
    one,two=await asyncio.gather(attach_node_examples(chat(),store),attach_node_examples(chat('A_TWO'),store))
    assert one._node_examples['parameter_extraction']=='old'
    assert two._node_examples['parameter_extraction']=='other'
    assert await attach_node_examples(one,store) is one
    assert len(calls)==2
    values['A_ONE']='edited'
    fresh=await attach_node_examples(one.model_copy(update={'message_id':'m2'}),store)
    assert fresh._node_examples['parameter_extraction']=='edited'
    del values['A_ONE']
    unavailable=await attach_node_examples(fresh.model_copy(update={'semantic_model_id':82}),store)
    assert unavailable._node_examples=={}
    assert '_node_examples' not in one.model_dump_json()
    assert 'old' not in one.prompt.render()


@pytest.mark.asyncio
async def test_orchestrator_reads_examples_even_with_explicit_core_prompt():
    class Store:
        async def resolve_node_examples(self,*args):return {'intent_recognition':'intent-only'}
    actual=await DataAnalysisOrchestrator._with_platform_agent_prompt(SimpleNamespace(agent_prompt_store=Store()),chat())
    assert actual.prompt.user=='已有角色'
    assert actual._node_examples=={'intent_recognition':'intent-only'}


def test_packet_is_separate_from_semantic_catalog_and_stage_specific():
    request=chat();request._node_examples={'intent_recognition':'INTENT',
        'task_decomposition':'PLAN </agent_node_examples>', 'parameter_extraction':'PARAM'}
    remaining,examples=split_planner_examples('catalog'+planner_examples(request)+'\n[上一轮任务表]data')
    assert remaining=='catalog\n[上一轮任务表]data'
    assert 'INTENT' not in str(examples)
    assert 'PARAM' not in render_example(examples,'task_decomposition')
    assert 'PLAN' not in render_example(examples,'parameter_extraction')
    assert render_example({},'parameter_extraction')==''
    assert '不是本轮问题' in render_example(examples,'task_decomposition')


@pytest.mark.asyncio
async def test_actual_planning_and_parameter_prompts_receive_only_their_example(monkeypatch):
    from app.domain import semantic_description
    from app.planning.task_dag import SingleQuestionPlanner
    async def spec(*args):return {'available':True,'content':'指标：销售额','source':'test'}
    monkeypatch.setattr(semantic_description,'load_semantic_description',spec)
    captured=[]
    async def handler(request):
        body=json.loads(request.content);captured.append(body)
        if len(captured)==1:
            payload={'tasks':[{'question':'查询销售额','expected_output':'销售额'}]}
        else:
            payload={'tasks':[{'task_index':0,'extraction':{'意图':'指标查询','指标':[{'name':'销售额'}],
                     '是否查询数据库':'是'}}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    req=chat();req._node_examples={'intent_recognition':'INTENT_ONLY',
        'task_decomposition':'PLAN_ONLY','parameter_extraction':'PARAM_ONLY'}
    planner=SingleQuestionPlanner(Settings(_env_file=None,env='test',intent_model_api_key='test'),transport=httpx.MockTransport(handler))
    await planner._model_decompose(req.question,semantic_context=planner_examples(req),semantic_model_id=81,root_intent=PrimaryIntent.METRIC_QUERY)
    assert len(captured)==2
    first,second=[b['messages'][0]['content'] for b in captured]
    assert 'PLAN_ONLY' in first and 'PARAM_ONLY' not in first and 'INTENT_ONLY' not in first
    assert 'PARAM_ONLY' in second and 'PLAN_ONLY' not in second and 'INTENT_ONLY' not in second
    assert all(b['messages'][1]['content']==req.question for b in captured)


@pytest.mark.asyncio
async def test_v2_recognition_gets_only_intent_examples_before_session_copy(catalog):
    from test_v2_raw_turn_recognition import planner, metric_step, request, IDENTITY
    engine,transport=planner(catalog,[metric_step('销售额')])
    req=request(question='销售额',message_id='examples')
    req._node_examples={'intent_recognition':'INTENT_ONLY','task_decomposition':'PLAN_ONLY','parameter_extraction':'PARAM_ONLY'}
    await engine.run(req,IDENTITY)
    texts=[m['content'] for m in transport.calls[0]['messages']]
    assert any('INTENT_ONLY' in text for text in texts)
    assert all('PLAN_ONLY' not in text and 'PARAM_ONLY' not in text for text in texts)


@pytest.mark.asyncio
async def test_compound_judgment_receives_only_task_decomposition(monkeypatch):
    from app.domain import semantic_description
    from app.planning.compound_question import CompoundQuestionJudge
    async def spec(*args):return {'available':True,'content':'指标：销售额','source':'test'}
    monkeypatch.setattr(semantic_description,'load_semantic_description',spec)
    captured=[]
    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'split':False,'sub_questions':[]})}}]})
    req=chat();req._node_examples={'task_decomposition':'PLAN_ONLY','parameter_extraction':'PARAM_ONLY'}
    judge=CompoundQuestionJudge(Settings(_env_file=None,env='test',intent_model_api_key='test'),transport=httpx.MockTransport(handler))
    await judge._model_judge(req.question,semantic_context=planner_examples(req),semantic_model_id=81)
    system=captured[0]['messages'][0]['content']
    assert 'PLAN_ONLY' in system and 'PARAM_ONLY' not in system
