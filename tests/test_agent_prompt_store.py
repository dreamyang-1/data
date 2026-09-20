import pytest

from app.services.agent_prompt_store import AgentPromptStore
from app.domain.models import ChatRequest
from app.services.orchestrator import DataAnalysisOrchestrator


class _Cursor:
    def __init__(self, agents):
        self.agents = agents
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, _parameters):
        self.query = query

    def fetchone(self):
        if "WHERE agent_code" in self.query:
            return None
        if "WHERE agent_id" in self.query:
            return {
                "instruction_type": 1,
                "role_setting": "国药数据分析助手",
                "background": "使用平台配置的业务背景",
            }
        return None

    def fetchall(self):
        return self.agents


class _Connection:
    def __init__(self, agents):
        self.cursor_value = _Cursor(agents)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_prompt_store_resolves_unique_published_agent_by_semantic_model():
    connection = _Connection([{"id": 81, "agent_code": "A_PLATFORM"}])
    store = AgentPromptStore(lambda: connection, cache_ttl_seconds=0)

    prompt = await store.resolve("data-analysis", semantic_model_id=81)

    assert prompt == {
        "user": "国药数据分析助手",
        "Aagent_background": "使用平台配置的业务背景",
        "concise_instruct": "",
    }
    assert connection.closed is True


@pytest.mark.asyncio
async def test_prompt_store_does_not_guess_when_semantic_model_has_two_agents():
    connection = _Connection([
        {"id": 1, "agent_code": "A_ONE"},
        {"id": 2, "agent_code": "A_TWO"},
    ])
    store = AgentPromptStore(lambda: connection, cache_ttl_seconds=0)

    assert await store.resolve("data-analysis", semantic_model_id=81) is None
    assert connection.closed is True


@pytest.mark.asyncio
async def test_orchestrator_freezes_one_platform_prompt_snapshot_per_turn():
    class Store:
        def __init__(self):
            self.calls = 0

        async def resolve(self, application_id, *, semantic_model_id=None):
            self.calls += 1
            assert application_id == "data-analysis"
            assert semantic_model_id == 81
            return {
                "user": "平台角色设定",
                "Aagent_background": "平台业务背景",
                "concise_instruct": "",
            }

    store = Store()
    owner = type("Owner", (), {"agent_prompt_store": store})()
    chat = ChatRequest(
        conversation_id="prompt-snapshot",
        message_id="message-1",
        question="查询销售额",
        application_id="data-analysis",
        semantic_model_id=81,
    )

    resolved = await DataAnalysisOrchestrator._with_platform_agent_prompt(
        owner, chat
    )
    repeated = await DataAnalysisOrchestrator._with_platform_agent_prompt(
        owner, resolved
    )

    assert resolved.prompt is not None
    assert "平台角色设定" in resolved.prompt.render()
    assert "平台业务背景" in resolved.prompt.render()
    assert repeated is resolved
    assert store.calls == 1
