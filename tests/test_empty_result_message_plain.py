"""查询成功（0 行）的空结果解释不得输出内部绑定诊断。

_empty_result_message 在筛选值绑定证据不足时，曾向用户追加"本次查询未携带完整的
筛选值绑定证据…未完成绑定的字段：…。请查看本次 ASL 的字段和值绑定…"等内部术语。
用户要求查询成功时不输出这些内容，只保留空结果事实与筛选条件。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent
from app.services.orchestrator import DataAnalysisOrchestrator as Orchestrator


def make_request() -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="empty-result-test",
        tenant_id="tenant",
        user_id="user",
        original_question="2025年7月份复旦大学附属华山医院销售订单金额最多的销售员是谁？",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[{"field": "医院名称", "value": "复旦大学附属华山医院"}],
        semantic_filter_bindings=[],
    )


class TestEmptyResultMessagePlain:
    def test_incomplete_binding_hides_internal_diagnostics(self):
        dataset = SimpleNamespace(source_data_as_of=None)
        message = Orchestrator._empty_result_message(make_request(), dataset)
        for mark in (
            "未完成绑定的字段",
            "筛选值绑定证据",
            "ASL",
            "请查看本次",
            "业务名称后重试",
        ):
            assert mark not in message, f"空结果解释泄露内部术语: {mark}"
        assert message.startswith("查询执行成功，返回 0 条结果")
        assert "医院名称=复旦大学附属华山医院" in message

    def test_no_filter_keeps_generic_explanation(self):
        request = make_request().model_copy(update={"filters": []})
        dataset = SimpleNamespace(source_data_as_of=None)
        message = Orchestrator._empty_result_message(request, dataset)
        assert "无数据不等同于指标值为 0" in message
        assert "ASL" not in message
