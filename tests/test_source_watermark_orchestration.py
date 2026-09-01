from datetime import date, datetime, timezone

from app.domain.models import (
    CanonicalAnalysisRequest,
    Dataset,
    EvidenceItem,
    PrimaryIntent,
    TimeRange,
)
from app.services.orchestrator import DataAnalysisOrchestrator


def request(start: date, end_exclusive: date) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="watermark-conversation",
        tenant_id="tenant",
        user_id="user",
        original_question="查询今年销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        time_range=TimeRange(start=start, end_exclusive=end_exclusive),
    )


def dataset() -> Dataset:
    return Dataset(
        columns=["销售额"],
        rows=[],
        row_count=0,
        snapshot_id="snapshot-1",
        data_as_of=datetime(2026, 8, 28, tzinfo=timezone.utc),
        source_data_as_of=date(2025, 12, 30),
        source_watermark_field="sales_order.created_date",
        quality_status="PASS",
    )


def evidence_for(value: CanonicalAnalysisRequest) -> EvidenceItem:
    return EvidenceItem(
        evidence_id="query:snapshot-1",
        kind="QUERY_RESULT",
        source_ref="data-source:58",
        payload={
            **DataAnalysisOrchestrator._source_watermark_payload(value, dataset()),
            "quality_status": "PASS",
        },
    )


def test_source_watermark_marks_request_outside_available_business_data() -> None:
    value = request(date(2026, 1, 1), date(2027, 1, 1))

    payload = DataAnalysisOrchestrator._source_watermark_payload(value, dataset())
    note = DataAnalysisOrchestrator._source_watermark_note(value, dataset())

    assert payload["requested_time_coverage"] == "OUTSIDE_SOURCE_WATERMARK"
    assert payload["source_data_as_of"] == "2025-12-30"
    assert "空结果不能解释为业务没有发生" in note
    assert "按销售记录时间统计" in note
    assert "sales_order.created_date" not in note


def test_source_watermark_keeps_fully_covered_request_high_reliability() -> None:
    value = request(date(2025, 10, 1), date(2025, 12, 31))

    reliability = DataAnalysisOrchestrator._reliability(
        value, [evidence_for(value)], "PASS"
    )

    assert reliability.level == "HIGH"
    assert reliability.gates["source_watermark_verified"] is True


def test_source_watermark_downgrades_uncovered_time_claims_to_limited() -> None:
    value = request(date(2025, 10, 1), date(2026, 2, 1))

    reliability = DataAnalysisOrchestrator._reliability(
        value, [evidence_for(value)], "PASS"
    )

    assert reliability.level == "LIMITED"
    assert any("未被当前业务数据水位完整覆盖" in item for item in reliability.warnings)


def test_missing_source_watermark_downgrades_time_query_without_failing_it() -> None:
    value = request(date(2025, 10, 1), date(2025, 12, 31))
    evidence = EvidenceItem(
        evidence_id="query:snapshot-1",
        kind="QUERY_RESULT",
        source_ref="data-source:58",
        payload={"quality_status": "PASS"},
    )

    reliability = DataAnalysisOrchestrator._reliability(value, [evidence], "PASS")

    assert reliability.level == "LIMITED"
    assert any("未提供可验证的业务数据水位" in item for item in reliability.warnings)
