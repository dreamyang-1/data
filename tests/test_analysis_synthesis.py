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
    assert "已验证结论" in answer and "待验证原因" in answer and "分析限制" in answer


@pytest.mark.asyncio
async def test_qwen_rejects_invented_number() -> None:
    output = {"claims": [{"statement": "销售额下降999。", "certainty": "VERIFIED_FACT", "evidence_ids": ["analysis:a1"]}]}
    with pytest.raises(SynthesisValidationError, match="ungrounded number"):
        await QwenAnalysisSynthesizer(settings(), transport_for(output)).synthesize(request(), analysis(), evidence())


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
