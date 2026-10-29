"""解释口径清单类问题（“XX域建了哪些指标和维度”）直接读语义文件作答。

冻结契约验证：意图识别完成后直接终态输出，不得进入澄清、任务规划、
ASL/SQL 及以下任何查询节点。
"""
import json

from unittest.mock import AsyncMock

from app.domain.models import PrimaryIntent, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.services.catalog_overview_answer import catalog_overview_response


IDENTITY = TrustedIdentity(tenant_id="tenant-a", user_id="user-a")


def request(**updates):
    from app.domain.models import CanonicalAnalysisRequest
    return CanonicalAnalysisRequest(
        semantic_model_id=130, conversation_id="catalog-overview",
        tenant_id="t", user_id="u", application_id="app",
        original_question="医药销售域建了哪些指标和维度",
        primary_intent=PrimaryIntent.SEMANTIC_CATALOG_QUERY, **updates)


def overview():
    return {
        "model_name": "医药销售语义模型",
        "domains": [{"id": 205, "name": "医药销售域", "metrics": [
            {"name": "含税销售总额", "description": "含税销售额合计"},
            {"name": "订单笔数", "description": ""},
        ]}],
        "shared_metrics": [],
        "dimensions": [
            {"name": "区域", "description": ""},
            {"name": "经销商", "description": ""},
        ],
    }


def test_catalog_question_classifies_to_semantic_catalog_query():
    result = RuleBasedIntentClassifier().classify(
        "医药销售域建了哪些指标和维度", IDENTITY, "c-catalog"
    )
    assert result.primary_intent == PrimaryIntent.SEMANTIC_CATALOG_QUERY
    assert "metric" not in result.missing_slots


def test_catalog_question_missing_slots_stay_empty_for_time_free_variant():
    result = RuleBasedIntentClassifier().classify(
        "医药销售域配置了哪些指标和维度", IDENTITY, "c-catalog-2"
    )
    assert result.primary_intent == PrimaryIntent.SEMANTIC_CATALOG_QUERY
    assert result.missing_slots == []


def test_existing_intents_are_not_rerouted():
    cases = {
        "有哪些指标": PrimaryIntent.CAPABILITY_HELP,
        "销售额的口径是什么？": PrimaryIntent.METRIC_DEFINITION,
        "含税销售总额最近一年按月的趋势": PrimaryIntent.TREND_ANALYSIS,
        "上海销售额最高的经销商是哪家": PrimaryIntent.METRIC_QUERY,
    }
    for question, expected in cases.items():
        result = RuleBasedIntentClassifier().classify(question, IDENTITY, "c-reg")
        assert result.primary_intent == expected, question


def test_catalog_answer_lists_registered_metrics_and_dimensions():
    response = catalog_overview_response(request(), overview())
    assert response.status == "COMPLETED"
    assert "含税销售总额" in response.answer and "订单笔数" in response.answer
    assert "区域" in response.answer and "经销商" in response.answer
    assert "医药销售域" in response.answer
    assert response.analysis_plan is None and response.dataset_id is None
    assert [step.stage for step in response.analysis_process] == ["KNOWLEDGE_RETRIEVAL"]
    assert all(item.kind == "SEMANTIC_CATALOG" for item in response.evidence)


def test_catalog_answer_filters_to_domain_named_in_question():
    data = overview()
    data["domains"].append({"id": 206, "name": "设备租赁域", "metrics": [
        {"name": "租赁收入", "description": ""}]})
    response = catalog_overview_response(request(), data)
    assert "医药销售域" in response.answer and "含税销售总额" in response.answer
    assert "租赁收入" not in response.answer


def test_catalog_answer_discloses_empty_registry_instead_of_inventing():
    data = overview()
    data["domains"][0]["metrics"] = []
    data["dimensions"] = []
    response = catalog_overview_response(request(), data)
    assert response.status == "PARTIAL_SUCCESS"
    assert "尚未登记" in response.answer


def test_stream_catalog_question_skips_all_query_stages(monkeypatch):
    from test_api import TestClient, build_test_app
    app = build_test_app()
    with TestClient(app) as client:
        agent = app.state.container.orchestrator
        agent._catalog_overview_snapshot = lambda semantic_model_id: overview()
        query = agent.adapters.query.query = AsyncMock(
            side_effect=AssertionError("catalog overview must not execute SQL"))
        recovery = agent._recover_live_published_metrics = AsyncMock(
            side_effect=AssertionError("catalog overview must not recover SQL metrics"))
        response = client.post("/agent_chat/stream", json={
            "semantic_model_id": 130, "application_id": "app",
            "conversation_id": "catalog-stream", "message_id": "m1",
            "question": "医药销售域建了哪些指标和维度"})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    complete = next(event for event in events if event.get("type") == "complete")
    assert complete["status"] == "COMPLETED"
    assert complete["intent"] == "SEMANTIC_CATALOG_QUERY"
    stages = {
        event.get("meta", {}).get("stage") for event in events
        if event.get("type") == "message_chunk"
    }
    assert not {
        "ASL_GENERATION", "SQL_TRANSLATION", "SQL_EXECUTION", "DATA_RETRIEVAL",
        "RELIABILITY_CHECK", "INSIGHT_ANALYSIS",
    }.intersection(stages)
    assert "FINAL_OUTPUT" in stages
    assert not complete["analysis_plan"]
    assert "含税销售总额" in complete["answer"]
    query.assert_not_awaited()
    recovery.assert_not_awaited()
