from datetime import datetime, timezone

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    ChatRequest,
    DataQueryResult,
    Dataset,
    KnowledgeContext,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def request_for(question: str):
    return RuleBasedIntentClassifier().classify(
        question,
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "conversation",
    )


def test_null_aggregate_is_reported_as_no_data_instead_of_none_value() -> None:
    request = request_for("查询本月销售额")
    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["销售额"],
        [{"销售额": None}],
        KnowledgeContext(query=request.original_question, documents=[]),
    )
    assert "没有有效数据" in answer
    assert "None" not in answer


def test_query_answer_uses_business_markdown_table_and_hides_physical_prefix() -> None:
    request = request_for("查询销售额前两名产品")
    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["product.product_name", "含税销售总额"],
        [
            {"product.product_name": "产品甲", "含税销售总额": 1234.5},
            {"product.product_name": "产品乙", "含税销售总额": None},
        ],
        KnowledgeContext(query=request.original_question, documents=[]),
    )
    assert "| 产品名称 | 含税销售总额 |" in answer
    assert "| 产品甲 | 1,234.5 |" in answer
    assert "| 产品乙 | — |" in answer
    assert "product.product_name" not in answer


def test_metric_table_displays_all_138_untruncated_rows() -> None:
    request = request_for(
        "统计每家医院承接的订单总金额（含税）及订单笔数，并关联医院等级"
    )
    rows = [
        {
            "医院": f"测试医院{index:03d}",
            "医院等级": "三级医院",
            "订单总金额（含税）": index * 100,
            "订单笔数": index,
        }
        for index in range(1, 139)
    ]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["医院", "医院等级", "订单总金额（含税）", "订单笔数"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "共 138 行" in answer
    assert "测试医院001" in answer
    assert "测试医院138" in answer
    assert answer.count("| 测试医院") == 138
    assert "当前展示前" not in answer


def test_complete_217_row_product_list_is_not_silently_presented_as_preview() -> None:
    request = request_for("查询国药集团上海医疗器械有限公司销售的产品有哪些")
    request.entity = "商品"
    request.fields = ["商品名称"]
    rows = [{"商品名称": f"产品{index:03d}"} for index in range(1, 218)]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["商品名称"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "共查询到 217 条明细" in answer
    assert "产品001" in answer
    assert "产品217" in answer
    assert answer.count("| 产品") == 217
    assert "当前展示前" not in answer


def test_relationship_projection_is_deduplicated_only_for_answer_display() -> None:
    request = request_for("某商品适用于哪些科室")
    columns = ["科室"]
    rows = [
        {"科室": "外科"},
        {"科室": "麻醉科"},
        {"科室": "外科"},
        {"科室": "麻醉科"},
    ]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        columns,
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "4 条原始关系记录" in answer
    assert "2 个唯一组合" in answer
    assert "原始数据集及证据行数仍为 4" in answer
    assert "| 科室 |" in answer
    assert answer.count("| 外科 |") == 1
    assert answer.count("| 麻醉科 |") == 1
    assert len(rows) == 4


def test_implicit_product_dealer_list_deduplicates_repeated_fact_paths() -> None:
    request = request_for("查询上海市医用外科口罩产品的经销商名单")
    request.entity = "经销商"
    request.fields = ["经销商名称"]
    rows = [{"经销商名称": "上海德昶实业有限公司"} for _ in range(196)]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["经销商名称"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "196 条原始关系记录" in answer
    assert "1 个唯一组合" in answer
    assert answer.count("| 上海德昶实业有限公司 |") == 1
    assert "共查询到 196 条明细" not in answer


def test_identical_transaction_detail_rows_are_not_silently_deduplicated() -> None:
    request = request_for("查询昨天订单明细，显示订单号和金额")
    rows = [
        {"订单号": "A-1", "金额": 100},
        {"订单号": "A-1", "金额": 100},
    ]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["订单号", "金额"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "共查询到 2 条明细" in answer
    assert "唯一组合" not in answer
    assert "| 订单号 | 金额 |" in answer
    assert answer.count("| A-1 | 100 |") == 2


def test_relationship_wording_does_not_deduplicate_order_facts() -> None:
    request = request_for("列出订单关联的商品清单")
    rows = [
        {"订单号": "A-1", "商品": "甲"},
        {"订单号": "A-1", "商品": "甲"},
    ]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["订单号", "商品"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "共查询到 2 条明细" in answer
    assert "唯一组合" not in answer
    assert "| 订单号 | 商品 |" in answer
    assert answer.count("| A-1 | 甲 |") == 2


def test_truncated_relationship_preview_is_never_deduplicated() -> None:
    request = request_for("某商品适用于哪些科室")
    rows = [{"科室": "外科"}, {"科室": "外科"}]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["科室"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
        result_truncated=True,
    )

    assert "共查询到 2 条明细" in answer
    assert "唯一组合" not in answer
    assert answer.count("| 外科 |") == 2


def test_relationship_projection_without_duplicates_keeps_normal_detail_answer() -> None:
    request = request_for("列出某商品关联的供应商名单")
    rows = [{"供应商": "甲"}, {"供应商": "乙"}]

    answer = DataAnalysisOrchestrator._analyze(
        request,
        ["供应商"],
        rows,
        KnowledgeContext(query=request.original_question, documents=[]),
    )

    assert "共查询到 2 条明细" in answer
    assert "唯一组合" not in answer


class RelationshipProjectionRetrieval:
    async def query(
        self, request, identity, *, semantic_model_id, business_domain_id
    ) -> DataQueryResult:
        rows = [
            {"科室": "外科"},
            {"科室": "麻醉科"},
            {"科室": "外科"},
            {"科室": "麻醉科"},
        ]
        return DataQueryResult(
            asl={"subject": {"entity": "product"}},
            sql="SELECT department_name FROM approved_relationship_view",
            data_source_id="source",
            dataset=Dataset(
                columns=["科室"],
                rows=rows,
                row_count=4,
                snapshot_id="relationship-snapshot",
                data_as_of=datetime.now(timezone.utc),
            ),
        )


@pytest.mark.asyncio
async def test_relationship_answer_keeps_query_evidence_at_physical_row_count() -> None:
    adapters = build_mock_adapters()
    adapters = type(adapters)(
        semantic=adapters.semantic,
        retrieval=RelationshipProjectionRetrieval(),
        knowledge=adapters.knowledge,
        policy=adapters.policy,
        analysis=adapters.analysis,
    )
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=adapters,
        sessions=InMemorySessionStore(),
    )

    response = await orchestrator.handle(
        ChatRequest(
            application_id="app",
            conversation_id="relationship",
            message_id="message-1",
            question="某商品适用于哪些科室",
            semantic_model_id=1,
            business_domain_id=1,
        ),
        TrustedIdentity(tenant_id="tenant", user_id="user"),
    )

    query_evidence = next(
        item for item in response.evidence if item.kind == "QUERY_RESULT"
    )
    assert query_evidence.payload["row_count"] == 4
    assert query_evidence.payload["returned_row_count"] == 4
    assert query_evidence.payload["presentation"] == {
        "mode": "UNIQUE_RELATIONSHIP_PROJECTION",
        "original_relationship_row_count": 4,
        "unique_combination_count": 2,
    }
    assert "4 条原始关系记录" in response.answer
    assert "2 个唯一组合" in response.answer


class CapturingDatasetStore:
    def __init__(self) -> None:
        self.kwargs = None

    def save_dataset(self, **kwargs):
        self.kwargs = kwargs

        class Reference:
            dataset_id = "dataset-regression"

            @staticmethod
            def to_dict():
                return {"dataset_id": "dataset-regression"}

        return Reference()


class CapturingSessions(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.reference = None

    async def put_dataset_reference(self, reference, *, recent_limit):
        self.reference = reference


@pytest.mark.asyncio
async def test_query_dataset_persistence_uses_metric_id_field() -> None:
    store = CapturingDatasetStore()
    sessions = CapturingSessions()
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
        dataset_store=store,
    )
    request = request_for("查询本月销售额")
    request.metrics[0].metric_id = "actual_payment_amount"
    result = DataQueryResult(
        asl={"version": "2.0"},
        sql="SELECT 1",
        dataset=Dataset(
            columns=["销售额"],
            rows=[{"销售额": 1}],
            row_count=1,
            snapshot_id="snapshot",
            data_as_of=datetime.now(timezone.utc),
        ),
    )

    dataset_id = await orchestrator._persist_query_dataset(request, result)

    assert dataset_id == "dataset-regression"
    assert store.kwargs["metric_ids"] == ["actual_payment_amount"]
    assert sessions.reference == {"dataset_id": "dataset-regression"}
