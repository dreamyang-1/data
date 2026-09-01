from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    DataQueryResult,
    Dataset,
    MetricRef,
    PrimaryIntent,
    SemanticSqlValidationCheck,
    SemanticSqlValidationLayer,
    SemanticSqlValidationReport,
)
from app.services.validated_query_recall import ValidatedQueryRecall
from app.stores.session import InMemorySessionStore


def request(question: str = "查询上海经销商合作次数前5名") -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        application_id="app-a",
        conversation_id="conversation-a",
        original_question=question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="合作次数", metric_id="81:dealer_cooperation_count")],
        dimensions=["经销商"],
        filters=[{"field": "地区", "operator": "EQ", "value": "上海市"}],
        ranking_limit=5,
        semantic_model_id=81,
    )


def validation(status: str = "PASS") -> SemanticSqlValidationReport:
    layer_status = "PASS" if status == "PASS" else "WARN"
    return SemanticSqlValidationReport(
        status=status,
        layers={
            name: SemanticSqlValidationLayer(
                status=layer_status,
                checks=[SemanticSqlValidationCheck(
                    code=f"{name.upper()}_CHECK",
                    status=layer_status,
                    message="validated",
                )],
            )
            for name in ("syntax", "semantic", "business")
        },
    )


def result(*, quality: str = "PASS", rows: int = 1, report=True) -> DataQueryResult:
    data_rows = [{"经销商": "甲", "合作次数": 10}] if rows else []
    return DataQueryResult(
        asl={"subject": {"entity": "dealer_profile"}},
        sql="SELECT dealer_name, cooperation_count FROM dealer_result",
        dataset=Dataset(
            columns=["经销商", "合作次数"],
            rows=data_rows,
            row_count=len(data_rows),
            total_row_count=len(data_rows),
            snapshot_id="snapshot-1",
            data_as_of=datetime.now(timezone.utc),
            quality_status=quality,
        ),
        semantic_validation_report=(
            validation() if report is True else (None if report is False else report)
        ),
    )


def test_admission_requires_three_layer_pass_execution_and_quality() -> None:
    assert ValidatedQueryRecall.eligible(result()) is True
    assert ValidatedQueryRecall.eligible(result(report=False)) is False
    assert ValidatedQueryRecall.eligible(result(report=validation("WARN"))) is False
    assert ValidatedQueryRecall.eligible(result(quality="WARN")) is False
    assert ValidatedQueryRecall.eligible(result(rows=0)) is False


@pytest.mark.asyncio
async def test_recall_is_scoped_and_contains_no_sql_asl_or_rows() -> None:
    store = InMemorySessionStore()
    recall = ValidatedQueryRecall(store, top_k=3, max_items=20)
    source = request()
    example = recall.build(source, result(), semantic_model_id=81)
    await recall.remember(example)

    recalled = await recall.recall(
        request("上海经销商按合作次数取前五名"), semantic_model_id=81
    )
    assert len(recalled) == 1
    payload = recalled[0].model_dump(mode="json")
    serialized = str(payload).lower()
    assert "select " not in serialized
    assert "sql" not in payload
    assert "asl" not in payload
    assert "rows" not in payload
    assert recalled[0].filter_shapes == [
        {"field": "地区", "operator": "EQ", "value_type": "str"}
    ]

    other_tenant = request("上海经销商按合作次数取前五名")
    other_tenant.tenant_id = "tenant-b"
    assert await recall.recall(other_tenant, semantic_model_id=81) == []


def test_semantic_payload_cannot_expose_scope_or_executable_plan() -> None:
    example = ValidatedQueryRecall.build(request(), result(), semantic_model_id=81)
    payload = example.semantic_payload()
    assert set(payload).isdisjoint({"sql", "asl", "rows", "tenant_id", "application_id"})
    assert payload["metric_ids"] == ["81:dealer_cooperation_count"]
