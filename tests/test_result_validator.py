from datetime import date, datetime, timezone

from app.analysis.engine import AnalysisOutput
from app.analysis.result_validator import ResultValidator
from app.domain.models import (
    CanonicalAnalysisRequest,
    Dataset,
    EvidenceItem,
    ExtensionExecution,
    MetricRef,
    PrimaryIntent,
    TimeRange,
)


def request(**updates):
    value = CanonicalAnalysisRequest(
        conversation_id="c",
        application_id="app",
        tenant_id="tenant",
        user_id="user",
        original_question="分析销售额变化",
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        metrics=[MetricRef(input="销售额")],
    )
    return value.model_copy(update=updates)


def dataset(**updates):
    value = Dataset(
        columns=["销售额"],
        rows=[{"销售额": 80.0}],
        snapshot_id="snapshot-1",
        data_as_of=datetime(2026, 8, 31, tzinfo=timezone.utc),
        quality_status="PASS",
        row_count=1,
    )
    return value.model_copy(update=updates)


def evidence():
    return [EvidenceItem(
        evidence_id="query:snapshot-1",
        kind="QUERY_RESULT",
        source_ref="database",
        payload={"row_count": 1},
    )]


def analysis(**facts):
    return AnalysisOutput(
        answer="销售额下降20%",
        method="comparison",
        facts={
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "base": 100.0,
            "current": 80.0,
            "change_rate": -0.2,
            "reconciliation_residual": 0.0,
            "causality_established": False,
            **facts,
        },
    )


def check(report, code):
    return next(item for item in report.checks if item.code == code)


def test_valid_analysis_result_passes_all_mandatory_checks():
    report = ResultValidator().validate(
        request=request(), dataset=dataset(), evidence=evidence(), analysis=analysis()
    )

    assert report.status == "PASS"
    assert check(report, "CHANGE_RATE").status == "PASS"
    assert check(report, "RECONCILIATION").status == "PASS"
    assert check(report, "EVIDENCE_GROUNDING").status == "PASS"
    assert report.confirmed_findings


def test_incorrect_change_rate_is_rejected():
    report = ResultValidator().validate(
        request=request(), dataset=dataset(), evidence=evidence(),
        analysis=analysis(change_rate=-0.1),
    )

    assert report.status == "FAIL"
    assert check(report, "CHANGE_RATE").status == "FAIL"


def test_nonzero_reconciliation_residual_is_rejected():
    report = ResultValidator().validate(
        request=request(), dataset=dataset(), evidence=evidence(),
        analysis=analysis(reconciliation_residual=3.0),
    )

    assert report.status == "FAIL"
    assert check(report, "RECONCILIATION").status == "FAIL"


def test_time_range_after_business_watermark_is_rejected():
    scoped_request = request(time_range=TimeRange(
        start=date(2026, 9, 1), end_exclusive=date(2026, 9, 2)
    ))
    scoped_dataset = dataset(
        source_data_as_of=date(2026, 8, 31),
        source_watermark_field="sales_order.created_date",
    )
    report = ResultValidator().validate(
        request=scoped_request, dataset=scoped_dataset,
        evidence=evidence(), analysis=analysis(),
    )

    assert report.status == "FAIL"
    assert check(report, "TIME_SCOPE").status == "FAIL"


def test_required_tool_failure_fails_but_optional_tool_failure_only_warns():
    execution = ExtensionExecution(
        name="profile", kind="HTTP_TOOL", status="FAILED", error="timeout"
    )
    validator = ResultValidator()
    required = validator.validate(
        request=request(), dataset=dataset(), evidence=evidence(), analysis=analysis(),
        tool_executions=[execution], required_tool_names={"profile"},
    )
    optional = validator.validate(
        request=request(), dataset=dataset(), evidence=evidence(), analysis=analysis(),
        tool_executions=[execution],
    )

    assert required.status == "FAIL"
    assert optional.status == "WARN"


def test_descriptive_contribution_cannot_be_rendered_as_proven_cause():
    report = ResultValidator().validate(
        request=request(), dataset=dataset(), evidence=evidence(), analysis=analysis(),
        final_answer="渠道变化导致销售额下降。",
    )

    assert report.status == "FAIL"
    assert check(report, "CAUSALITY").status == "FAIL"
