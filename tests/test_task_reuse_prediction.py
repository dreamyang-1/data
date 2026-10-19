"""任务级复用预判的专项回归：规划展示前用同一指纹口径预判无依赖任务
能否复用上一轮数据集，命中才在任务名上加"复用上一轮查询结果"前缀。"""
from types import SimpleNamespace

import pytest

from app.domain.models import (
    AtomicTask,
    CanonicalAnalysisRequest,
    ChatRequest,
    MetricRef,
    PrimaryIntent,
    TaskPlan,
    TrustedIdentity,
)
from app.adapters import build_mock_adapters
from app.config import Settings
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.authorized_scope import bind_authorized_scope
from app.stores import InMemorySessionStore


def base_request(question: str, metric: str) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="conversation",
        application_id="app",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input=metric)],
        semantic_model_id=81,
    )


def build_agent() -> DataAnalysisOrchestrator:
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )
    # 数据集存储开关只作真值判断，指纹比对全部走桩件
    agent.dataset_store = SimpleNamespace()
    return agent


def plan_with_tasks() -> TaskPlan:
    return TaskPlan(
        planner="DETERMINISTIC_RULE",
        tasks=[
            AtomicTask(
                task_id="task-1",
                question="统计2025年10月上海地区含税销售总额排名前三的经销商",
                primary_intent=PrimaryIntent.METRIC_QUERY,
                extraction={"指标": ["含税销售总额"]},
            ),
            AtomicTask(
                task_id="task-2",
                question="查询这三家经销商的主要销售产品",
                depends_on=["task-1"],
                primary_intent=PrimaryIntent.DETAIL_QUERY,
                extraction={"实体": ["经销商"]},
            ),
        ],
    )


def build_chat() -> ChatRequest:
    return ChatRequest(
        semantic_model_id=81, application_id="app",
        conversation_id="conv", message_id="m1",
        question="这三家每家主要销售什么产品？",
    )


def persistable_reference(agent, request, chat) -> dict:
    """生产落盘的引用带 scope 指纹：预判的范围校验按同一口径比对。"""
    return {
        "dataset_id": "ds-1",
        "row_count": 1,
        "semantic_model_id": 81,
        "business_domain_ids": [],
        "scope": {
            "authorized_semantic_scope_fingerprint": (
                chat.authorized_semantic_scope.fingerprint()
            ),
        },
        "transformation_log": [
            {"type": "query_provenance",
             "pre_asl_query_fingerprint": agent._query_fingerprint(request)},
        ],
    }


def stub_session_references(agent, reference: dict) -> None:
    async def fake_references(*args, **kwargs):
        return [reference]

    agent.sessions = SimpleNamespace(
        get_recent_dataset_references=fake_references
    )


@pytest.mark.asyncio
async def test_prediction_hits_matching_persisted_fingerprint():
    agent = build_agent()
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    chat = build_chat()

    async def fake_classify(question, identity, conversation_id, *,
                            pre_resolved=False, agent_prompt="",
                            planner_extraction=None):
        return base_request(question, "含税销售总额")

    agent._classify = fake_classify
    request = await fake_classify(
        "统计2025年10月上海地区含税销售总额排名前三的经销商", identity, "conv"
    )
    bind_authorized_scope(request, chat.authorized_semantic_scope)
    stub_session_references(
        agent, persistable_reference(agent, request, chat)
    )

    predictions = await agent._predict_task_dataset_reuse(
        chat, identity, plan_with_tasks()
    )
    # 只有无依赖的任务参与预判，依赖任务不命中
    assert predictions == {"task-1": True}


@pytest.mark.asyncio
async def test_prediction_skips_scope_mismatch():
    agent = build_agent()
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    chat = build_chat()

    async def fake_classify(question, identity, conversation_id, *,
                            pre_resolved=False, agent_prompt="",
                            planner_extraction=None):
        return base_request(question, "含税销售总额")

    agent._classify = fake_classify
    agent._dataset_reference_matches_scope = lambda item, req: False
    reference = {
        "dataset_id": "ds-1",
        "semantic_model_id": 81,
        "business_domain_ids": [],
        "scope": {},
        "transformation_log": [
            {"type": "query_provenance", "pre_asl_query_fingerprint": "any"},
        ],
    }
    stub_session_references(agent, reference)

    predictions = await agent._predict_task_dataset_reuse(
        chat, identity, plan_with_tasks()
    )
    assert predictions == {}


@pytest.mark.asyncio
async def test_prediction_skips_tasks_without_extraction_or_with_dependencies():
    agent = build_agent()
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    chat = build_chat()
    reference = {
        "dataset_id": "ds-1",
        "semantic_model_id": 81,
        "business_domain_ids": [],
        "scope": {},
        "transformation_log": [
            {"type": "query_provenance", "pre_asl_query_fingerprint": "any"},
        ],
    }
    stub_session_references(agent, reference)
    plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        tasks=[
            # 没有意图、参数和提取的任务：执行期要走识别模型，指纹无法同源预判
            AtomicTask(task_id="task-1", question="统计一下销量"),
            AtomicTask(
                task_id="task-2", question="查询明细",
                depends_on=["task-1"], primary_intent=PrimaryIntent.DETAIL_QUERY,
                extraction={"实体": ["经销商"]},
            ),
        ],
    )
    predictions = await agent._predict_task_dataset_reuse(chat, identity, plan)
    assert predictions == {}
