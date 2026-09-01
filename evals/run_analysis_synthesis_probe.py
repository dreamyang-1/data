from __future__ import annotations

import asyncio
import json

from app.analysis.engine import AnalysisOutput
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, EvidenceItem, MetricRef, PrimaryIntent


async def main() -> None:
    settings = Settings()
    request = CanonicalAnalysisRequest(
        conversation_id="qwen-synthesis-probe",
        tenant_id="probe",
        user_id="probe",
        original_question="为什么本期销售额下降？",
        primary_intent=PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        metrics=[MetricRef(input="销售额", metric_id="sales", version="v1")],
    )
    analysis = AnalysisOutput(
        answer=(
            "本期销售额下降50。华东贡献-80，占绝对贡献72.73%；"
            "华南贡献+30，覆盖度100.00%。华东促销结束是待验证候选。"
        ),
        method="ranked_contribution_candidates",
        facts={
            "contribution_sum": -50,
            "coverage": 1.0,
            "causality_established": False,
            "ranked_candidates": [
                {"label": "华东", "contribution": -80, "absolute_contribution_share": 0.7273},
                {"label": "华南", "contribution": 30, "absolute_contribution_share": 0.2727},
            ],
            "matched_knowledge": [
                {"labels": "华东", "content": "华东区域本期促销结束", "source": "运营记录"}
            ],
        },
        warnings=["归因结果是贡献驱动而非因果证明"],
    )
    evidence = [
        EvidenceItem(
            evidence_id="query:probe", kind="QUERY_RESULT", source_ref="data:probe",
            payload={"row_count": 2},
        ),
        EvidenceItem(
            evidence_id="analysis:probe", kind="ANALYSIS_RESULT",
            source_ref="deterministic:ranked_contribution_candidates",
            payload={"facts": analysis.facts, "warnings": analysis.warnings},
        ),
        EvidenceItem(
            evidence_id="knowledge:probe", kind="ANALYSIS_KNOWLEDGE", source_ref="kb:probe",
            payload={"sources": [{"source": "运营记录"}]},
        ),
    ]
    answer, output = await QwenAnalysisSynthesizer(settings).synthesize(
        request, analysis, evidence
    )
    print(answer)
    print(json.dumps(output.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
