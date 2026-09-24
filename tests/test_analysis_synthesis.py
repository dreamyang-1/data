from __future__ import annotations

import json
import httpx
import pytest

from app.analysis.engine import AnalysisOutput
from app.analysis.synthesis import QwenAnalysisSynthesizer, SynthesisValidationError
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, EvidenceItem, MetricRef, PrimaryIntent


def request():
    return CanonicalAnalysisRequest(
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="为什么销售额下降", rewritten_question="分析上海销售额的变化",
        primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        metrics=[MetricRef(input="销售额")],
    )


def analysis():
    return AnalysisOutput(
        answer="销售额下降50。华东贡献-80，华南贡献30。",
        method="ranked_contribution_candidates",
        facts={"contribution_sum": -50, "coverage": 1.0,
               "query_data": {"rows": [{"地区": "华东", "贡献": -80}, {"地区": "华南", "贡献": 30}], "sample_only": False}},
        warnings=["贡献分析不能证明业务因果"],
    )


def evidence():
    return [
        EvidenceItem(evidence_id="query:q1", kind="QUERY_RESULT", source_ref="data:test", payload={"row_count": 2}),
        EvidenceItem(evidence_id="analysis:a1", kind="ANALYSIS_RESULT", source_ref="deterministic:test", payload={"facts": analysis().facts}),
        EvidenceItem(evidence_id="knowledge:k1", kind="ANALYSIS_KNOWLEDGE", source_ref="kb:test", payload={"text": "不可混入的外部原因"}),
    ]


def settings(**changes):
    return Settings(env="test", intent_model_api_key="test-key",
                    analysis_synthesis_enabled=True, analysis_synthesis_max_retries=0, **changes)


def transport_for(output, calls=None):
    async def handler(req):
        if calls is not None:
            calls.append(json.loads(req.content))
        content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_data_insight_uses_completed_question_data_and_platform_style_without_review():
    calls = []
    result = {"claims": [{"statement": "华南的正向贡献部分抵消华东的下降，但未改变总体下降方向。"}]}
    text, parsed = await QwenAnalysisSynthesizer(
        settings(analysis_synthesis_validation_retries=1), transport_for(result, calls)
    ).synthesize(request(), analysis(), evidence(), agent_prompt="面向医药业务人员说明")
    assert text == result["claims"][0]["statement"]
    assert len(parsed.claims) == 1 and len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    data = json.loads(calls[0]["messages"][1]["content"])
    assert data["completed_question"] == "分析上海销售额的变化"
    assert data["facts"]["query_data"]["rows"][0]["贡献"] == -80
    assert data["warnings"] == analysis().warnings
    assert "knowledge:k1" not in data["evidence"]
    assert "allowed_numbers_for_output" not in data
    assert "面向医药业务人员说明" in prompt
    assert "合理推断" in prompt and "不要补造数据" in prompt
    assert "八百至一千五百字" in prompt and "不要结论先行" in prompt
    assert "不生成图表" in prompt and "sample_only" in prompt
    assert "高级分析专家工作方法" in prompt


@pytest.mark.asyncio
async def test_data_insight_accepts_derived_calculation_not_in_numeric_allowlist():
    output = {"claims": [{"statement": "华南贡献30抵消华东下降80的37.5%，剩余净下降50。",
                          "certainty": "VERIFIED_FACT", "evidence_ids": ["query:q1"]}]}
    calls = []
    text, _ = await QwenAnalysisSynthesizer(settings(), transport_for(output, calls)).synthesize(
        request(), analysis(), evidence())
    assert "37.5%" in text and len(calls) == 1


@pytest.mark.asyncio
async def test_unlocked_analysis_and_missing_claim_metadata_do_not_block_report():
    unlocked = AnalysisOutput(answer="订单笔数为49。", method="query-summary", facts={}, warnings=[])
    text, parsed = await QwenAnalysisSynthesizer(settings(), transport_for(
        {"claims": [{"statement": "此次结果是该范围内的订单数量，没有同期对照，不能据此判断增长。"}]}
    )).synthesize(request(), unlocked, [])
    assert "订单数量" in text
    assert parsed.claims[0].evidence_ids == []


@pytest.mark.asyncio
async def test_unknown_evidence_metadata_is_dropped_without_hiding_analysis():
    text, output = await QwenAnalysisSynthesizer(settings(), transport_for({"claims": [{
        "statement": "华东下降抵消了华南的正向贡献。",
        "certainty": "VERIFIED_FACT", "evidence_ids": ["made-up:id", "analysis:a1"],
    }]})).synthesize(request(), analysis(), evidence())
    assert text and output.claims[0].evidence_ids == ["analysis:a1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    {"claims": [{"statement": "可读分析。"}]},
    {"claims": [{"statement": "可读分析。", "certainty": ["bad"], "evidence_ids": None}]},
    {"claims": ["可读分析。"]},
    {"analysis": "可读分析。"},
    {"content": "可读分析。"},
    "可读分析。",
    '"可读分析。"',
    '```json\n{"claims":[{"statement":"可读分析。"}]}\n```',
])
async def test_paragraph_format_variations_remain_displayable(output):
    text, _ = await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(
        request(), analysis(), evidence())
    assert text == "可读分析。"


@pytest.mark.asyncio
async def test_paragraph_order_length_and_missing_limitation_claim_do_not_reject_text():
    paragraphs = [
        {"statement": "先比较数据范围。", "certainty": "LIMITATION"},
        {"statement": "展开分析依据。" * 100, "certainty": "VERIFIED_FACT"},
        {"statement": "先比较数据范围。", "certainty": "LIMITATION"},
    ]
    text, parsed = await QwenAnalysisSynthesizer(settings(), transport_for(
        {"claims": paragraphs})).synthesize(request(), analysis(), evidence())
    assert len(parsed.claims) == 3
    assert text.startswith(paragraphs[0]["statement"])
    assert paragraphs[1]["statement"] in text


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " ", "{}", '{"claims":[]}', '{"claims":[{"statement":""}]}', '{"claims":'])
async def test_empty_or_broken_response_is_availability_failure_not_content_review(content):
    with pytest.raises(SynthesisValidationError):
        await QwenAnalysisSynthesizer(settings(), transport_for(content)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_missing_api_key_still_reports_service_unavailable():
    with pytest.raises(RuntimeError, match="API key"):
        await QwenAnalysisSynthesizer(settings().model_copy(update={"intent_model_api_key": None})).synthesize(
            request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_transient_http_failure_retries_transport_only():
    calls = []
    async def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "模型分析。"}}]})
    model = QwenAnalysisSynthesizer(settings().model_copy(update={"analysis_synthesis_max_retries": 1}),
                                    httpx.MockTransport(handler))
    text, _ = await model.synthesize(request(), analysis(), evidence())
    assert text == "模型分析。" and len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_permission_http_failure_is_not_retried_or_hidden():
    calls = []
    async def handler(req):
        calls.append(1)
        return httpx.Response(403)
    with pytest.raises(httpx.HTTPStatusError):
        await QwenAnalysisSynthesizer(settings(), httpx.MockTransport(handler)).synthesize(
            request(), analysis(), evidence())
    assert calls == [1]
