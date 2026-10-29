# 用户2026-10-26要求：按具体失败原因解释，不再断言统一改写问题的文案。
"""ASL 绑定失败的最终输出必须口语化，不复述内部校验原文。

STRUCTURED_ASL_BINDING 阶段 SAFE_FALLBACK 时，最终输出曾直接拼接
request.ambiguities（内部校验 issue 原文：英文诊断、字段路径、'null' 等），
与思考里的解析校验内容重复且过于专业。改后由
Orchestrator._plain_binding_failure_answer 按歧义类型归类成通俗原因。
"""
from __future__ import annotations

import pytest

from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, SemanticAmbiguity
from app.services.orchestrator import DataAnalysisOrchestrator as Orchestrator


def make_request(ambiguities: list[SemanticAmbiguity]) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="plain-test",
        tenant_id="tenant",
        user_id="user",
        original_question="测试问题",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_ambiguities=ambiguities,
    )


FORBIDDEN_INTERNAL_MARKS = (
    "omitted", "required grouping", "required filter", "ASL_", "STRUCTURED_",
    "SYSTEM_FAILURE", "SAFE_FALLBACK", "candidates", "blockers", "slot=",
)


def assert_plain_language(answer: str):
    lowered = answer.casefold()
    for mark in FORBIDDEN_INTERNAL_MARKS:
        assert mark.casefold() not in lowered, f"泄露内部术语: {mark}"
    assert "null" not in lowered


class TestPlainBindingFailureAnswer:
    def test_system_failure_context_is_colloquial(self):
        # 真实场景：任务规划未产出结构化参数，issue 原文带槽位名与 'null'
        request = make_request([SemanticAmbiguity(
            type="context",
            question="结构化参数【结构化提取】null：上游未提供结构化参数，"
                     "请检查任务拆分与规划节点；ASL不会重新读取原问题提取。",
            phrase="null",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "前一步没有传来整理好的查询条件" in answer
        assert "无需重复说明问题" in answer
        assert "尚未读取数据" in answer

    def test_filter_failure_names_the_business_word(self):
        request = make_request([SemanticAmbiguity(
            type="filter",
            question="ASL omitted a metadata-resolved required filter",
            phrase="经销商编码",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "筛选条件“经销商编码”" in answer
        assert "遗漏了已经确认的" in answer

    def test_internal_slot_names_are_never_shown(self):
        request = make_request([SemanticAmbiguity(
            type="filter",
            question="ASL omitted a metadata-resolved required filter",
            phrase="dimensions",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "dimensions" not in answer
        assert "遗漏了已经确认的筛选条件" in answer

    def test_dimension_failure_uses_grouping_wording(self):
        request = make_request([SemanticAmbiguity(
            type="dimension",
            question="ASL omitted a metadata-resolved required grouping",
            phrase="商品",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "遗漏了要求的分组或展示字段“商品”" in answer

    def test_metric_failure_uses_metric_wording(self):
        request = make_request([SemanticAmbiguity(
            type="metric",
            question="intent contract requires at least one governed metric",
            phrase="覆盖率",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "缺少已确认的指标“覆盖率”定义" in answer

    def test_time_anchor_failure_is_explained_simply(self):
        request = make_request([SemanticAmbiguity(
            type="time_anchor",
            question="ASL_TIME_ANCHOR_MISSING",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "时间范围" in answer

    def test_empty_ambiguities_fall_back_to_generic_reason(self):
        answer = Orchestrator._plain_binding_failure_answer(make_request([]))
        assert_plain_language(answer)
        assert "服务未返回具体失败项" in answer

    def test_reasons_are_deduplicated(self):
        request = make_request([
            SemanticAmbiguity(type="filter", question="f1", phrase="经销商编码"),
            SemanticAmbiguity(type="filter", question="f2", phrase="经销商编码"),
        ])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert answer.count("筛选条件“经销商编码”") == 1

    def test_output_always_tells_user_what_to_do(self):
        request = make_request([SemanticAmbiguity(
            type="context", question="上游未提供结构化参数", phrase="null",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert "尚未读取数据" in answer
        assert "检查步骤之间的数据传递" in answer
        assert "无需重复说明问题" in answer


class TestStructuredParamClarificationPlain:
    """ASL 参数绑定追问（ASK 路径）同样必须口语化。

    Oagnet issue() 的追问原文带槽位名、过滤条件 JSON、物理字段路径与
    “向量标准值”等内部术语，澄清提问展示前必须改写成业务化提问。
    """

    RAW_VALUE_MISMATCH = (
        '结构化参数【过滤条件[1]】{"entity": "产品分类", "field": "产品一级分类", '
        '"op": "=", "value": "内窥镜"}：'
        "字段 product_category.category_level1 已匹配，"
        "但条件值尚未匹配到该字段的向量标准值，请核对字段层级及值目录。"
        "请补充或确认该项；已明确的其他条件无需重复提供。"
    )

    def test_value_mismatch_question_is_plain(self):
        text = Orchestrator._sanitize_clarification_text(self.RAW_VALUE_MISMATCH)
        for mark in ("结构化参数【", "向量标准值", "category_level1", "{", "}",
                     "请补充或确认该项", "op"):
            assert mark not in text, f"追问泄露内部术语: {mark}"
        assert "内窥镜" in text
        assert "产品一级分类" in text
        assert "写法" in text or "所属分类" in text

    def test_value_mismatch_without_value_keeps_field_only(self):
        raw = (
            '结构化参数【过滤条件[2]】{"field": "城市", "op": "=", "value": []}：'
            "字段 dim_city.city_id 已匹配，但条件值尚未匹配到该字段的向量标准值，"
            "请核对字段层级及值目录"
        )
        text = Orchestrator._sanitize_clarification_text(raw)
        assert "结构化参数【" not in text
        assert "向量标准值" not in text
        assert "dim_city" not in text
        assert "城市" in text

    def test_non_json_structured_param_falls_back_generic(self):
        raw = "结构化参数【结构化提取】null：上游未提供结构化参数，请检查任务拆分与规划节点"
        text = Orchestrator._sanitize_clarification_text(raw)
        for mark in ("结构化参数【", "null", "任务拆分与规划"):
            assert mark not in text
        assert "前一步没有传来整理好的查询条件" in text

    def test_other_structured_reasons_keep_business_condition(self):
        raw = (
            '结构化参数【排序[1]】{"field": "含税销售总额", "order": "DOWN"}：'
            "当前ASL仅支持一个明确升序/降序的排序字段"
        )
        text = Orchestrator._sanitize_clarification_text(raw)
        for mark in ("结构化参数【", "{", "}", "ASL"):
            assert mark not in text
        assert "含税销售总额" in text
        assert "排序方式未被正确识别" in text

    def test_normal_business_question_passes_through(self):
        text = Orchestrator._sanitize_clarification_text("请确认要统计哪个大区的销售额？")
        assert text == "请确认要统计哪个大区的销售额？"
