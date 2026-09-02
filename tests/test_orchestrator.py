import asyncio
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterBundle, AdapterError
from app.config import Settings
from app.domain.models import (
    AgentResponse,
    AnalysisOperator,
    AtomicTask,
    CanonicalAnalysisRequest,
    ChatRequest,
    ConversationControl,
    DataQueryResult,
    Dataset,
    HistoryMessage,
    MetricRef,
    PrimaryIntent,
    ReliabilityReport,
    TaskPlan,
    TimeRange,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.long_memory import (
    InMemoryLongTermMemoryStore,
    MemoryCandidate,
    MemoryScope,
    MemoryType,
)


def service() -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )


def test_explicit_field_projection_replaces_previous_table_columns():
    classifier = RuleBasedIntentClassifier()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    pending = classifier.classify(
        "查询上海地区三级医院名单。", identity, "field-projection"
    )
    incoming = classifier.classify(
        "只保留医院名称和医院等级。", identity, "field-projection"
    )
    merged = classifier.merge_clarification(
        pending.model_copy(deep=True), "只保留医院名称和医院等级。"
    )

    result = DataAnalysisOrchestrator._preserve_pending_execution_contract(
        pending,
        merged,
        clarification_answer="只保留医院名称和医院等级。",
        incoming=incoming,
    )

    assert result.fields == ["医院名称", "医院等级"]
    assert {"field": "医院等级", "operator": "EQ", "value": "三级医院"} in result.filters


def test_requested_eq_filter_column_is_restored_for_display():
    request = CanonicalAnalysisRequest(
        conversation_id="projection-constant",
        tenant_id="t1",
        user_id="u1",
        original_question="只保留医院名称和医院等级",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        fields=["医院名称", "医院等级"],
        filters=[{"field": "医院等级", "operator": "EQ", "value": "三级医院"}],
    )
    dataset = Dataset(
        columns=["医院名称"],
        rows=[{"医院名称": "测试医院"}],
        row_count=1,
        snapshot_id="projection-constant-snapshot",
        data_as_of=datetime(2025, 12, 30, tzinfo=timezone.utc),
    )

    DataAnalysisOrchestrator._restore_projected_filter_columns(request, dataset)

    assert dataset.columns == ["医院名称", "医院等级"]
    assert dataset.rows == [{"医院名称": "测试医院", "医院等级": "三级医院"}]


def test_requested_business_column_renames_existing_semantic_alias():
    request = CanonicalAnalysisRequest(
        conversation_id="projection-alias",
        tenant_id="t1",
        user_id="u1",
        original_question="只保留医院名称和医院等级",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        fields=["医院名称", "医院等级"],
        filters=[{"field": "医院等级", "operator": "EQ", "value": "三级医院"}],
    )
    dataset = Dataset(
        columns=["医院名称", "hospital_level"],
        rows=[{"医院名称": "测试医院", "hospital_level": "三级"}],
        row_count=1,
        snapshot_id="projection-alias-snapshot",
        data_as_of=datetime(2025, 12, 30, tzinfo=timezone.utc),
    )

    DataAnalysisOrchestrator._restore_projected_filter_columns(request, dataset)

    assert dataset.columns == ["医院名称", "医院等级"]
    assert dataset.rows == [{"医院名称": "测试医院", "医院等级": "三级"}]


def test_singular_product_pronoun_is_bound_from_previous_result_table():
    request = CanonicalAnalysisRequest(
        conversation_id="result-pronoun",
        tenant_id="t1",
        user_id="u1",
        original_question="它卖给了哪些医院？",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
    )

    DataAnalysisOrchestrator._bind_result_entity_reference(
        request,
        "它卖给了哪些医院？",
        ["商品", "含税销售总额"],
        [{"商品": "血液透析设备", "含税销售总额": 634285.724}],
    )

    assert {"field": "商品名称", "operator": "EQ", "value": "血液透析设备"} in request.filters
    assert "RESULT_ENTITY_REFERENCE=商品名称" in request.assumptions


def test_user_visible_answer_hides_known_physical_identifiers():
    answer = DataAnalysisOrchestrator._sanitize_user_visible_answer(
        "按 sales_order.created_date 排序，来源 sales_order.order_key"
    )

    assert answer == "按 销售记录日期 排序，来源 订单号"


def test_filter_cancellation_is_not_whole_task_cancellation():
    assert DataAnalysisOrchestrator._is_cancel_command("取消") is True
    assert DataAnalysisOrchestrator._is_cancel_command("取消查询") is True
    assert DataAnalysisOrchestrator._is_cancel_command("取消医院等级条件") is False
    assert DataAnalysisOrchestrator._is_cancel_command("取消比较，只看2026年") is False


class TimeClarificationLosingClassifier:
    """Simulate an isolated follow-up being mistaken for a detail request."""

    def classify(self, question, identity, conversation_id):
        if question.startswith("帮我找出上海地区"):
            return CanonicalAnalysisRequest(
                conversation_id=conversation_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                original_question=question,
                primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
                secondary_intents=[PrimaryIntent.DETAIL_QUERY],
                operators=[
                    AnalysisOperator.FILTER,
                    AnalysisOperator.GROUP_BY,
                    AnalysisOperator.COMPARE,
                    AnalysisOperator.SORT,
                    AnalysisOperator.TOP_N,
                ],
                metrics=[MetricRef(
                    input="整体业务规模",
                    metric_id="metric.business_scale",
                    canonical_name="整体业务规模",
                )],
                entity="经销商",
                dimensions=["经销商"],
                filters=[
                    {"field": "地区", "operator": "=", "value": "上海"},
                    {"field": "品牌", "operator": "=", "value": "振德医疗"},
                    {"field": "商品", "operator": "=", "value": "医用外科口罩"},
                ],
                comparison_type="对象间比较",
                ranking_limit=3,
                missing_slots=["time_range"],
                assumptions=["RANK_BY=整体业务规模"],
                intent_confidence=0.97,
            )
        return CanonicalAnalysisRequest(
            conversation_id=conversation_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            original_question=question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            operators=[AnalysisOperator.FILTER, AnalysisOperator.RENDER_TABLE],
            entity="销售记录",
            fields=["经销商名称", "销售日期"],
            time_range=TimeRange(
                start=date(2025, 10, 17),
                end_exclusive=date(2025, 12, 31),
            ),
            missing_slots=[],
            intent_confidence=0.9,
        )

    def merge_clarification(self, previous, answer):
        # Deliberately mimic a lossy model merger. The orchestrator must treat
        # this as a time-slot patch because the active pending state owns the
        # already confirmed execution contract.
        merged = self.classify(
            answer,
            TrustedIdentity(tenant_id=previous.tenant_id, user_id=previous.user_id),
            previous.conversation_id,
        )
        merged.original_question = f"{previous.original_question}\n补充：{answer}"
        merged.conversation_control = ConversationControl.CLARIFICATION_RESPONSE
        return merged


class CapturingRankingRetrieval:
    def __init__(self) -> None:
        self.request = None

    async def health(self):
        return True

    async def rewrite_health(self):
        return True

    async def query(
        self, request, identity, *, semantic_model_id, business_domain_id
    ):
        self.request = request.model_copy(deep=True)
        rows = [
            {"经销商名称": "甲", "整体业务规模": 300},
            {"经销商名称": "乙", "整体业务规模": 200},
            {"经销商名称": "丙", "整体业务规模": 100},
        ]
        return DataQueryResult(
            asl={
                "version": "2.0",
                "intent": "query",
                "metrics": [{
                    "name": "metric.business_scale",
                    "alias": "整体业务规模",
                }],
                "ambiguity": [],
            },
            sql="SELECT governed_ranking",
            dataset=Dataset(
                columns=list(rows[0]),
                rows=rows,
                row_count=3,
                snapshot_id="pending-ranking-snapshot",
                data_as_of=datetime(2025, 12, 30, tzinfo=timezone.utc),
                source_data_as_of=date(2025, 12, 30),
                source_watermark_field="sales_order.created_date",
            ),
            data_source_id="test",
        )


class FailingThenCapturingRetrieval:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0
        self.request = None

    async def health(self):
        return True

    async def rewrite_health(self):
        return True

    async def query(self, request, identity, *, semantic_model_id, business_domain_id):
        self.calls += 1
        self.request = request.model_copy(deep=True)
        if self.calls == 1:
            raise AdapterError("HTTP_502", "upstream unavailable", retryable=True)
        return await self.delegate.query(
            request, identity, semantic_model_id=semantic_model_id,
            business_domain_id=business_domain_id,
        )


class RelationshipCountProjectionRetrieval:
    def __init__(self, *, truncated: bool = False, total_row_count: int | None = None):
        self.request = None
        self.truncated = truncated
        self.total_row_count = total_row_count

    async def health(self):
        return True

    async def rewrite_health(self):
        return True

    async def query(self, request, identity, *, semantic_model_id, business_domain_id):
        self.request = request.model_copy(deep=True)
        assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
        assert request.fields == ["经销商名称"]
        assert request.execution_contract_transform == (
            "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
        )
        rows = [
            {"经销商名称": f"经销商{index:03d}"}
            for index in range(1, 102)
        ]
        return DataQueryResult(
            asl={
                "version": "2.0",
                "subject": {"entity": "dealer"},
                "metrics": [],
                "dimensions": [{"name": "dealer.dealer_name"}],
                "ambiguity": [],
            },
            sql=(
                "SELECT DISTINCT dealer.dealer_name AS 经销商名称 "
                "FROM dealer LEFT JOIN sales_order ON 1=1"
            ),
            dataset=Dataset(
                columns=["经销商名称"],
                rows=rows,
                row_count=len(rows),
                total_row_count=(
                    self.total_row_count
                    if self.total_row_count is not None
                    else len(rows)
                ),
                truncated=self.truncated,
                snapshot_id="relationship-count-snapshot",
                data_as_of=datetime.now(timezone.utc),
                source_data_as_of=date.today(),
                source_watermark_field="sales_order.created_date",
            ),
            data_source_id="58",
        )


@pytest.mark.asyncio
async def test_relationship_count_projection_is_accepted_as_verified_metric_evidence():
    base = build_mock_adapters()
    retrieval = RelationshipCountProjectionRetrieval()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False,
            multi_question_enabled=False,
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=AdapterBundle(
            semantic=base.semantic,
            retrieval=retrieval,
            knowledge=base.knowledge,
            policy=base.policy,
            analysis=base.analysis,
        ),
        sessions=InMemorySessionStore(),
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="relationship-count",
            message_id="relationship-count-1",
            question="统计上海市紫杉醇释放冠脉球囊导管已合作经销商数",
            semantic_model_id=81,
            business_domain_ids=[205],
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    assert response.status == "COMPLETED"
    assert response.reliability is not None
    assert response.reliability.level == "HIGH"
    assert response.reliability.gates == {
        "query_succeeded": True,
        "metric_bound": True,
        "semantic_metric_verified": True,
        "derived_metric_contract_verified": True,
        "source_watermark_verified": True,
    }
    assert "101" in response.answer
    derived = next(
        item for item in response.evidence
        if item.kind == "DERIVED_METRIC_RESOLUTION"
    )
    assert derived.payload["source_projection"] == "经销商名称"
    assert derived.payload["source_row_count"] == 101
    assert derived.payload["derived_value"] == 101


def test_relationship_count_projection_rejects_unconfirmed_truncated_total():
    request = RuleBasedIntentClassifier().classify(
        "统计上海市紫杉醇释放冠脉球囊导管已合作经销商数",
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "relationship-count-truncated",
    )
    rows = [{"经销商名称": f"经销商{index:03d}"} for index in range(100)]
    result = DataQueryResult(
        asl={"metrics": [], "dimensions": [{"name": "dealer.dealer_name"}]},
        sql="SELECT DISTINCT dealer.dealer_name AS 经销商名称 FROM dealer",
        dataset=Dataset(
            columns=["经销商名称"],
            rows=rows,
            row_count=len(rows),
            truncated=True,
            snapshot_id="relationship-count-truncated-snapshot",
            data_as_of=datetime.now(timezone.utc),
        ),
    )

    with pytest.raises(AdapterError) as captured:
        DataAnalysisOrchestrator._relationship_count_projection_result(
            request, result
        )

    assert captured.value.code == "RELATIONSHIP_COUNT_SOURCE_INCOMPLETE"


def test_relationship_count_projection_accepts_confirmed_distinct_total():
    request = RuleBasedIntentClassifier().classify(
        "统计上海市紫杉醇释放冠脉球囊导管已合作经销商数",
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "relationship-count-confirmed-total",
    )
    rows = [{"经销商名称": f"经销商{index:03d}"} for index in range(100)]
    result = DataQueryResult(
        asl={"metrics": [], "dimensions": [{"name": "dealer.dealer_name"}]},
        sql="SELECT DISTINCT dealer.dealer_name AS 经销商名称 FROM dealer",
        dataset=Dataset(
            columns=["经销商名称"],
            rows=rows,
            row_count=len(rows),
            total_row_count=101,
            truncated=True,
            snapshot_id="relationship-count-confirmed-snapshot",
            data_as_of=datetime.now(timezone.utc),
        ),
    )

    transformed = DataAnalysisOrchestrator._relationship_count_projection_result(
        request, result
    )

    assert transformed.dataset.rows == [{"已合作经销商数": 101}]
    assert transformed.execution_transforms[-1]["distinctness_method"] == (
        "UPSTREAM_DISTINCT_TOTAL"
    )
    assert transformed.execution_transforms[-1]["source_total_confirmed"] is True


def test_asl_metric_binding_preserves_semantic_model_scope_for_followups():
    request = CanonicalAnalysisRequest(
        conversation_id="metric-scope",
        application_id="app",
        tenant_id="tenant",
        user_id="user",
        original_question="查询上个月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[
            MetricRef(
                input="销售额",
                metric_id="6:actual_payment_amount",
                version="current",
                canonical_name="实付金额",
                unit="元",
            )
        ],
    )

    DataAnalysisOrchestrator._bind_metrics_from_asl(
        request,
        {
            "metrics": [
                {"name": "actual_payment_amount", "alias": "销售额"}
            ]
        },
        6,
    )

    assert request.metrics == [
        MetricRef(
            input="销售额",
            metric_id="6:actual_payment_amount",
            version="current",
            canonical_name="实付金额",
            unit="元",
        )
    ]


@pytest.mark.asyncio
async def test_business_only_lineage_is_structured_and_not_claimed_high_reliability():
    response = await service().handle(
        ChatRequest(
            application_id="app-lineage",
            conversation_id="lineage-conversation",
            message_id="lineage-message",
            question="销售额的数据血缘是什么？",
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.DATA_LINEAGE
    assert "业务血缘：" in response.answer
    assert " → " in response.answer
    assert response.reliability.level == "LIMITED"
    assert response.reliability.gates["physical_lineage_present"] is False


class SlowCancelableOrchestrator(DataAnalysisOrchestrator):
    def __init__(self) -> None:
        super().__init__(
            settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
            classifier=RuleBasedIntentClassifier(),
            adapters=build_mock_adapters(),
            sessions=InMemorySessionStore(),
        )
        self.started = asyncio.Event()
        self.execution_cancelled = asyncio.Event()

    async def _handle_request(self, chat, identity):
        if self._is_cancel_command(chat.question):
            return AgentResponse(
                request_id=uuid4(),
                conversation_id=chat.conversation_id,
                status="CANCELLED",
                intent=PrimaryIntent.OUT_OF_SCOPE,
                answer="cancel",
                reliability=ReliabilityReport(level="HIGH", score=1.0, gates={}),
            )
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.execution_cancelled.set()
            raise


@pytest.mark.asyncio
async def test_query_requires_and_accepts_clarification():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="c1", message_id="m1", question="帮我查一下销售额"),
        identity,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert "time_range" in first.missing_slots
    assert [step.stage for step in first.analysis_process] == [
        "QUESTION_REWRITE",
        "UNDERSTANDING",
        "COMPLETENESS_CHECK",
    ]
    assert first.analysis_process[-1].status == "NEEDS_INPUT"

    second = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="c1", message_id="m2", question="本月"), identity
    )
    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.METRIC_QUERY
    assert second.reliability and second.reliability.level == "LIMITED"
    assert any(
        "未提供可验证的业务数据水位" in warning
        for warning in second.reliability.warnings
    )


@pytest.mark.asyncio
async def test_time_clarification_preserves_ranked_comparison_execution_contract():
    retrieval = CapturingRankingRetrieval()
    base = build_mock_adapters()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=TimeClarificationLosingClassifier(),
        adapters=AdapterBundle(
            semantic=base.semantic,
            retrieval=retrieval,
            knowledge=base.knowledge,
            policy=base.policy,
            analysis=base.analysis,
        ),
        sessions=InMemorySessionStore(),
    )
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="ranked-time-clarification",
            message_id="m1",
            question=(
                "帮我找出上海地区正在销售振德医疗品牌的医用外科口罩产品的"
                "经销商名单，并按他们现有的整体业务规模排序。"
            ),
        ),
        identity,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert first.missing_slots == ["time_range"]

    second = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="ranked-time-clarification",
            message_id="m2",
            question="按2025年10月17日至2025年12月30日期间有销售记录的经销商筛选",
        ),
        identity,
    )

    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert retrieval.request is not None
    executed = retrieval.request
    assert executed.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert executed.secondary_intents == [PrimaryIntent.DETAIL_QUERY]
    assert [item.input for item in executed.metrics] == ["整体业务规模"]
    assert executed.entity == "经销商"
    assert executed.dimensions == ["经销商"]
    assert executed.fields == []
    assert executed.filters == [
        {"field": "地区", "operator": "=", "value": "上海"},
        {"field": "品牌", "operator": "=", "value": "振德医疗"},
        {"field": "商品", "operator": "=", "value": "医用外科口罩"},
    ]
    assert executed.comparison_type == "对象间比较"
    assert executed.ranking_limit == 3
    assert executed.operators == [
        AnalysisOperator.FILTER,
        AnalysisOperator.GROUP_BY,
        AnalysisOperator.COMPARE,
        AnalysisOperator.SORT,
        AnalysisOperator.TOP_N,
    ]
    assert executed.time_range == TimeRange(
        start=date(2025, 10, 17),
        end_exclusive=date(2025, 12, 31),
    )
    assert "TRANSACTION_TIME_SCOPE=SALES_RECORD" in executed.assumptions
    assert "第1名 甲=300" in second.answer


def test_sales_report_relationship_tasks_receive_trusted_time_scope_only():
    sales_plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        final_deliverable="COMBINED_REPORT",
        tasks=[
            AtomicTask(task_id="task-1", question="分析某产品整体销售趋势"),
            AtomicTask(task_id="task-2", question="列出某产品医院覆盖明细"),
            AtomicTask(task_id="task-3", question="列出某产品合作经销商数据明细"),
        ],
    )
    employee_plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        final_deliverable="COMBINED_REPORT",
        tasks=[
            AtomicTask(task_id="task-1", question="分析销售人员入职趋势"),
            AtomicTask(task_id="task-2", question="列出销售人员部门覆盖明细"),
        ],
    )

    assert DataAnalysisOrchestrator._report_task_internal_assumptions(
        sales_plan, sales_plan.tasks[0]
    ) == ()
    assert DataAnalysisOrchestrator._report_task_internal_assumptions(
        sales_plan, sales_plan.tasks[1]
    ) == ("TRANSACTION_TIME_SCOPE=SALES_RECORD",)
    assert DataAnalysisOrchestrator._report_task_internal_assumptions(
        sales_plan, sales_plan.tasks[2]
    ) == ("TRANSACTION_TIME_SCOPE=SALES_RECORD",)
    assert DataAnalysisOrchestrator._report_task_internal_assumptions(
        employee_plan, employee_plan.tasks[1]
    ) == ()


@pytest.mark.asyncio
async def test_pending_clarification_is_not_merged_after_semantic_scope_switch():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="scope-switch", message_id="m1",
            question="帮我查一下销售额", semantic_model_id=8,
        ),
        identity,
    )
    assert first.status == "NEEDS_CLARIFICATION"
    second = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="scope-switch", message_id="m2",
            question="本月", semantic_model_id=9,
        ),
        identity,
    )
    assert second.status != "COMPLETED"
    pending = await agent.sessions.get_pending("t1", "u1", "app1", "scope-switch")
    assert pending is None or pending.request.semantic_model_id == 9
    if pending is not None:
        assert not any(metric.input == "销售额" for metric in pending.request.metrics)


@pytest.mark.asyncio
async def test_complete_new_query_discards_unanswered_pending_clarification():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="abandon-pending",
            message_id="m1",
            question="展示前三名",
            semantic_model_id=81,
        ),
        identity,
    )
    assert first.status == "NEEDS_CLARIFICATION"

    second = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="abandon-pending",
            message_id="m2",
            question="查询最近一年销售过费森尤斯产品的经销商名单。",
            semantic_model_id=81,
        ),
        identity,
    )

    assert second.status != "NEEDS_CLARIFICATION"
    assert second.intent == PrimaryIntent.DETAIL_QUERY
    pending = await agent.sessions.get_pending(
        "t1", "u1", "app1", "abandon-pending"
    )
    assert pending is None
    remembered = await agent.sessions.get_last_request(
        "t1", "u1", "app1", "abandon-pending"
    )
    assert remembered is not None
    assert remembered.metrics == []


@pytest.mark.asyncio
async def test_detail_query_runs_without_permission_module_for_now():
    response = await service().handle(
        ChatRequest(
            application_id="app1",
            conversation_id="c2",
            message_id="m1",
            question="查询昨天订单明细，显示订单号和金额",
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )
    assert response.status == "COMPLETED"


@pytest.mark.asyncio
async def test_forecast_fails_closed_when_history_is_insufficient():
    response = await service().handle(
        ChatRequest(
            application_id="app1",
            conversation_id="c3", message_id="m1", question="预测下个月销售额"
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )
    assert response.intent == PrimaryIntent.FORECAST_ANALYSIS
    assert response.status == "NEEDS_CLARIFICATION"
    assert response.missing_slots == ["forecast_history_range"]


@pytest.mark.asyncio
async def test_duplicate_message_id_returns_cached_response_without_advancing_round():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    chat = ChatRequest(application_id="app1", conversation_id="idem", message_id="same", question="帮我查销售额")
    first = await agent.handle(chat, identity)
    duplicate = await agent.handle(chat, identity)
    assert duplicate == first
    completed = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="idem", message_id="next", question="本月"), identity
    )
    assert completed.status == "COMPLETED"


@pytest.mark.asyncio
async def test_repeated_self_contained_query_with_new_message_id_reuses_response():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="repeat-query",
            message_id="m1", question="查询最近一年销售额。",
        ),
        identity,
    )
    repeated = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="repeat-query",
            message_id="m2", question="查询最近一年销售额",
        ),
        identity,
    )

    assert repeated.answer == first.answer
    assert repeated.evidence == first.evidence
    assert repeated.request_id != first.request_id


def test_elliptical_followups_are_not_repeat_cache_candidates():
    for question in ("展示20条结果", "其中最高的是哪个", "那再查询最近一个月"):
        assert not DataAnalysisOrchestrator._is_repeat_cache_candidate(
            ChatRequest(
                application_id="app1", conversation_id="repeat-query",
                message_id=question, question=question,
            )
        )


@pytest.mark.asyncio
async def test_explicit_dataset_unavailable_fails_closed_without_database_fallback():
    agent = service()
    response = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="missing-dataset",
            message_id="m1",
            question="计算这份表里的销售额合计",
            dataset_id="dataset-does-not-exist",
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )
    assert response.status == "SAFE_FALLBACK"
    assert "数据集" in response.answer


@pytest.mark.asyncio
async def test_cancel_clears_pending_clarification():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    await agent.handle(ChatRequest(application_id="app1", conversation_id="cancel", message_id="m1", question="查销售额"), identity)
    cancelled = await agent.handle(ChatRequest(application_id="app1", conversation_id="cancel", message_id="m2", question="算了，不用了"), identity)
    assert cancelled.status == "CANCELLED"
    fresh = await agent.handle(ChatRequest(application_id="app1", conversation_id="cancel", message_id="m3", question="你好"), identity)
    assert fresh.intent == PrimaryIntent.CHAT


@pytest.mark.asyncio
async def test_cancel_command_stops_running_request_in_same_trusted_conversation():
    agent = SlowCancelableOrchestrator()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    running = asyncio.create_task(
        agent.handle(
            ChatRequest(
                application_id="app1",
                conversation_id="running-cancel",
                message_id="m1",
                question="查询本月销售趋势",
            ),
            identity,
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    command = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="running-cancel",
            message_id="m2",
            question="停止查询",
        ),
        identity,
    )
    original = await asyncio.wait_for(running, timeout=1)

    assert command.status == "CANCELLED"
    assert command.reliability.gates["running_request_cancelled"] is True
    assert original.status == "CANCELLED"
    assert agent.execution_cancelled.is_set()
    assert agent._running_requests == {}


@pytest.mark.asyncio
async def test_cancel_command_cannot_stop_another_users_running_request():
    agent = SlowCancelableOrchestrator()
    owner = TrustedIdentity(tenant_id="t1", user_id="owner")
    other = TrustedIdentity(tenant_id="t1", user_id="other")
    running = asyncio.create_task(
        agent.handle(
            ChatRequest(
                application_id="app1",
                conversation_id="shared-id",
                message_id="m1",
                question="查询本月销售趋势",
            ),
            owner,
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    unrelated = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="shared-id",
            message_id="m2",
            question="取消",
        ),
        other,
    )
    assert unrelated.status == "CANCELLED"
    assert unrelated.reliability.gates["running_request_cancelled"] is False
    assert not running.done()

    await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="shared-id",
            message_id="m3",
            question="取消",
        ),
        owner,
    )
    assert (await asyncio.wait_for(running, timeout=1)).status == "CANCELLED"


@pytest.mark.asyncio
async def test_same_conversation_id_is_isolated_between_users():
    agent = service()
    await agent.handle(
        ChatRequest(application_id="app1", conversation_id="shared", message_id="m1", question="查销售额"),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )
    other = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="shared", message_id="m1", question="你好"),
        TrustedIdentity(tenant_id="t1", user_id="u2"),
    )
    assert other.intent == PrimaryIntent.CHAT


@pytest.mark.asyncio
async def test_follow_up_inherits_last_completed_request():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="follow", message_id="m1", question="查询本月销售额"), identity
    )
    assert first.status == "COMPLETED"
    follow = await agent.handle(
        ChatRequest(application_id="app1", conversation_id="follow", message_id="m2", question="那华东呢"), identity
    )
    assert follow.status == "COMPLETED"
    remembered = await agent.sessions.get_last_request("t1", "u1", "app1", "follow")
    assert remembered.conversation_control.value == "FOLLOW_UP"
    assert "查询本月销售额" in remembered.original_question
    assert "那华东呢" in remembered.original_question


@pytest.mark.asyncio
async def test_follow_up_uses_task_frame_after_upstream_failure():
    adapters = build_mock_adapters()
    retrieval = FailingThenCapturingRetrieval(adapters.retrieval)
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=AdapterBundle(
            semantic=adapters.semantic, retrieval=retrieval,
            knowledge=adapters.knowledge, policy=adapters.policy,
            analysis=adapters.analysis,
        ),
        sessions=InMemorySessionStore(),
    )
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")

    first = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="failed-follow",
            message_id="m1", question="查询2026年7月销售额",
        ), identity,
    )
    assert first.status != "COMPLETED"
    assert await agent.sessions.get_last_request("t1", "u1", "app1", "failed-follow") is None
    frame = await agent.sessions.get_task_frame("t1", "u1", "app1", "failed-follow")
    assert frame is not None
    assert frame.asl_template is None

    follow = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="failed-follow",
            message_id="m2", question="按日统计",
        ), identity,
    )
    assert follow.status != "NEEDS_CLARIFICATION"
    assert retrieval.calls == 2
    assert [metric.input for metric in retrieval.request.metrics] == ["销售额"]
    assert retrieval.request.primary_intent == PrimaryIntent.TREND_ANALYSIS


@pytest.mark.asyncio
async def test_granularity_only_follow_up_inherits_metric_and_becomes_trend():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="daily-follow",
            message_id="m1", question="查询2026年7月销售额",
        ),
        identity,
    )
    assert first.status == "COMPLETED"

    follow = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="daily-follow",
            message_id="m2", question="按日统计",
        ),
        identity,
    )

    assert follow.intent == PrimaryIntent.TREND_ANALYSIS
    assert follow.status != "NEEDS_CLARIFICATION"
    remembered = await agent.sessions.get_last_request(
        "t1", "u1", "app1", "daily-follow"
    )
    assert remembered is not None
    assert [metric.input for metric in remembered.metrics] == ["销售额"]
    assert remembered.time_range is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "follow_up",
    ["按月给我", "只看上海", "换成订单量", "也加上销售数量", "那江苏呢", "前20个"],
)
async def test_natural_elliptical_follow_up_recovers_task_frame(follow_up):
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id=f"natural-{follow_up}",
            message_id="m1", question="查询2026年7月销售额",
        ), identity,
    )
    assert first.status == "COMPLETED"

    response = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id=f"natural-{follow_up}",
            message_id="m2", question=follow_up,
        ), identity,
    )
    assert response.status != "NEEDS_CLARIFICATION"
    frame = await agent.sessions.get_task_frame(
        "t1", "u1", "app1", f"natural-{follow_up}"
    )
    assert frame is not None
    assert frame.metrics


@pytest.mark.asyncio
async def test_history_recovers_clarification_after_short_memory_is_missing():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    response = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="cold-session",
            message_id="m3",
            question="本月",
            history=[
                HistoryMessage(role="user", content="帮我查询销售额", message_id="m1"),
                HistoryMessage(
                    role="assistant",
                    content="还需要补充以下信息：要分析哪个时间范围？",
                    message_id="m2",
                ),
            ],
        ),
        identity,
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.METRIC_QUERY
    remembered = await agent.sessions.get_last_request("t1", "u1", "app1", "cold-session")
    assert remembered is not None
    assert remembered.conversation_control.value == "CLARIFICATION_RESPONSE"
    assert "帮我查询销售额" in remembered.original_question
    assert "本月" in remembered.original_question


@pytest.mark.asyncio
async def test_confirmed_long_term_preference_is_applied_and_auditable():
    memories = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="t1", user_id="u1", application_id="app1")
    candidate = await memories.create_candidate(
        MemoryCandidate(
            scope=scope,
            memory_type=MemoryType.DISPLAY_PREFERENCE,
            memory_key="default_unit",
            summary="金额默认使用万元",
            value={"unit": "万元"},
            confidence=1,
            source_session_id="setup",
            source_message_id="setup-1",
            created_by="u1",
        )
    )
    active = await memories.confirm(scope, candidate.memory_id, confirmed_by="u1")
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        memories=memories,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="memory-use",
            message_id="m1",
            question="查询本月销售额",
            use_longterm_memory=True,
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )

    assert response.status == "COMPLETED"
    assert any(item.kind == "CONFIRMED_LONG_TERM_MEMORY" for item in response.evidence)
    remembered = await agent.sessions.get_last_request("t1", "u1", "app1", "memory-use")
    assert remembered is not None
    assert remembered.confirmed_memory_ids == [active.memory_id]
    assert "万元" in (remembered.rewritten_question or "")


@pytest.mark.asyncio
async def test_confirmed_default_fills_only_a_missing_slot():
    memories = InMemoryLongTermMemoryStore()
    scope = MemoryScope(tenant_id="t1", user_id="u1", application_id="app1")
    candidate = await memories.create_candidate(
        MemoryCandidate(
            scope=scope,
            memory_type=MemoryType.USER_PREFERENCE,
            memory_key="default_time_period",
            summary="默认查询本月",
            value={"time_period": "本月"},
            confidence=1,
            source_session_id="setup",
            source_message_id="setup-time",
            created_by="u1",
        )
    )
    await memories.confirm(scope, candidate.memory_id, confirmed_by="u1")
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        memories=memories,
    )

    response = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="memory-default",
            message_id="m1",
            question="查询销售额",
            use_longterm_memory=True,
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )

    assert response.status == "COMPLETED"
    remembered = await agent.sessions.get_task_frame(
        "t1", "u1", "app1", "memory-default"
    )
    assert remembered is not None
    assert remembered.time_range.start == date(2026, 8, 1)
    assert remembered.time_range.end_exclusive == date(2026, 9, 1)
    assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" not in remembered.assumptions

    explicit = await agent.handle(
        ChatRequest(
            application_id="app1", conversation_id="memory-explicit",
            message_id="m1", question="查询2026年7月销售额",
            use_longterm_memory=True,
        ),
        TrustedIdentity(tenant_id="t1", user_id="u1"),
    )
    assert explicit.status == "COMPLETED"
    explicit_frame = await agent.sessions.get_task_frame(
        "t1", "u1", "app1", "memory-explicit"
    )
    assert explicit_frame.time_range.start == date(2026, 7, 1)
    assert explicit_frame.time_range.end_exclusive == date(2026, 8, 1)


@pytest.mark.asyncio
async def test_contextual_follow_up_inherits_slots_but_switches_to_analysis_intent():
    agent = service()
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    first = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="analysis-follow-up",
            message_id="m1",
            question="查询本月销售额",
        ),
        identity,
    )
    assert first.status == "COMPLETED"

    follow_up = await agent.handle(
        ChatRequest(
            application_id="app1",
            conversation_id="analysis-follow-up",
            message_id="m2",
            question="这个数据为什么下降？",
        ),
        identity,
    )

    assert follow_up.intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS
    assert follow_up.status != "NEEDS_CLARIFICATION"
    remembered = await agent.sessions.get_last_request(
        "t1", "u1", "app1", "analysis-follow-up"
    )
    # The advanced analysis safely falls back on the one-row mock dataset, so the last completed
    # request remains the first query. The response intent proves routing switched correctly.
    assert remembered is not None
    assert remembered.metrics[0].input == "销售额"
