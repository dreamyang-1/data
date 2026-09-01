import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import AnalysisOperator, PrimaryIntent, TrustedIdentity
from app.intent import HybridIntentClassifier, StructuredIntentModelClient


def settings(**updates) -> Settings:
    values = {
        "env": "test",
        "adapter_mode": "mock",
        "intent_model_enabled": True,
        "intent_model_base_url": "https://model.test/v1",
        "intent_model_api_key": SecretStr("test-key"),
        "intent_model_name": "test-model",
        "intent_model_response_format": "json_schema",
    }
    values.update(updates)
    return Settings(**values)


def model_response(output: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]},
    )


@pytest.mark.asyncio
async def test_model_detail_label_cannot_drop_metric_from_grouped_partner_list():
    output = {
        "primary_intent": "DETAIL_QUERY",
        "secondary_intents": [],
        "operators": ["FILTER", "RENDER_TABLE"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.96,
        "evidence": ["经销商清单"],
        "metrics": ["销售总额"],
        "dimensions": ["经销商", "品牌"],
        "entity": "经销商",
        "fields": [],
        "comparison_type": None,
        "ambiguities": [],
    }

    async def handler(_: httpx.Request) -> httpx.Response:
        return model_response(output)

    configured = settings()
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(
            configured, httpx.MockTransport(handler)
        ),
    )
    result = await classifier.classify(
        "查询上海市江苏苏云品牌低值耗材的经销商清单，"
        "并附带经销商对应销售总额",
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        "c-grouped-partner",
    )

    assert result.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in result.metrics] == ["销售总额"]
    assert result.dimensions == ["经销商"]
    assert result.risk_level == "MEDIUM"
    assert result.filters == [
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {"field": "品牌名称", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品分类", "operator": "EQ", "value": "低值耗材"},
    ]


@pytest.mark.asyncio
async def test_model_handles_implicit_intent_without_strong_rule_signal():
    output = {
        "primary_intent": "ANOMALY_ANALYSIS",
        "secondary_intents": [],
        "operators": ["ANOMALY_DETECT"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.96,
        "evidence": ["情况不太对"],
        "metrics": [],
        "dimensions": [],
        "entity": None,
        "fields": [],
        "comparison_type": None,
        "ambiguities": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["response_format"]["json_schema"]["strict"] is True
        assert request.headers["authorization"] == "Bearer test-key"
        return model_response(output)

    client = StructuredIntentModelClient(settings(), httpx.MockTransport(handler))
    classifier = HybridIntentClassifier(settings(), model_client=client)
    result = await classifier.classify(
        "最近销售情况不太对，帮我看看",
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        "c1",
    )
    assert result.primary_intent == PrimaryIntent.ANOMALY_ANALYSIS
    assert result.intent_source == "STRUCTURED_MODEL"
    assert result.intent_confidence == 0.96


@pytest.mark.asyncio
async def test_low_confidence_model_falls_back_to_rules():
    output = {
        "primary_intent": "ANOMALY_ANALYSIS",
        "secondary_intents": [],
        "operators": ["ANOMALY_DETECT"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.55,
        "evidence": [],
        "metrics": ["销售额"],
        "dimensions": [],
        "entity": None,
        "fields": [],
        "comparison_type": None,
        "ambiguities": ["用户未说明异常含义"],
    }

    async def handler(_: httpx.Request) -> httpx.Response:
        return model_response(output)

    configured = settings(intent_model_min_confidence=0.8)
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(configured, httpx.MockTransport(handler)),
    )
    result = await classifier.classify(
        "最近业务情况怎么样", TrustedIdentity(tenant_id="t1", user_id="u1"), "c1"
    )
    assert result.primary_intent == PrimaryIntent.METRIC_QUERY
    assert "LOW_MODEL_CONFIDENCE_RULE_FALLBACK" in result.assumptions


@pytest.mark.asyncio
async def test_invalid_model_payload_fails_closed_to_rules():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    configured = settings()
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(configured, httpx.MockTransport(handler)),
    )
    result = await classifier.classify(
        "帮我看看最近情况", TrustedIdentity(tenant_id="t1", user_id="u1"), "c1"
    )
    assert result.primary_intent == PrimaryIntent.METRIC_QUERY
    assert "STRUCTURED_MODEL_UNAVAILABLE_RULE_FALLBACK" in result.assumptions


@pytest.mark.asyncio
async def test_strong_comparison_rule_vetoes_model_misclassification():
    output = {
        "primary_intent": "METRIC_QUERY",
        "secondary_intents": [],
        "operators": ["AGGREGATE"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.95,
        "evidence": ["销售额"],
        "metrics": ["销售额"],
        "dimensions": [],
        "entity": None,
        "fields": [],
        "comparison_type": None,
        "ambiguities": [],
    }

    async def handler(_: httpx.Request) -> httpx.Response:
        return model_response(output)

    configured = settings()
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(configured, httpx.MockTransport(handler)),
    )
    result = await classifier.classify(
        "本月销售额同比增长率是多少",
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        "c1",
    )
    assert result.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert "MODEL_CONFLICT_STRONG_RULE_FALLBACK" in result.assumptions


@pytest.mark.asyncio
async def test_strong_business_rule_still_uses_model_for_completion_and_entity_extraction():
    output = {
        "primary_intent": "TREND_ANALYSIS",
        "secondary_intents": [],
        "operators": ["AGGREGATE", "TIME_BUCKET"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.97,
        "evidence": ["按月", "销售趋势"],
        "metrics": ["销售额"],
        "dimensions": ["产品"],
        "entity": "产品",
        "fields": [],
        "comparison_type": None,
        "ambiguities": [],
        "completed_question": "按月分析外周插管中心静脉导管产品的销售额趋势。",
    }
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return model_response(output)

    configured = settings()
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(
            configured, httpx.MockTransport(handler)
        ),
    )
    result = await classifier.classify(
        "按月分析外周插管中心静脉导管的销售趋势。",
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        "c-model-semantics",
    )

    assert len(calls) == 1
    assert result.intent_source == "STRUCTURED_MODEL"
    assert result.entity == "产品"
    assert result.dimensions == ["产品"]
    assert result.rewritten_question == output["completed_question"]
    assert "MODEL_ENTITY_EXTRACTION_APPLIED" in result.assumptions
    assert "MODEL_QUESTION_COMPLETION_APPLIED" in result.assumptions
    assert "STRONG_RULE_MODEL_SKIPPED" not in result.assumptions


def test_model_completion_guard_rejects_lost_current_numbers_and_sql():
    source = (
        "11月较10月下降多少\n"
        "已确认的上一轮上下文（当前问题明确内容优先）：指标=销售额"
    )

    assert HybridIntentClassifier._safe_completed_question(
        "比较11月和10月销售额下降多少", source
    ) == "比较11月和10月销售额下降多少"
    assert HybridIntentClassifier._safe_completed_question(
        "分析11月销售额下降多少", source
    ) is None
    assert HybridIntentClassifier._safe_completed_question(
        "SELECT * FROM sales_order", source
    ) is None


@pytest.mark.asyncio
async def test_ranked_partner_shape_restores_sort_after_model_operator_overwrite():
    output = {
        "primary_intent": "COMPARISON_ANALYSIS",
        "secondary_intents": [],
        "operators": ["COMPARE"],
        "conversation_control": "NEW_REQUEST",
        "confidence": 0.95,
        "evidence": ["按整体业务规模排序"],
        "metrics": ["整体业务规模"],
        "dimensions": ["经销商"],
        "entity": None,
        "fields": [],
        "comparison_type": None,
        "ambiguities": [],
    }

    async def handler(_: httpx.Request) -> httpx.Response:
        return model_response(output)

    configured = settings()
    classifier = HybridIntentClassifier(
        configured,
        model_client=StructuredIntentModelClient(
            configured, httpx.MockTransport(handler)
        ),
    )
    result = await classifier.classify(
        "请列出上海市医用外科口罩产品的经销商名单，并按整体业务规模排序",
        TrustedIdentity(tenant_id="t1", user_id="u1"),
        "c-ranked-partner",
    )

    assert result.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert result.comparison_type == "对象间比较"
    assert AnalysisOperator.GROUP_BY in result.operators
    assert AnalysisOperator.SORT in result.operators
