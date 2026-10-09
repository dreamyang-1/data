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
    assert data["planning_context"]["completed_question"] == data["completed_question"]
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


@pytest.mark.asyncio
async def test_dsl_is_live_user_reference_and_preserves_actual_query(monkeypatch, tmp_path):
    from app.domain import semantic_description as sd
    path = tmp_path / "semantic.md"
    description = "指标：医院总数；单位：家；绑定维度：省份、城市。医院所在城市不同于经销商所在城市。"
    path.write_text(description, encoding="utf-8-sig")
    monkeypatch.setattr(sd, "_LOCAL_DESCRIPTION_PATH", path)
    sd.clear_cache()
    actual = {"asl": {"metrics": [{"name": "hospital_count"}], "dimensions": [], "time_context": None},
              "sql": "SELECT COUNT(*) FROM hospital"}
    source = analysis()
    source.facts["executed_query"] = actual
    calls = []
    model = QwenAnalysisSynthesizer(settings(), transport_for("按实际范围解释医院总数。", calls))
    await model.synthesize(request(), source, evidence())
    body = json.loads(calls[0]["messages"][1]["content"])
    assert body["semantic_reference"] == {"source": "语义描述文件.md", "available": True, "content": description}
    assert body["facts"]["executed_query"] == actual
    assert "semantic_reference" not in source.facts
    system = calls[0]["messages"][0]["content"]
    assert description not in system  # Business text must not become system instructions.
    assert "不表示本次已经按这些维度分组" in system
    assert "不自动证明业务口径正确" in system
    assert "如实说明差异" in system
    path.write_text("更新后的业务说明", encoding="utf-8")
    await model.synthesize(request(), source, evidence())
    assert json.loads(calls[1]["messages"][1]["content"])["semantic_reference"]["content"] == "更新后的业务说明"


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", [None, b"", b"\xff\xfe\x00"])
async def test_missing_empty_or_unreadable_dsl_does_not_block_analysis(monkeypatch, tmp_path, contents):
    from app.domain import semantic_description as sd
    path = tmp_path / "semantic.md"
    if contents is not None:
        path.write_bytes(contents)
    monkeypatch.setattr(sd, "_LOCAL_DESCRIPTION_PATH", path)
    sd.clear_cache()
    calls = []
    text, _ = await QwenAnalysisSynthesizer(settings(), transport_for("已有数据仍可分析。", calls)).synthesize(
        request(), analysis(), evidence())
    assert text == "已有数据仍可分析。"
    reference = json.loads(calls[0]["messages"][1]["content"])["semantic_reference"]
    assert reference["available"] is False and reference["content"] == ""


def test_insight_and_planning_use_the_same_semantic_document():
    # 拆分与洞察综合统一走 semantic_description 加载器，本地兜底文档路径唯一。
    from app.domain.semantic_description import _LOCAL_DESCRIPTION_PATH
    assert _LOCAL_DESCRIPTION_PATH.name == "语义描述文件.md"


@pytest.mark.asyncio
async def test_nl_context_driven_style_is_used_without_importing_its_tool_workflow():
    calls = []
    await QwenAnalysisSynthesizer(settings(), transport_for("按问题自然展开分析。", calls)).synthesize(
        request(), analysis(), evidence())
    body = calls[0]
    prompt = body["messages"][0]["content"]
    # Reuse the real step5 guidance, not the README's outdated report template.
    assert "基于上下文中的事实和数据回答，不要编造信息" in prompt
    assert "如果有工具执行结果，优先基于结果回答" in prompt
    assert "用户需求的核心目标、约束条件和关键变量" in prompt
    assert "不固定套用章节" in prompt and "保留有数据支持的发现和表述" in prompt
    assert "不发起追问、不重新规划或调用工具" in prompt
    assert "禁止在末尾追加" in prompt
    assert "趋势关注全期方向" not in prompt  # Retired fixed intent recipe.
    assert "可用工具" not in prompt and "<<CLARIFICATION>>" not in prompt
    assert "tools" not in body
    assert "semantic_reference" in json.loads(body["messages"][1]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("paragraphs", [
    ["### 范围与观察\n本次只有一个汇总值，无法判断同比变化。", "它描述的是本次查询范围，并非增长判断。"],
    ["先看差异：华东贡献-80，华南贡献30。", "华南抵消了部分下降，净变化为-50；这不能证明下降的业务原因。"],
    ["当前仅提供20条预览，完整结果有781条。", "可以介绍预览记录，但不能据此计算全量集中度。"],
])
async def test_nl_style_report_preserves_model_paragraphs_and_headings(paragraphs):
    calls = []
    text, output = await QwenAnalysisSynthesizer(settings(), transport_for(
        {"claims": [{"statement": p} for p in paragraphs]}, calls)).synthesize(request(), analysis(), evidence())
    assert text == "\n\n".join(paragraphs)
    assert [claim.statement for claim in output.claims] == paragraphs
    assert len(calls) == 1  # No new narrative review, rewrite or ordering pass.
