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
        assert "系统处理问题" in answer
        assert "不是您的问题描述有误" in answer
        assert "本次查询没有执行" in answer

    def test_filter_failure_names_the_business_word(self):
        request = make_request([SemanticAmbiguity(
            type="filter",
            question="ASL omitted a metadata-resolved required filter",
            phrase="经销商编码",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "筛选条件“经销商编码”" in answer
        assert "没有能准确对应到数据里的字段" in answer

    def test_internal_slot_names_are_never_shown(self):
        request = make_request([SemanticAmbiguity(
            type="filter",
            question="ASL omitted a metadata-resolved required filter",
            phrase="dimensions",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "dimensions" not in answer
        assert "部分筛选条件没有能准确对应到数据里的字段" in answer

    def test_dimension_failure_uses_grouping_wording(self):
        request = make_request([SemanticAmbiguity(
            type="dimension",
            question="ASL omitted a metadata-resolved required grouping",
            phrase="商品",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "“商品”的分组或展示方式" in answer

    def test_metric_failure_uses_metric_wording(self):
        request = make_request([SemanticAmbiguity(
            type="metric",
            question="intent contract requires at least one governed metric",
            phrase="覆盖率",
        )])
        answer = Orchestrator._plain_binding_failure_answer(request)
        assert_plain_language(answer)
        assert "指标“覆盖率”的统计口径" in answer

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
        assert "查询条件在系统内部校验时没有通过" in answer

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
        assert "没有执行任何数据查询" in answer
        assert "换个说法" in answer
        assert "不用重复提供" in answer
