from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    AgentResponse, AtomicTask, CanonicalAnalysisRequest, ChatRequest,
    DependencyConstraint, EvidenceItem, PlannerExtraction, PrimaryIntent,
    ReliabilityReport, TaskPlan, TrustedIdentity,
)
from app.planning import MultiQuestionPlanner
from app.planning.dependency_contract import (
    key_kind, missing_display_fields, sql_column_bindings, structured_query_changed,
)
from app.services.orchestrator import DataAnalysisOrchestrator, _INTERNAL_ASSUMPTIONS
from app.stores import InMemorySessionStore
from minio_followup_store import DatasetReference, DatasetScope


@pytest.mark.parametrize("column,kind", [
    ("业务员（编码）", "code"), ("经销商(编号)", "id"),
    ("custom.subject_code", "code"), ("salesperson.salesperson_name", "display"),
    ("订单笔数", "display"), ("医院代码", "code"),
])
def test_dependency_identity_kind(column, kind):
    assert key_kind(column) == kind


@pytest.mark.parametrize("field", ["商品名称", "规格型号", "公司名称", "未知业务属性"])
def test_new_projection_cannot_reuse_ranking(field):
    source = {"指标": [{"name": "销售额"}], "维度": ["业务员"],
              "展示字段": [{"entity": "业务员", "field": "销售员姓名"}]}
    target = {"展示字段": [{"entity": "目标实体", "field": field}]}
    assert structured_query_changed(target, [source])


def test_local_projection_and_changed_time_grain_scope():
    source = {"展示字段": [{"entity": "商品", "field": "商品名称"}],
              "时间粒度": {"time_range": "2025年"}, "过滤条件": []}
    assert not structured_query_changed(source, [source])
    assert structured_query_changed({**source, "时间粒度": {"time_range": "2026年"}}, [source])
    assert structured_query_changed({**source, "过滤条件": [{"field": "状态", "value": "有效"}]}, [source])
    assert structured_query_changed({"展示字段": [{"entity": "医院", "field": "商品名称"}]}, [source])
    assert structured_query_changed({"展示字段": [{"entity": "医院", "field": "名称"}]},
                                    [{"展示字段": [{"field": "名称"}]}])


def test_column_provenance_uses_executed_projection_not_label_inference():
    sql = "SELECT salesperson.salesperson_code AS `业务员（编码）`, SUM(o.amount) AS `销售额` FROM salesperson"
    assert sql_column_bindings(sql, ["业务员（编码）", "销售额"]) == {
        "业务员（编码）": "salesperson.salesperson_code"}
    assert sql_column_bindings(sql, ["不存在"]) == {}


@pytest.mark.parametrize("question", [
    "根据上述任务返回的销售员，查询销售的产品明细清单。",
    "由上一步返回的业务员负责销售的产品有哪些？",
    "查询该销售员的合作医院。",
    "查询上述销售员的商品名称及规格型号。",
])
def test_dependency_reference_aliases(question):
    assert DataAnalysisOrchestrator._dag_references_dependency_result(question)


def test_select_role_key_and_not_numeric_metric():
    cols = ("销售员姓名", "业务员（编码）", "含税销售总额")
    rows = ({cols[0]: "测试姓名", cols[1]: "001", cols[2]: 42},)
    assert DataAnalysisOrchestrator._select_dependency_column(
        "根据上述任务返回的销售员查询产品", cols, rows) == "业务员（编码）"


def constraint():
    values = ["001"]
    return DependencyConstraint(source_task_id="task-1", source_dataset_id="fixture-source",
        source_column="salesperson.salesperson_code", values=values,
        value_fingerprint=hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest())


def test_dependency_filter_enters_both_structured_channels_without_losing_scope():
    request = CanonicalAnalysisRequest(conversation_id="test", tenant_id="t", user_id="u",
        original_question="查产品", primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=81, dependency_constraints=[constraint()])
    request._planner_extraction = PlannerExtraction(structured={
        "展示字段": [{"entity": "商品", "field": "商品名称"}],
        "过滤条件": [{"entity": "医院", "field": "医院名称", "op": "=", "value": ["测试医院"]}],
        "时间粒度": {"unit": "年", "time_range": "2025年"}})
    copy = request.model_copy(deep=True)
    HttpDataRetrievalAdapter._merge_dependency_filters(copy)
    HttpDataRetrievalAdapter._merge_dependency_filters(copy)
    assert len(copy.filters) == 1
    assert len(copy._planner_extraction.structured["过滤条件"]) == 2
    assert copy._planner_extraction.structured["时间粒度"]["time_range"] == "2025年"
    assert len(request._planner_extraction.structured["过滤条件"]) == 1
    HttpDataRetrievalAdapter._validate_dependency_constraints(
        {"filters": copy.filters}, copy)
    for field in ("salesperson.salesperson_name", "hospital.salesperson_code"):
        with pytest.raises(AdapterError):
            HttpDataRetrievalAdapter._validate_dependency_constraints(
                {"filters": [{"field": field, "operator": "IN", "value": ["001"]}]}, copy)


def test_requested_output_guard_rejects_old_table_and_accepts_field_aliases():
    extraction = {"展示字段": [{"entity": "商品", "field": "商品名称"}]}
    assert missing_display_fields(extraction, ["销售员姓名", "含税销售总额"]) == ["商品名称"]
    assert not missing_display_fields(extraction, ["product_name"])


def test_wrong_table_never_finishes_with_high_reliability():
    task = AtomicTask(task_id="task-2", question="查产品", depends_on=["task-1"],
        extraction={"展示字段": [{"entity": "商品", "field": "商品名称"}]})
    response = AgentResponse(request_id=uuid4(), conversation_id="test", status="COMPLETED",
        intent=PrimaryIntent.DETAIL_QUERY, answer="错误的旧表", dataset_id="old-table",
        evidence=[EvidenceItem(evidence_id="q", kind="QUERY_RESULT", source_ref="test",
                              payload={"columns": ["销售员姓名", "销售额"], "row_count": 1})],
        reliability=ReliabilityReport(level="HIGH", score=1, gates={}))
    result = DataAnalysisOrchestrator._validate_task_output(task, response)
    assert result.status == "SAFE_FALLBACK" and result.reliability.level == "FAIL"
    assert result.dataset_id is None and result.result_file_url is None
    assert DataAnalysisOrchestrator._task_missing_output(task, response) == ["商品名称"]


def test_ordering_dependency_is_not_a_business_object_filter():
    assert not DataAnalysisOrchestrator._dag_references_dependency_result("查询上海地区的区域全部医院总数")


@pytest.mark.parametrize("owner,field", [("商品", "商品名称"), ("医院", "医院名称"),
                                        ("经销商", "经销商名称"), ("销售公司", "公司名称")])
def test_bound_child_shape_does_not_inherit_parent_ranking(owner, field):
    request = CanonicalAnalysisRequest(conversation_id="test", tenant_id="t", user_id="u",
        original_question="依赖查询", primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="销售员", fields=["含税销售总额"], dimensions=["销售员"], ranking_limit=1)
    extraction = PlannerExtraction(intent=PrimaryIntent.DETAIL_QUERY, structured={
        "实体": ["销售订单", owner], "指标": [], "维度": [],
        "展示字段": [{"entity": owner, "field": field}],
        "过滤条件": [{"entity": "医院", "field": "医院名称", "op": "=", "value": ["测试医院"]}],
        "限制": None})
    before = request.model_copy(deep=True)
    DataAnalysisOrchestrator._apply_dag_structured_query_shape(request, extraction)
    assert request == before  # no changes outside the bounded DAG path
    token = _INTERNAL_ASSUMPTIONS.set(("DAG_BOUND_STRUCTURED_QUERY",))
    try:
        DataAnalysisOrchestrator._apply_dag_structured_query_shape(request, extraction)
    finally:
        _INTERNAL_ASSUMPTIONS.reset(token)
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == owner and request.fields == [field]
    assert not request.metrics and not request.dimensions and request.ranking_limit is None
    assert request.filters == [{"entity": "医院", "field": "医院名称", "operator": "=", "value": "测试医院"}]


@pytest.mark.asyncio
async def test_fresh_asl_sql_boundary_receives_typed_winner_filter():
    from test_http_adapters import request as adapter_request, StubClient, IDENTITY
    request = adapter_request().model_copy(update={"primary_intent": PrimaryIntent.DETAIL_QUERY,
        "entity": "商品", "fields": ["商品名称"], "metrics": [],
        "dependency_constraints": [constraint()]})
    request._planner_extraction = PlannerExtraction(structured={"指标": [], "维度": [],
        "展示字段": [{"entity": "商品", "field": "商品名称"}], "过滤条件": []})
    asl = {"version": "2.0", "subject": {"entity": "sales_order"}, "metrics": [],
        "dimensions": [{"name": "product.product_name"}], "ambiguity": [],
        "filters": [{"field": "salesperson.salesperson_code", "operator": "IN", "value": ["001"]}]}
    sql = "SELECT DISTINCT p.product_name FROM sales_order o JOIN product p ON p.product_code=o.product_code WHERE o.salesperson_code IN ('001')"
    client = StubClient([{"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": sql},
        {"success": True, "sql": sql, "data": [{"product_name": "测试产品"}],
         "columns": ["product_name"], "row_count": 1}])
    result = await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        request, IDENTITY, semantic_model_id=81, business_domain_id=205)
    assert len(client.calls) == 3 and result.dataset.row_count == 1
    extraction = client.calls[0][2]["structured_extraction"]
    assert extraction["过滤条件"] == [{"entity": "salesperson", "field": "salesperson.salesperson_code",
                                         "op": "in", "value": ["001"]}]
    assert request._planner_extraction.structured["过滤条件"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target_owner,target_field,question", [
    ("商品", "商品名称", "根据上述任务返回的销售员，查询2025年该销售员在测试医院销售的产品明细清单。"),
    ("医院", "医院名称", "根据上述任务返回的销售员，查询2025年合作的医院。"),
    ("经销商", "经销商名称", "查询该销售员2025年的合作经销商。"),
    ("商品", "规格型号", "查询上述销售员2025年销售商品的规格型号。"),
])
async def test_original_and_three_similar_dependent_queries(target_owner, target_field, question):
    settings = Settings(env="test", multi_question_model_enabled=False, analysis_synthesis_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    source_columns = ("销售员姓名", "业务员（编码）", "含税销售总额")
    rows = ({"销售员姓名": "测试销售员", "业务员（编码）": "001", "含税销售总额": 42},)
    class Store:
        def load_dataset(self, reference, *, current_scope):
            assert current_scope == reference.scope
            return SimpleNamespace(reference=reference, rows=rows)
    seen = []
    class Service(DataAnalysisOrchestrator):
        async def _handle(self, child, identity):
            seen.append(child)
            if len(seen) == 1:
                now = datetime.now(timezone.utc)
                reference = DatasetReference(dataset_id="fixture-source", bucket="test",
                    object_name="test/source", scope=DatasetScope(identity.tenant_id, identity.user_id,
                        child.application_id, child.conversation_id, child.authorized_semantic_scope.fingerprint()),
                    columns=source_columns, row_count=1, byte_size=50, snapshot_id="test-snapshot",
                    data_as_of=now.isoformat(), created_at=now.isoformat(),
                    expires_at=(now+timedelta(hours=1)).isoformat(), source_type="DATABASE_QUERY", source_ref="fixture",
                    transformation_log=({"type": "query_provenance", "column_bindings": {
                        "业务员（编码）": "salesperson.salesperson_code"}},))
                await sessions.put_dataset_reference(reference.to_dict(), recent_limit=10)
                columns = list(source_columns)
                dataset_id = reference.dataset_id
            else:
                assert child.dataset_id is None
                assert child._completed_question_execution is True
                assert child.history == []
                assert child.conversation_id != seen[0].conversation_id
                assert child.dependency_constraints[0].source_column == "salesperson.salesperson_code"
                assert child.dependency_constraints[0].values == ["001"]
                assert child._planner_extraction.structured["时间粒度"]["time_range"] == "2025年"
                columns = [target_field]
                dataset_id = None
            return AgentResponse(request_id=uuid4(), conversation_id=child.conversation_id,
                status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
                answer="已返回数据。", dataset_id=dataset_id,
                evidence=[EvidenceItem(evidence_id="query", kind="QUERY_RESULT", source_ref="fixture",
                    payload={"columns": columns, "row_count": 1})],
                reliability=ReliabilityReport(level="HIGH", score=1, gates={"query": True}))
    source = {"维度": ["业务员"], "指标": [{"name": "含税销售总额"}],
              "展示字段": [{"entity": "业务员", "field": "销售员姓名"}],
              "过滤条件": [{"field": "医院名称", "value": ["测试医院"]}],
              "时间粒度": {"unit": "年", "time_range": "2025年"}}
    target = {**source, "维度": [], "指标": [],
              "展示字段": [{"entity": target_owner, "field": target_field}]}
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="查询2025年测试医院销售额最高的销售员", extraction=source),
        AtomicTask(task_id="task-2", question=question, depends_on=["task-1"], extraction=target)])
    service = Service(settings=settings, classifier=SimpleNamespace(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings), dataset_store=Store())
    result = await service._handle_task_plan(ChatRequest(semantic_model_id=81, application_id="app",
        conversation_id="original", message_id="m1", question="测试排名及其明细"),
        TrustedIdentity(tenant_id="t", user_id="u"), plan)
    assert len(seen) == 2
    assert result.task_results[1].status == "COMPLETED"
    assert any(item.kind == "DEPENDENCY_CONSTRAINT" for item in result.evidence)
