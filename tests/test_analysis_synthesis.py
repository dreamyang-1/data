from __future__ import annotations

import json
import httpx
import pytest

from app.analysis.engine import AnalysisOutput
from app.analysis.synthesis import QwenAnalysisSynthesizer, SynthesisValidationError
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, EvidenceItem, MetricRef, PrimaryIntent


def request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="为什么销售额下降",
        primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        metrics=[MetricRef(input="销售额")],
    )


def analysis() -> AnalysisOutput:
    return AnalysisOutput(
        answer="销售额下降50。华东贡献-80，华南贡献30，覆盖度100%。",
        method="ranked_contribution_candidates",
        facts={
            "contribution_sum": -50, "coverage": 1.0,
            "causality_established": False,
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "algorithm_contract_version": "analysis-contract-v2",
            "matched_knowledge": [{"content": "促销结束可能影响销量"}],
            "ranked_candidates": [
                {"label": "华东", "contribution": -80},
                {"label": "华南", "contribution": 30},
            ],
        },
        warnings=["归因结果是贡献驱动而非因果证明"],
    )


def evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(evidence_id="query:q1", kind="QUERY_RESULT", source_ref="data:test", payload={"row_count": 2}),
        EvidenceItem(evidence_id="analysis:a1", kind="ANALYSIS_RESULT", source_ref="deterministic:test", payload={"facts": analysis().facts}),
        EvidenceItem(evidence_id="knowledge:k1", kind="ANALYSIS_KNOWLEDGE", source_ref="kb:test", payload={"sources": [{"source": "运营记录"}]}),
    ]


def transport_for(output: dict) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})
    return httpx.MockTransport(handler)


def capturing_transport(output: dict, captured: dict) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(json.loads(body["messages"][1]["content"]))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(output)}}]},
        )

    return httpx.MockTransport(handler)


def sequence_transport(outputs: list[dict], call_count: list[int]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        index = call_count[0]
        call_count[0] += 1
        output = outputs[min(index, len(outputs) - 1)]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(output)}}]},
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_data_insight_model_loads_platform_user_prompt_into_system_message() -> None:
    captured = {}
    output = {"claims": [
        {
            "statement": "销售额下降50，华东贡献-80，华南贡献30。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
        {
            "statement": "当前归因不足以证明因果关系。",
            "certainty": "LIMITATION",
            "evidence_ids": ["analysis:a1"],
        },
    ]}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["system"] = body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(output)}}]},
        )

    await QwenAnalysisSynthesizer(
        settings(), httpx.MockTransport(handler)
    ).synthesize(
        request(),
        analysis(),
        evidence(),
        agent_prompt="平台表达设定：面向医药业务人员说明。",
    )

    assert "智能体用户设定（平台配置" in captured["system"]
    assert "平台表达设定：面向医药业务人员说明。" in captured["system"]
    assert "高级分析专家工作方法" in captured["system"]
    assert "第一条说明本次观察的指标" in captured["system"]
    assert "只解释输入已经验证的计算结果" in captured["system"]
    assert "不要结论先行" in captured["system"]
    assert "贡献最大不等于业务根因" in captured["system"]
    assert "多维、趋势、排名或归因任务" in captured["system"]
    assert "最终仍只输出符合 Schema 的 JSON claims" in captured["system"]


def settings() -> Settings:
    return Settings(env="test", intent_model_api_key="test-key", analysis_synthesis_enabled=True, analysis_synthesis_max_retries=0)


@pytest.mark.asyncio
async def test_qwen_accepts_algorithm_selected_grounded_claims() -> None:
    output = {"claims": [
        {"statement": "销售额下降50，华东贡献-80，华南贡献30。", "certainty": "VERIFIED_FACT", "evidence_ids": ["query:q1", "analysis:a1"]},
        {"statement": "促销结束可能是候选原因，尚未验证。", "certainty": "SUPPORTED_HYPOTHESIS", "evidence_ids": ["knowledge:k1"]},
        {"statement": "当前归因不足以证明因果关系。", "certainty": "LIMITATION", "evidence_ids": ["analysis:a1"]},
    ]}
    answer, parsed = await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())
    assert len(parsed.claims) == 3
    assert "从本次查询结果来看" in answer
    assert "结合已有业务资料" in answer
    assert "需要注意的是" in answer


@pytest.mark.asyncio
async def test_qwen_rejects_invented_number() -> None:
    output = {"claims": [{"statement": "销售额下降999。", "certainty": "VERIFIED_FACT", "evidence_ids": ["analysis:a1"]}]}
    with pytest.raises(SynthesisValidationError, match="ungrounded number"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_qwen_may_repeat_explicit_time_number_from_user_question() -> None:
    scoped_request = request().model_copy(
        update={"original_question": "分析2026年销售额变化"}
    )
    output = {"claims": [
        {
            "statement": "2026年销售额下降50。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
        {
            "statement": "当前归因不足以证明因果关系。",
            "certainty": "LIMITATION",
            "evidence_ids": ["analysis:a1"],
        },
    ]}

    answer, _ = await QwenAnalysisSynthesizer(
        settings(), transport_for(output)
    ).synthesize(scoped_request, analysis(), evidence())

    assert "2026年销售额下降50" in answer


@pytest.mark.asyncio
async def test_query_summary_accepts_natural_grounded_paraphrase() -> None:
    query_summary = AnalysisOutput(
        answer=(
            "本次查询共命中1条结果，返回字段为订单笔数。\n"
            "本次返回的是完整查询结果，没有发生结果截断。\n"
            "这次结果的核心值是订单笔数为49。"
        ),
        method="validated_query_result_summary",
        facts={
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "row_count": 1,
            "returned_row_count": 1,
            "truncated": False,
        },
    )
    query_request = request().model_copy(
        update={"original_question": "查询2025年江苏省订单笔数"}
    )
    query_evidence = [
        EvidenceItem(
            evidence_id="analysis:a1",
            kind="ANALYSIS_RESULT",
            source_ref="deterministic:test",
            payload={"facts": query_summary.facts},
        )
    ]
    output = {"claims": [
        {
            "statement": "2025年江苏省订单笔数查询结果已确认，返回的订单笔数为49笔。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
        {
            "statement": "本次返回的是完整查询结果，没有发生结果截断。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
    ]}

    answer, _ = await QwenAnalysisSynthesizer(
        settings(), transport_for(output)
    ).synthesize(query_request, query_summary, query_evidence)

    assert "订单笔数为49笔" in answer
    assert "没有发生结果截断" in answer


@pytest.mark.asyncio
async def test_query_summary_still_rejects_unrelated_business_claim() -> None:
    query_summary = AnalysisOutput(
        answer="本次查询共命中1条结果，订单笔数为49。",
        method="validated_query_result_summary",
        facts={
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "row_count": 1,
            "columns": ["订单笔数"],
        },
    )
    output = {"claims": [{
        "statement": "订单笔数为49，反映市场需求旺盛。",
        "certainty": "VERIFIED_FACT",
        "evidence_ids": ["analysis:a1"],
    }]}

    with pytest.raises(SynthesisValidationError, match="business interpretation"):
        await QwenAnalysisSynthesizer(
            settings(), transport_for(output)
        ).synthesize(
            request(),
            query_summary,
            [EvidenceItem(
                evidence_id="analysis:a1",
                kind="ANALYSIS_RESULT",
                source_ref="deterministic:test",
                payload={"facts": query_summary.facts},
            )],
        )


@pytest.mark.asyncio
async def test_synthesis_prompt_exposes_an_explicit_numeric_allowlist() -> None:
    captured: dict = {}
    output = {"claims": [
        {
            "statement": "销售额下降50。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
        {
            "statement": "当前归因不足以证明因果关系。",
            "certainty": "LIMITATION",
            "evidence_ids": ["analysis:a1"],
        },
    ]}

    await QwenAnalysisSynthesizer(
        settings(), capturing_transport(output, captured)
    ).synthesize(request(), analysis(), evidence())

    allowed = captured["allowed_numbers_for_output"]
    assert 50 in allowed
    assert -80 in allowed
    assert 999 not in allowed


@pytest.mark.asyncio
async def test_synthesis_repairs_one_validation_failure_then_returns_valid_claims() -> None:
    invalid = {"claims": [{
        "statement": "销售额下降999。",
        "certainty": "VERIFIED_FACT",
        "evidence_ids": ["analysis:a1"],
    }]}
    repaired = {"claims": [
        {
            "statement": "销售额下降50。",
            "certainty": "VERIFIED_FACT",
            "evidence_ids": ["analysis:a1"],
        },
        {
            "statement": "当前归因不足以证明因果关系。",
            "certainty": "LIMITATION",
            "evidence_ids": ["analysis:a1"],
        },
    ]}
    calls = [0]

    answer, _ = await QwenAnalysisSynthesizer(
        settings(), sequence_transport([invalid, repaired], calls)
    ).synthesize(request(), analysis(), evidence())

    assert calls == [2]
    assert "销售额下降50" in answer
    assert "999" not in answer


@pytest.mark.asyncio
async def test_qwen_rejects_unknown_evidence() -> None:
    output = {"claims": [{"statement": "销售额下降50。", "certainty": "VERIFIED_FACT", "evidence_ids": ["made-up:evidence"]}]}
    with pytest.raises(SynthesisValidationError, match="unknown evidence"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_qwen_cannot_promote_unselected_knowledge_to_reason() -> None:
    output = {"claims": [{"statement": "市场竞争可能是候选原因，尚未验证。", "certainty": "SUPPORTED_HYPOTHESIS", "evidence_ids": ["knowledge:k1"]}]}
    with pytest.raises(SynthesisValidationError, match="not selected"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_qwen_rejects_causal_verified_fact() -> None:
    output = {"claims": [{"statement": "华东贡献-80导致销售额下降50。", "certainty": "VERIFIED_FACT", "evidence_ids": ["analysis:a1"]}]}
    with pytest.raises(SynthesisValidationError, match="must not assert causality"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_qwen_requires_locked_algorithm_decision() -> None:
    unlocked = AnalysisOutput(answer="销售额下降50。", method="unsafe", facts={}, warnings=[])
    output = {"claims": [{"statement": "销售额下降50。", "certainty": "VERIFIED_FACT", "evidence_ids": ["analysis:a1"]}]}
    with pytest.raises(SynthesisValidationError, match="locked"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), unlocked, evidence())


@pytest.mark.asyncio
async def test_qwen_must_preserve_algorithm_warnings() -> None:
    output = {"claims": [{"statement": "销售额下降50。", "certainty": "VERIFIED_FACT", "evidence_ids": ["analysis:a1"]}]}
    with pytest.raises(SynthesisValidationError, match="require a limitation claim"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


@pytest.mark.asyncio
async def test_qwen_ranking_cannot_drop_requested_profile_columns() -> None:
    base = analysis()
    ranked = AnalysisOutput(
        answer="经销商排名：甲=100（合作时长=24，合作次数=8）。",
        method=base.method,
        facts={
            **base.facts,
            "profile_columns": ["合作时长", "合作次数"],
        },
        warnings=[],
    )
    output = {"claims": [{
        "statement": "经销商排名：甲=100（合作时长=24）。",
        "certainty": "VERIFIED_FACT",
        "evidence_ids": ["analysis:a1"],
    }]}

    with pytest.raises(SynthesisValidationError, match="omitted requested profile"):
        await QwenAnalysisSynthesizer(
            settings(), transport_for(output)
        ).synthesize(request(), ranked, evidence())
