import json

import httpx
import pytest

from app.analysis.engine import AnalysisOutput
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    EvidenceItem,
    ExtensionExecution,
    HistoryMessage,
    MetricRef,
    PrimaryIntent,
)
from app.services.context_builder import ContextBuilder


def test_context_builder_keeps_question_and_respects_history_budget():
    history = [
        HistoryMessage(role="user", content=f"第 {index} 轮：" + "内容" * 300)
        for index in range(30)
    ]
    context = ContextBuilder(max_characters=5000, max_history_messages=20).build(
        current_question="继续分析上海",
        system_prompt="只使用经过验证的数据。",
        history=history,
    )

    assert context.current_question == "继续分析上海"
    assert len(context.recent_history) < len(history)
    assert context.omitted["history"] > 0
    assert context.estimated_characters <= 5000


def test_context_builder_preserves_old_correction_anchor_when_space_allows():
    history = [HistoryMessage(role="user", content="最初查询销售额")]
    history.extend(
        HistoryMessage(role="assistant" if index % 2 else "user", content=f"普通消息 {index}")
        for index in range(30)
    )
    history.insert(2, HistoryMessage(role="user", content="不是订单量，改成销售额"))

    context = ContextBuilder(max_characters=20_000, max_history_messages=12).build(
        current_question="继续", history=history
    )

    assert any("改成销售额" in item.content for item in context.recent_history)


def test_tool_results_are_summarized_without_raw_rows():
    execution = ExtensionExecution(
        name="inventory", kind="HTTP_TOOL", status="COMPLETED",
        output={"rows": [{"sku": str(index)} for index in range(1000)], "total": 1000},
        execution_id="execution", latency_ms=12,
    )
    context = ContextBuilder().build(
        current_question="分析库存", tool_results=[execution]
    )

    summary = context.tool_result_summaries[0]
    assert summary["output_row_count"] == 1000
    assert summary["total"] == 1000
    assert "rows" not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.asyncio
async def test_synthesizer_receives_structured_context_not_full_tool_output():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "claims": [{
                    "statement": "销售额下降20%",
                    "certainty": "VERIFIED_FACT",
                    "evidence_ids": ["analysis:1"],
                }]
            }, ensure_ascii=False)}}]
        })

    settings = Settings(
        env="test", intent_model_api_key="key",
        intent_model_base_url="https://model.example/v1",
    )
    synthesizer = QwenAnalysisSynthesizer(
        settings, transport=httpx.MockTransport(handler)
    )
    context = ContextBuilder().build(
        current_question="分析销售额变化",
        tool_results=[ExtensionExecution(
            name="query", kind="HTTP_TOOL", status="COMPLETED",
            output={"rows": [{"销售额": index} for index in range(1000)]},
        )],
    )
    request = CanonicalAnalysisRequest(
        conversation_id="c", application_id="app", tenant_id="t", user_id="u",
        original_question="分析销售额变化",
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        metrics=[MetricRef(input="销售额")],
    )
    analysis = AnalysisOutput(
        answer="销售额下降20%", method="comparison",
        facts={
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "change_rate": -0.2,
        },
    )
    evidence = [EvidenceItem(
        evidence_id="analysis:1", kind="ANALYSIS_RESULT",
        source_ref="deterministic", payload={},
    )]

    await synthesizer.synthesize(request, analysis, evidence, context=context)

    user_payload = json.loads(captured["messages"][1]["content"])
    tool_summary = user_payload["agent_context"]["tool_result_summaries"][0]
    assert tool_summary["output_row_count"] == 1000
    assert "rows" not in json.dumps(tool_summary, ensure_ascii=False)
