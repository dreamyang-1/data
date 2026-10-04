from app.domain.models import EvidenceItem, PrimaryIntent, ReliabilityReport
from app.presentation import intent_label_zh, render_reliability_validation


def test_validation_is_chinese_and_expands_each_evidence_item() -> None:
    rendered = render_reliability_validation(
        ReliabilityReport(
            level="HIGH",
            score=1.0,
            gates={"query_succeeded": True, "semantic_metric_verified": True},
        ),
        [
            EvidenceItem(
                evidence_id="query:secret-snapshot",
                kind="QUERY_RESULT",
                source_ref="data-source:58",
                payload={"row_count": 20, "columns": ["经销商", "区域医院覆盖率"]},
            ),
            EvidenceItem(
                evidence_id="semantic:81:205:coverage",
                kind="SEMANTIC_METRIC_RESOLUTION",
                source_ref="oagnet-asl",
                payload={"canonical_name": "区域医院覆盖率", "verified": True},
            ),
        ],
        "PASS",
    )

    assert "校验结论：高可信（1.00）" in rendered
    assert "数据质量：通过" in rendered
    assert "证据（2项）：" in rendered
    assert "1. 查询结果：返回 20 行，字段包括经销商、区域医院覆盖率（来源：" in rendered
    assert "2. 指标口径证据：指标“区域医院覆盖率”已完成口径绑定（来源：" in rendered
    assert "告警：无。" in rendered
    assert "HIGH" not in rendered
    assert "PASS" not in rendered
    assert "data-source:58" not in rendered
    assert "oagnet-asl" not in rendered


def test_validation_expands_warning_with_concrete_reason() -> None:
    warning = "请求时间范围未被当前业务数据水位完整覆盖，不能据此判断水位后的业务事实。"
    rendered = render_reliability_validation(
        ReliabilityReport(
            level="LIMITED",
            score=0.75,
            gates={"query_succeeded": True},
            warnings=[warning],
        ),
        [
            EvidenceItem(
                evidence_id="query:q1",
                kind="QUERY_RESULT",
                source_ref="data-source:58",
                payload={"row_count": 3},
            )
        ],
        "WARNING",
    )

    assert "校验结论：有限可信（0.75）" in rendered
    assert "数据质量：有告警" in rendered
    assert "告警（1项）：" in rendered
    assert warning.rstrip("。") in rendered
    assert "（产生原因：请求时间范围与当前业务数据更新时间不完全一致）" in rendered
    assert rendered.splitlines()[-2].endswith("）")


def test_internal_quality_status_inside_warning_is_translated() -> None:
    rendered = render_reliability_validation(
        ReliabilityReport(
            level="LIMITED",
            score=0.7,
            gates={"query_succeeded": True},
            warnings=["上游数据质量状态为 DEGRADED，结论需要复核。"],
        ),
        [],
        "DEGRADED",
    )

    assert "上游数据质量状态为 降级，结论需要复核" in rendered
    assert "DEGRADED" not in rendered


def test_unknown_evidence_uses_safe_chinese_fallback() -> None:
    rendered = render_reliability_validation(
        ReliabilityReport(level="HIGH", score=1.0, gates={"ok": True}),
        [
            EvidenceItem(
                evidence_id="internal-secret",
                kind="FUTURE_INTERNAL_PROOF",
                source_ref="https://internal.example/secret",
                payload={},
            )
        ],
        "PASS",
    )

    assert "执行过程证据：已记录本轮受控处理步骤（来源：" in rendered
    assert "FUTURE_INTERNAL_PROOF" not in rendered
    assert "internal.example" not in rendered


def test_output_intent_labels_are_chinese() -> None:
    assert intent_label_zh(PrimaryIntent.METRIC_QUERY) == "指标查询"
    assert intent_label_zh(PrimaryIntent.TREND_ANALYSIS) == "趋势分析"
