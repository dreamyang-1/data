from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.adapters.base import AdapterBundle, MetricDiscovery
from app.analysis import AnalysisError, AnalysisOutput
from app.analysis.synthesis import SynthesisClaim, SynthesisOutput
from app.config import Settings
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ChatRequest,
    DataQueryResult,
    Dataset,
    EvidenceItem,
    KnowledgeContext,
    MetricRef,
    PrimaryIntent,
    TimeRange,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.orchestrator import _requires_deterministic_analysis
from app.stores import InMemorySessionStore
from minio_followup_store import DatasetReference, LoadedDataset


class SemanticStub:
    async def resolve_metrics(self, request, semantic_model_id):
        return request.metrics

    async def definition(self, metric):
        raise AssertionError("not used")

    async def lineage(self, metric, identity):
        raise AssertionError("not used")


def test_transaction_activity_definition_is_disclosed_with_exact_period():
    request = CanonicalAnalysisRequest(
        conversation_id="activity-definition-note",
        tenant_id="t1",
        user_id="u1",
        original_question="近一年内哪些活跃经销商在销售某产品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        time_range=TimeRange(
            start=date(2025, 8, 28),
            end_exclusive=date(2026, 8, 29),
        ),
        assumptions=[
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
        ],
    )

    assert DataAnalysisOrchestrator._activity_definition_note(request) == (
        "活跃口径：本次将 2025-08-28 至 2026-08-28 "
        "期间内存在销售记录的对象定义为活跃对象。"
    )


@pytest.mark.asyncio
async def test_missing_metric_can_become_published_attribute_detail():
    class QueryStub:
        async def discover_metrics(self, *args, **kwargs):
            return MetricDiscovery(metrics=[])

        async def discover_attribute_details(self, *args, **kwargs):
            return MetricDiscovery(
                metrics=[],
                evidence_fingerprint="sha256:attribute-snapshot",
                subject="device_inspection_data",
                dimensions=(
                    "detection_value",
                    "detection_unit",
                ),
                filters=(
                    {"field": "device_daily.device_name", "operator": "=", "value": "卧式成缆1"},
                    {"field": "device_daily.model_name", "operator": "=", "value": "摇篮2#3150盘径"},
                ),
            )

    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.classifier = SimpleNamespace(rules=RuleBasedIntentClassifier())
    orchestrator.adapters = SimpleNamespace(query=QueryStub())
    request = CanonicalAnalysisRequest(
        conversation_id="raw-attribute-detail",
        tenant_id="tenant",
        user_id="user",
        original_question="卧式成缆1的摇篮2#3150盘径近两个月的检测值",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        operators=[AnalysisOperator.FILTER],
        semantic_entity_mentions=["卧式成缆1", "摇篮2#3150盘径"],
        missing_slots=["metric"],
        time_range=TimeRange(
            start=date(2026, 7, 2), end_exclusive=date(2026, 9, 3)
        ),
    )
    chat = ChatRequest(
        application_id="app",
        conversation_id=request.conversation_id,
        message_id="m1",
        question=request.original_question,
        semantic_model_id=85,
        business_domain_id=217,
    )

    recovered = await orchestrator._recover_live_published_metrics(
        request, chat, TrustedIdentity(tenant_id="tenant", user_id="user")
    )

    assert recovered is True
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.metrics == []
    assert request.entity == "device_inspection_data"
    assert request.fields == [
        "detection_value", "detection_unit",
    ]
    assert request.semantic_entity_mentions == []
    assert request.missing_slots == []
    assert "QUERY_SHAPE_TRANSFORM=METRIC_TO_PUBLISHED_ATTRIBUTE_DETAIL" in request.assumptions


def test_optional_presentation_failure_does_not_lower_data_reliability() -> None:
    analysis_request = CanonicalAnalysisRequest(
        conversation_id="presentation-degraded",
        tenant_id="t1",
        user_id="u1",
        original_question="按销售额比较经销商",
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
    )
    reliability = DataAnalysisOrchestrator._reliability(
        analysis_request,
        [
            EvidenceItem(
                evidence_id="query:q1",
                kind="QUERY_RESULT",
                source_ref="data:test",
                payload={"row_count": 2},
            ),
            EvidenceItem(
                evidence_id="analysis:a1",
                kind="ANALYSIS_RESULT",
                source_ref="deterministic:test",
                payload={
                    "method": "validated_ordered_metric_ranking",
                    "facts": {},
                    "warnings": [],
                    "presentation_warnings": [
                        "Qwen分析总结不可用，已返回确定性分析结果"
                    ],
                },
            ),
        ],
        "PASS",
    )

    assert reliability.level == "HIGH"
    assert reliability.warnings == []


class RetrievalStub:
    def __init__(
        self, *, truncated: bool = False, download_only: bool = False,
        row_count: int = 2,
    ):
        self.truncated = truncated
        self.download_only = download_only
        self.row_count = row_count

    async def query(self, request, identity, *, semantic_model_id, business_domain_id):
        rows = (
            []
            if self.download_only
            else (
                [
                    {"月份": "2026-01", "销售额": 100},
                    {"月份": "2026-02", "销售额": 125},
                ]
                if self.row_count == 2
                else [
                    {
                        "月份": f"2026-{(index % 12) + 1:02d}",
                        "销售额": 100 + index,
                    }
                    for index in range(self.row_count)
                ]
            )
        )
        return DataQueryResult(
            asl={"metrics": [{"name": "sales", "alias": "销售额"}]},
            sql="SELECT month, sales FROM approved_view",
            data_source_id="test-source",
            dataset=Dataset(
                columns=["月份", "销售额"],
                rows=rows,
                row_count=len(rows),
                total_row_count=201 if self.download_only else len(rows),
                snapshot_id="snapshot-1",
                data_as_of=datetime.now(timezone.utc),
                truncated=self.truncated or self.download_only,
            ),
            result_file_url=(
                "http://minio/bam/result.xlsx" if self.download_only else None
            ),
        )


class KnowledgeStub:
    async def retrieve_analysis_context(self, request, dataset, identity):
        raise AssertionError("knowledge retrieval must be skipped without an authorized scope")


def service(*, truncated: bool = False, download_only: bool = False,
            row_count: int = 2,
            synthesizer=None, analysis_engine=None,
            knowledge_defaults: list[str] | None = None,
            sessions=None, dataset_store=None, report_exporter=None,
            extension_dispatcher=None) -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False,
            knowledge_base_names=knowledge_defaults or [],
        ),
        RuleBasedIntentClassifier(),
        AdapterBundle(
            semantic=SemanticStub(),
            retrieval=RetrievalStub(
                truncated=truncated, download_only=download_only,
                row_count=row_count,
            ),
            knowledge=KnowledgeStub(),
        ),
        sessions or InMemorySessionStore(),
        analysis_engine=analysis_engine,
        analysis_synthesizer=synthesizer,
        dataset_store=dataset_store,
        report_exporter=report_exporter,
        extension_dispatcher=extension_dispatcher,
    )


class SmallDatasetStore:
    def __init__(self):
        self.rows = ()

    def save_dataset(self, **kwargs):
        self.rows = tuple(kwargs["rows"])
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        return DatasetReference(
            dataset_id="dataset-small-report",
            bucket="bam",
            object_name="data-analysis/datasets/small.json",
            scope=kwargs["scope"],
            columns=tuple(kwargs["columns"]),
            row_count=len(self.rows),
            byte_size=100,
            snapshot_id=kwargs["snapshot_id"],
            data_as_of=kwargs["data_as_of"].isoformat(),
            created_at=now,
            expires_at=(now_dt + timedelta(hours=1)).isoformat(),
            source_type=kwargs["source_type"],
            source_ref=kwargs["source_ref"],
        )

    def load_dataset(self, reference, *, current_scope):
        assert current_scope == reference.scope
        return LoadedDataset(reference, self.rows)


class ReportExporterStub:
    def __init__(self):
        self.exported_dataset_id = None

    def export(self, reference, **kwargs):
        self.exported_dataset_id = reference.dataset_id
        return {
            "report_id": "report-small",
            "format": kwargs["file_format"],
            "object_name": "data-analysis/reports/report-small.xlsx",
            "download_url": "http://minio/bam/report-small.xlsx",
            "byte_size": 123,
            "object_expires_at": datetime.now(timezone.utc).isoformat(),
            "report_reference": {
                "report_id": "report-small",
                "object_name": "data-analysis/reports/report-small.xlsx",
                "expires_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def delete_object(self, object_name):
        raise AssertionError(f"unexpected report cleanup: {object_name}")


class FailingReportExporter:
    def export(self, reference, **kwargs):
        raise RuntimeError("simulated MinIO report failure")


class SynthesisStub:
    async def synthesize(self, request, analysis, evidence):
        return (
            "模型整理后的证据化总结",
            SynthesisOutput(
                claims=[
                    SynthesisClaim(
                        statement="销售额从100上升到125。",
                        certainty="VERIFIED_FACT",
                        evidence_ids=[f"analysis:{request.request_id}"],
                    )
                ]
            ),
        )


class FailingAnalysisEngine:
    def analyze(self, request, columns, rows, knowledge):
        raise AnalysisError("测试中的数据形状不支持分析")


class WarningAnalysisEngine:
    def analyze(self, request, columns, rows, knowledge):
        return AnalysisOutput(
            answer="确定性分析结论。",
            method="test_method",
            facts={"value": 1},
            warnings=["这是必须向用户披露的限制"],
        )


def test_plain_detail_sort_does_not_trigger_comparison_analysis() -> None:
    request = CanonicalAnalysisRequest(
        conversation_id="detail-sort",
        tenant_id="tenant",
        user_id="user",
        original_question="列出订单明细并按订单号排序",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        operators=[AnalysisOperator.FILTER, AnalysisOperator.SORT],
        entity="订单",
        fields=["订单号"],
    )

    assert _requires_deterministic_analysis(request) is False


@pytest.mark.asyncio
async def test_trend_chain_returns_calculation_evidence() -> None:
    response = await service().handle(
        ChatRequest(
            application_id="app",
            conversation_id="trend",
            message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "COMPLETED"
    assert "25.00%" in response.answer
    analysis = next(item for item in response.evidence if item.kind == "ANALYSIS_RESULT")
    assert analysis.payload["method"] == "robust_trend_diagnostics"
    assert analysis.payload["facts"]["change_rate"] == pytest.approx(0.25)
    assert response.reliability and response.reliability.gates["analysis_succeeded"] is True
    assert response.reliability.gates["semantic_metric_verified"] is True
    semantic_evidence = next(
        item for item in response.evidence
        if item.kind == "SEMANTIC_METRIC_RESOLUTION"
    )
    assert semantic_evidence.payload == {
        "metric_id": "sales",
        "canonical_name": "销售额",
        "semantic_model_id": 1,
        "business_domain_id": 1,
        "verified": True,
    }
    stages = [step.stage for step in response.analysis_process]
    assert stages == [
        "QUESTION_REWRITE",
        "UNDERSTANDING",
        "ANALYSIS_PLANNING",
        "COMPLETENESS_CHECK",
        "DATA_QUERY",
        "METRIC_VERIFICATION",
        "DETERMINISTIC_ANALYSIS",
        "RELIABILITY_CHECK",
    ]
    analysis_step = next(
        step for step in response.analysis_process
        if step.stage == "DETERMINISTIC_ANALYSIS"
    )
    assert analysis.evidence_id in analysis_step.evidence_ids
    serialized_process = str(
        [step.model_dump(mode="json") for step in response.analysis_process]
    )
    assert "SELECT month" not in serialized_process
    assert "approved_view" not in serialized_process


@pytest.mark.asyncio
async def test_empty_application_kb_binding_never_falls_back_to_global_default() -> None:
    response = await service(knowledge_defaults=["GLOBAL_KB_MUST_NOT_BE_USED"]).handle(
        ChatRequest(
            application_id="app", conversation_id="no-kb", message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1, business_domain_id=1,
            knowledge_base_names=[],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "COMPLETED"


@pytest.mark.asyncio
async def test_truncated_analysis_fails_closed_before_calculation() -> None:
    response = await service(truncated=True).handle(
        ChatRequest(
            application_id="app",
            conversation_id="truncated",
            message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "SAFE_FALLBACK"
    assert "截断" in response.answer
    assert not response.evidence


@pytest.mark.asyncio
async def test_extension_receives_explicit_preview_contract_above_200_rows() -> None:
    class CapturingExtensionDispatcher:
        def __init__(self):
            self.payload = None

        async def execute(self, **kwargs):
            self.payload = kwargs["payload"]
            return []

    dispatcher = CapturingExtensionDispatcher()
    response = await service(
        row_count=250,
        extension_dispatcher=dispatcher,
    ).handle(
        ChatRequest(
            application_id="app",
            conversation_id="extension-preview",
            message_id="m1",
            question="查询2026年销售额",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert dispatcher.payload is not None
    assert len(dispatcher.payload["rows"]) == 200
    assert dispatcher.payload["row_count"] == 250
    assert dispatcher.payload["returned_row_count_for_extension"] == 200
    assert dispatcher.payload["truncated_for_extension"] is True
    assert dispatcher.payload["complete_dataset_available_to_extension"] is False


@pytest.mark.asyncio
async def test_download_only_metric_query_returns_file_instead_of_fake_empty_data() -> None:
    response = await service(download_only=True).handle(
        ChatRequest(
            application_id="app",
            conversation_id="download-only",
            message_id="m1",
            question="查询2026年7月销售额",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert "201 条" in response.answer
    assert response.result_file_url == "http://minio/bam/result.xlsx"
    assert len(response.files) == 1
    assert response.files[0].format == "xlsx"
    assert response.files[0].download_url == response.result_file_url
    assert "[下载完整查询结果（XLSX）](http://minio/bam/result.xlsx)" in response.answer
    assert response.dataset_id is None


@pytest.mark.asyncio
async def test_empty_detail_query_is_a_completed_auditable_result() -> None:
    response = await service(row_count=0).handle(
        ChatRequest(
            application_id="app",
            conversation_id="empty-detail-result",
            message_id="m1",
            question="查询最近一年销售过费森尤斯产品的经销商名单",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.intent is PrimaryIntent.DETAIL_QUERY
    assert "0 条结果" in response.answer
    assert "并非查询执行失败" in response.answer
    query_evidence = next(
        item for item in response.evidence if item.kind == "QUERY_RESULT"
    )
    assert query_evidence.payload["row_count"] == 0
    assert response.reliability is not None
    assert response.reliability.gates["empty_result_confirmed"] is True


@pytest.mark.asyncio
async def test_empty_metric_query_is_completed_without_fabricating_zero() -> None:
    response = await service(row_count=0).handle(
        ChatRequest(
            application_id="app",
            conversation_id="empty-metric-result",
            message_id="m1",
            question="查询2025年12月含税销售总额",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.intent is PrimaryIntent.METRIC_QUERY
    assert "无数据不等同于指标值为 0" in response.answer
    query_evidence = next(
        item for item in response.evidence if item.kind == "QUERY_RESULT"
    )
    assert query_evidence.payload["row_count"] == 0


@pytest.mark.asyncio
async def test_sparse_trend_preserves_and_displays_real_query_rows() -> None:
    response = await service(row_count=1).handle(
        ChatRequest(
            application_id="app",
            conversation_id="sparse-trend-result",
            message_id="m1",
            question="查看2026年各城市每月销售趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "PARTIAL_SUCCESS"
    assert response.intent is PrimaryIntent.TREND_ANALYSIS
    assert "数据库实际返回的数据" in response.answer
    assert "2026-01" in response.answer
    assert "100" in response.answer
    assert "最少 2 行" in response.answer
    assert not any(item.kind == "ANALYSIS_RESULT" for item in response.evidence)
    query_evidence = next(
        item for item in response.evidence if item.kind == "QUERY_RESULT"
    )
    assert query_evidence.payload["row_count"] == 1
    assert query_evidence.payload["analysis_sufficiency"] == {
        "sufficient": False,
        "required_rows": 2,
        "returned_rows": 1,
    }
    assert response.reliability is not None
    assert response.reliability.level == "LIMITED"
    assert response.reliability.gates["query_evidence_preserved"] is True
    assert response.reliability.gates["analysis_conclusion_withheld"] is True


@pytest.mark.asyncio
async def test_large_report_exports_detail_but_does_not_fake_analysis_from_preview() -> None:
    sessions = InMemorySessionStore()
    response = await service(download_only=True, sessions=sessions).handle(
        ChatRequest(
            application_id="app",
            conversation_id="large-report",
            message_id="m1",
            question="查询2026年7月销售额并导出Excel",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "PARTIAL_SUCCESS"
    assert response.intent == PrimaryIntent.REPORT_GENERATION
    assert response.result_file_url == "http://minio/bam/result.xlsx"
    assert response.dataset_id is None
    assert len(response.files) == 1
    assert response.files[0].download_url == response.result_file_url
    assert response.result_file_url in response.answer
    assert "SQL服务已生成完整明细文件" in response.answer
    assert "未生成分析结论" in response.answer
    assert response.evidence[0].payload["complete_result_file"] is True
    assert response.evidence[0].payload["complete_analysis_statistics"] is False
    assert response.reliability is not None
    assert response.reliability.level == "LIMITED"
    previous = await sessions.get_last_request(
        "tenant", "user", "app", "large-report"
    )
    assert previous is not None
    assert "LATEST_RESULT_DATASET_NOT_REUSABLE" in previous.assumptions


@pytest.mark.asyncio
async def test_small_report_is_persisted_then_exported_by_agent() -> None:
    sessions = InMemorySessionStore()
    store = SmallDatasetStore()
    exporter = ReportExporterStub()
    response = await service(
        sessions=sessions,
        dataset_store=store,
        report_exporter=exporter,
    ).handle(
        ChatRequest(
            application_id="app",
            conversation_id="small-report",
            message_id="m1",
            question="查询2026年1月至2月销售额并导出Excel",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.REPORT_GENERATION
    assert response.dataset_id == "dataset-small-report"
    assert exporter.exported_dataset_id == response.dataset_id
    assert response.result_file_url is None
    assert response.files[0].format == "xlsx"
    assert response.files[0].download_url == "http://minio/bam/report-small.xlsx"


@pytest.mark.asyncio
async def test_followup_export_reuses_previous_dataset_without_requery() -> None:
    sessions = InMemorySessionStore()
    store = SmallDatasetStore()
    exporter = ReportExporterStub()
    agent = service(
        sessions=sessions,
        dataset_store=store,
        report_exporter=exporter,
    )
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")

    first = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-export",
            message_id="m1",
            question="查询2026年1月至2月销售额",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        identity,
    )
    assert first.status == "COMPLETED"
    assert first.dataset_id == "dataset-small-report"

    second = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-export",
            message_id="m2",
            question="把刚才结果导出成Excel",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        identity,
    )

    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.REPORT_GENERATION
    assert second.dataset_id == first.dataset_id
    assert second.files[0].download_url == "http://minio/bam/report-small.xlsx"


@pytest.mark.asyncio
async def test_report_storage_failure_does_not_discard_valid_query_result() -> None:
    response = await service(
        dataset_store=SmallDatasetStore(),
        report_exporter=FailingReportExporter(),
    ).handle(
        ChatRequest(
            application_id="app",
            conversation_id="report-storage-failure",
            message_id="m1",
            question="查询2026年1月至2月销售额并导出Excel",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.dataset_id == "dataset-small-report"
    assert response.files == []
    assert "下载文件生成失败" in response.answer


@pytest.mark.asyncio
async def test_qwen_synthesis_is_used_only_after_analysis_evidence_exists() -> None:
    response = await service(synthesizer=SynthesisStub()).handle(
        ChatRequest(
            application_id="app",
            conversation_id="synthesis",
            message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "COMPLETED"
    assert response.answer == "模型整理后的证据化总结"
    kinds = [item.kind for item in response.evidence]
    assert kinds.index("ANALYSIS_RESULT") < kinds.index("ANSWER_SYNTHESIS")
    synthesis_step = next(
        step for step in response.analysis_process if step.stage == "MODEL_SYNTHESIS"
    )
    assert synthesis_step.status == "COMPLETED"
    assert "模型不被允许新增无证据事实" in synthesis_step.summary


@pytest.mark.asyncio
async def test_analysis_failure_preserves_query_and_metric_evidence() -> None:
    response = await service(analysis_engine=FailingAnalysisEngine()).handle(
        ChatRequest(
            application_id="app",
            conversation_id="analysis-failure",
            message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "SAFE_FALLBACK"
    assert {item.kind for item in response.evidence} == {
        "QUERY_RESULT",
        "SEMANTIC_METRIC_RESOLUTION",
    }
    assert response.analysis_process[-1].stage == "SAFE_TERMINATION"
    assert response.analysis_process[-1].status == "FAILED"


@pytest.mark.asyncio
async def test_analysis_warnings_are_visible_in_answer() -> None:
    response = await service(analysis_engine=WarningAnalysisEngine()).handle(
        ChatRequest(
            application_id="app",
            conversation_id="analysis-warning",
            message_id="m1",
            question="分析2026年1月到2月销售额趋势",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    assert response.status == "COMPLETED"
    assert "注意事项" in response.answer
    assert "这是必须向用户披露的限制" in response.answer
