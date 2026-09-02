import asyncio
import hashlib
import json
import pytest
from app.adapters.base import AdapterError
from app.adapters.http import (
    HttpDataRetrievalAdapter,
    HttpKnowledgeAdapter,
    HttpSemanticAdapter,
    PlatformHttpClient,
)
from app.config import Settings
from datetime import date, datetime, timezone
from app.domain.models import AnalysisOperator, CanonicalAnalysisRequest, Dataset, DependencyConstraint, MetricRef, PrimaryIntent, TimeRange, TrustedIdentity
from app.services.knowledge_retrieval import RedisKnowledgeSearchCache
from app.services.relationship_projection import (
    requires_distinct_relationship_projection,
)

IDENTITY = TrustedIdentity(tenant_id="t1", user_id="u1")

class StubClient:
    def __init__(self, responses): self.responses, self.calls = iter(responses), []
    async def post(self, base_url, path, payload, **kwargs):
        self.calls.append((base_url, path, payload, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ContractClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def openapi_has_paths(self, base_url, required_paths):
        self.calls.append((base_url, required_paths))
        return next(self.results)


@pytest.mark.asyncio
async def test_live_metric_discovery_accepts_sql_verified_published_metric():
    asl = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "department"},
        "metrics": [{
            "name": "hospital_affiliated_department_count",
            "alias": "科室数量",
        }],
        "dimensions": [],
        "filters": [{
            "field": "hospital.hospital_name",
            "operator": "=",
            "value": "上海市皮肤病医院",
        }],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }
    raw_asl = json.dumps(asl, ensure_ascii=False, separators=(",", ":"))
    evidence = {
        "evidence_version": "1.0",
        "producer": "OAGNET",
        "semantic_model_id": 81,
        "requested_business_domain_ids": [205],
        "resolved_business_domain_ids": [205],
        "selected_metrics": [{
            "canonical_code": "hospital_affiliated_department_count",
            "canonical_name": "医院下属科室数量",
            "semantic_model_id": 81,
            "business_domain_id": 205,
            "calculation_formula": (
                "hospital_affiliated_department_count="
                "COUNT(DISTINCT department.dept_code)"
            ),
            "metadata_source": "MYSQL_SEMANTIC_LAYER",
            "sql_verified": True,
        }],
        "asl_signature": "sha256:" + hashlib.sha256(
            raw_asl.encode("utf-8")
        ).hexdigest(),
    }
    evidence["evidence_fingerprint"] = (
        HttpDataRetrievalAdapter._semantic_evidence_fingerprint(evidence)
    )
    client = StubClient([{
        "success": True,
        "result": raw_asl,
        "semantic_evidence": evidence,
    }])
    req = CanonicalAnalysisRequest(
        conversation_id="live-metric-discovery",
        tenant_id="t1",
        user_id="u1",
        original_question="查询上海市皮肤病医院有多少个科室",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        business_domain_ids=[205],
    )

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).discover_metrics(
        req,
        IDENTITY,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert [metric.metric_id for metric in result.metrics] == [
        "81:hospital_affiliated_department_count"
    ]
    assert result.metrics[0].canonical_name == "医院下属科室数量"
    assert result.time_independent_snapshot is True
    assert result.subject == "department"
    assert result.filters == ({
        "field": "hospital.hospital_name",
        "operator": "=",
        "value": "上海市皮肤病医院",
    },)
    assert result.dimensions == ()


@pytest.mark.asyncio
async def test_live_metric_discovery_rejects_vector_only_metric_hit():
    raw_asl = json.dumps({
        "subject": {"entity": "department"},
        "metrics": [{"name": "stale_metric"}],
        "time_context": None,
    }, separators=(",", ":"))
    evidence = {
        "evidence_version": "1.0",
        "producer": "OAGNET",
        "semantic_model_id": 81,
        "requested_business_domain_ids": [],
        "resolved_business_domain_ids": [],
        "selected_metrics": [{
            "canonical_code": "stale_metric",
            "canonical_name": "旧指标",
            "semantic_model_id": 81,
            "calculation_formula": "stale_metric=COUNT(*)",
            "metadata_source": "VECTOR_INDEX_FALLBACK",
            "sql_verified": False,
        }],
        "asl_signature": "sha256:" + hashlib.sha256(
            raw_asl.encode("utf-8")
        ).hexdigest(),
    }
    evidence["evidence_fingerprint"] = (
        HttpDataRetrievalAdapter._semantic_evidence_fingerprint(evidence)
    )
    client = StubClient([{
        "success": True,
        "result": raw_asl,
        "semantic_evidence": evidence,
    }])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).discover_metrics(
        request(), IDENTITY, semantic_model_id=81, business_domain_id=None
    )

    assert result.metrics == []


@pytest.mark.asyncio
async def test_attribute_detail_discovery_accepts_published_metricless_projection():
    asl = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "device_inspection_data"},
        "metrics": [],
        "dimensions": [
            {"name": "device_daily.detection_value"},
            {"name": "device_daily.detection_unit"},
        ],
        "filters": [
            {"field": "device_daily.device_name", "operator": "=", "value": "卧式成缆1"},
            {"field": "device_daily.model_name", "operator": "=", "value": "摇篮2#3150盘径"},
        ],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [{
            "type": "metric", "question": "指标目录为空", "candidates": [],
        }],
    }
    raw_asl = json.dumps(asl, ensure_ascii=False, separators=(",", ":"))
    evidence = {
        "evidence_version": "1.0",
        "producer": "OAGNET",
        "semantic_model_id": 85,
        "requested_business_domain_ids": [217],
        "resolved_business_domain_ids": [217],
        "selected_metrics": [],
        "asl_signature": "sha256:" + hashlib.sha256(
            raw_asl.encode("utf-8")
        ).hexdigest(),
    }
    evidence["evidence_fingerprint"] = (
        HttpDataRetrievalAdapter._semantic_evidence_fingerprint(evidence)
    )
    client = StubClient([{
        "success": True,
        "result": raw_asl,
        "semantic_evidence": evidence,
        "asl_repair": [],
    }])
    req = CanonicalAnalysisRequest(
        conversation_id="attribute-detail-discovery",
        tenant_id="t1",
        user_id="u1",
        original_question="卧式成缆1的摇篮2#3150盘径近两个月的检测值",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        business_domain_ids=[217],
    )

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).discover_attribute_details(
        req,
        IDENTITY,
        semantic_model_id=85,
        business_domain_id=217,
    )

    assert result.metrics == []
    assert result.subject == "device_inspection_data"
    assert result.dimensions == (
        "detection_value", "detection_unit",
    )
    assert result.filters == (
        {"field": "device_daily.device_name", "operator": "=", "value": "卧式成缆1"},
        {"field": "device_daily.model_name", "operator": "=", "value": "摇篮2#3150盘径"},
    )
    assert client.calls[0][2]["metricless_projection"] is True


def test_sql_relationship_graph_rejects_unjoined_bridge_reference():
    sql = (
        "SELECT COUNT(DISTINCT department.dept_code) FROM department "
        "LEFT JOIN hospital ON hospital.hospital_id = "
        "hospital_dept_relation.hospital_id "
        "WHERE hospital.hospital_name = '测试医院'"
    )

    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._validate_sql_relationship_graph(sql)

    assert exc.value.code == "SQL_RELATIONSHIP_GRAPH_INCOMPLETE"
    assert exc.value.details == {"missing_tables": ["hospital_dept_relation"]}


def test_sql_relationship_graph_accepts_declared_bridge_relation():
    HttpDataRetrievalAdapter._validate_sql_relationship_graph(
        "SELECT COUNT(DISTINCT department.dept_code) FROM department "
        "LEFT JOIN hospital_dept_relation ON "
        "hospital_dept_relation.dept_code = department.dept_code "
        "LEFT JOIN hospital ON hospital.hospital_id = "
        "hospital_dept_relation.hospital_id"
    )


def test_http_error_code_reads_nested_fastapi_detail() -> None:
    import httpx

    response = httpx.Response(
        502,
        json={"success": False, "detail": {"code": "ASL_OUTPUT_INVALID"}},
    )
    assert PlatformHttpClient._upstream_error_code(response) == "ASL_OUTPUT_INVALID"


def test_http_error_code_uses_stable_status_fallback() -> None:
    import httpx

    response = httpx.Response(500, json={"success": False, "error": "private details"})
    assert PlatformHttpClient._upstream_error_code(response) == "HTTP_500"


@pytest.mark.asyncio
async def test_report_retrieval_preserves_original_sales_semantics_only():
    report = CanonicalAnalysisRequest(
        conversation_id="generic-sales-report",
        tenant_id="t1",
        user_id="u1",
        original_question="我在上海卖外周插管中心静脉导管，给我生成分析报告",
        rewritten_question=(
            "生成分析报告；对象：产品；过滤条件："
            "地区 EQ 上海市，商品名称 EQ 外周插管中心静脉导管"
        ),
        primary_intent=PrimaryIntent.REPORT_GENERATION,
        entity="产品",
        filters=[
            {"field": "地区", "operator": "EQ", "value": "上海市"},
            {
                "field": "商品名称",
                "operator": "EQ",
                "value": "外周插管中心静脉导管",
            },
        ],
    )
    client = StubClient([{"success": False}])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(report, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert exc.value.code == "ASL_GENERATION_FAILED"
    payload = client.calls[0][2]
    assert report.original_question in payload["retrieval_query"]
    assert report.original_question not in payload["query"]
    assert payload["query"].startswith(report.rewritten_question)

def request():
    return CanonicalAnalysisRequest(conversation_id="c1", tenant_id="t1", user_id="u1", original_question="不同会员等级的客单价是多少？", primary_intent=PrimaryIntent.METRIC_QUERY)


def test_semantic_entity_mention_accepts_source_backed_canonical_repair():
    req = request().model_copy(update={
        "semantic_entity_mentions": ["费森尤斯"],
    })
    asl = {
        "filters": [{
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "费森尤斯",
        }],
    }

    HttpDataRetrievalAdapter._validate_semantic_entity_mentions(
        asl,
        req,
        [{
            "type": "ADD_SOURCE_RESOLVED_ENTITY_FILTER",
            "mention": "费森尤斯",
            "canonical_value": "费森尤斯",
            "resolved_field": "manufacturer.parent_brand",
        }],
    )


def test_semantic_entity_mention_rejects_missing_source_binding_proof():
    req = request().model_copy(update={
        "semantic_entity_mentions": ["费森尤斯"],
    })

    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._validate_semantic_entity_mentions(
            {"filters": []}, req, [],
        )

    assert exc.value.code == "ASL_ENTITY_MENTION_UNRESOLVED"


@pytest.mark.asyncio
async def test_contextual_entity_mention_is_preserved_for_current_semantic_recall():
    req = CanonicalAnalysisRequest(
        conversation_id="contextual-brand-recall",
        tenant_id="t1",
        user_id="u1",
        original_question="那费森尤斯呢",
        rewritten_question=(
            "查询明细；对象：经销商；返回字段：经销商名称；分析维度：经销商"
        ),
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        dimensions=["经销商"],
        semantic_entity_mentions=["费森尤斯"],
    )
    client = StubClient([{"success": False}])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(req, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert exc.value.code == "ASL_GENERATION_FAILED"
    retrieval_query = client.calls[0][2]["retrieval_query"]
    assert "费森尤斯" in retrieval_query
    assert "当前语义层维度及关系" in retrieval_query
    assert all(
        label in retrieval_query
        for label in ("品牌", "母厂牌", "生产厂家", "商品品类")
    )


def test_required_non_null_name_filter_accepts_current_semantic_field():
    required = CanonicalAnalysisRequest(
        conversation_id="non-null-filter",
        tenant_id="t1",
        user_id="u1",
        original_question="查询医院名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
        assumptions=["REQUIRED_NAME_NON_NULL=医院名称"],
    )

    HttpDataRetrievalAdapter._validate_request_filters(
        {
            "filters": [{
                "field": "hospital.hospital_name",
                "operator": "IS NOT NULL",
                "value": None,
            }]
        },
        required,
    )


def test_required_non_null_name_filter_cannot_be_dropped():
    required = CanonicalAnalysisRequest(
        conversation_id="missing-non-null-filter",
        tenant_id="t1",
        user_id="u1",
        original_question="查询医院名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
        assumptions=["REQUIRED_NAME_NON_NULL=医院名称"],
    )

    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._validate_request_filters(
            {"filters": []}, required
        )

    assert exc.value.code == "ASL_REQUIRED_NAME_NON_NULL_MISSING"


def test_required_non_null_name_filter_is_bound_to_projected_semantic_field():
    required = CanonicalAnalysisRequest(
        conversation_id="bind-non-null-filter",
        tenant_id="t1",
        user_id="u1",
        original_question="查询医院名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
        assumptions=["REQUIRED_NAME_NON_NULL=医院名称"],
    )
    asl = {
        "dimensions": [{
            "name": "hospital.hospital_name",
            "alias": "医院名称",
        }],
        "filters": [],
    }

    HttpDataRetrievalAdapter._ensure_required_name_non_null_filters(
        asl, required
    )

    assert asl["filters"] == [{
        "field": "hospital.hospital_name",
        "operator": "!=",
        "value": "",
    }]
    HttpDataRetrievalAdapter._validate_request_filters(asl, required)


def dependency_constraint(values=None):
    values = values or ["超声科", "麻醉科"]
    canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return DependencyConstraint(
        source_task_id="task-1",
        source_dataset_id="dataset-upstream",
        source_column="dept_name",
        values=values,
        value_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


@pytest.mark.asyncio
async def test_dependency_constraint_rejects_unfiltered_asl_before_sql_translation():
    constrained = request().model_copy(update={
        "primary_intent": PrimaryIntent.DETAIL_QUERY,
        "entity": "经销商",
        "fields": ["经销商名称"],
        "dependency_constraints": [dependency_constraint()],
    })
    client = StubClient([{
        "success": True,
        "result": json.dumps({
            "version": "2.0",
            "subject": {"entity": "dealer_result"},
            "metrics": [],
            "dimensions": [{"name": "dealer_result.dealer_name"}],
            "filters": [],
            "ambiguity": [],
        }, ensure_ascii=False),
    }])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            constrained, IDENTITY, semantic_model_id=81, business_domain_id=205
        )

    assert exc.value.code == "ASL_DEPENDENCY_CONSTRAINT_MISSING"
    assert len(client.calls) == 1
    assert "禁止省略" in client.calls[0][2]["query"]


@pytest.mark.asyncio
async def test_dependency_constraint_allows_exact_in_filter_and_executes_sql():
    constraint = dependency_constraint()
    constrained = request().model_copy(update={
        "primary_intent": PrimaryIntent.DETAIL_QUERY,
        "entity": "经销商",
        "fields": ["经销商名称"],
        "dependency_constraints": [constraint],
    })
    asl = {
        "version": "2.0",
        "subject": {"entity": "dealer_result"},
        "metrics": [],
        "dimensions": [{"name": "dealer_result.dealer_name"}],
        "filters": [{
            "field": "department.dept_name",
            "operator": "IN",
            "value": constraint.values,
        }],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {"success": True, "sql": "SELECT dealer_name FROM dealer_result WHERE dept_name IN (?, ?)"},
        {
            "success": True,
            "sql": "SELECT dealer_name FROM dealer_result WHERE dept_name IN (?, ?)",
            "data": [{"dealer_name": "经销商A"}],
            "columns": ["dealer_name"],
            "row_count": 1,
        },
    ])

    result = await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        constrained, IDENTITY, semantic_model_id=81, business_domain_id=205
    )

    assert result.dataset.row_count == 1
    assert [call[1] for call in client.calls] == ["/agent/query", "/api/translate", "/api/execute"]
    assert "dept_name" in client.calls[0][2]["retrieval_query"]
    assert "Internal DAG semantic retrieval requirement" in client.calls[0][2]["retrieval_query"]
    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["projection_mode"] == "DISTINCT"


def test_dependency_constraint_field_family_keeps_join_key_kind() -> None:
    assert HttpDataRetrievalAdapter._constraint_field_matches(
        "department.dept_code", "product_dept_relation.dept_code"
    )
    assert not HttpDataRetrievalAdapter._constraint_field_matches(
        "department.dept_code", "department.dept_name"
    )
    assert not HttpDataRetrievalAdapter._constraint_field_matches(
        "department.dept_name", "product_dept_relation.dept_code"
    )


@pytest.mark.asyncio
async def test_bound_metric_ids_are_sent_and_asl_cannot_switch_metric():
    bound = request().model_copy(update={
        "metrics": [MetricRef(input="客单价", metric_id="8:average_transaction_value")]
    })
    client = StubClient([{
        "success": True,
        "result": json.dumps({
            "version": "2.0", "metrics": [{"name": "sales_amount"}], "ambiguity": [],
        }),
    }])
    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            bound, IDENTITY, semantic_model_id=8, business_domain_id=13
        )
    assert exc.value.code == "ASL_METRIC_SELECTION_INVALID"
    assert client.calls[0][2]["metric_ids"] == ["8:average_transaction_value"]
    assert len(client.calls) == 1


def test_detail_field_validation_accepts_registered_physical_names():
    missing = HttpDataRetrievalAdapter._missing_detail_fields(
        ["商品名称", "商品分类", "供应商名称", "联系人手机号"],
        [
            {"name": "goods_info.goods_name"},
            {"name": "goods_info.goods_category"},
            {"name": "supplier_info.supplier_name"},
            {"name": "supplier_info.contact_phone"},
        ],
    )

    assert missing == []


def test_detail_field_validation_accepts_model81_order_key():
    missing = HttpDataRetrievalAdapter._missing_detail_fields(
        ["订单号", "金额"],
        [
            {"name": "sales_order.order_key"},
            {"name": "sales_order.amount_with_tax"},
        ],
    )

    assert missing == []


def test_detail_field_validation_accepts_dealer_email_and_address_columns():
    missing = HttpDataRetrievalAdapter._missing_detail_fields(
        ["经销商名称", "邮箱", "地址"],
        [
            {"name": "dealer_result.dealer_name"},
            {"name": "dealer_result.email"},
            {"name": "dealer_result.address"},
        ],
    )

    assert missing == []


def test_detail_field_validation_accepts_registered_hospital_name_column():
    missing = HttpDataRetrievalAdapter._missing_detail_fields(
        ["医院名称"],
        [{"name": "hospital.hospital_name"}],
    )

    assert missing == []


@pytest.mark.parametrize(
    ("field", "logical_dimension"),
    (
        ("经销商名称", "dealer"),
        ("医院名称", "hospital"),
        ("商品名称", "product"),
        ("厂家名称", "manufacturer"),
    ),
)
def test_detail_field_validation_accepts_registered_logical_dimensions(
    field, logical_dimension,
):
    assert HttpDataRetrievalAdapter._missing_detail_fields(
        [field], [{"name": logical_dimension}],
    ) == []


def test_detail_retrieval_query_expands_related_entities_without_physical_names():
    detail = CanonicalAnalysisRequest(
        conversation_id="detail-relation-recall",
        tenant_id="t1",
        user_id="u1",
        original_question="提供上海地区做BD品牌产品的经销商及联系方式",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称", "邮箱", "地址"],
    )

    query = HttpDataRetrievalAdapter._detail_retrieval_query(
        detail, detail.original_question
    )

    assert "经销商画像" in query
    assert all(value in query for value in ("产品", "商品", "品牌", "邮箱", "地址"))


def test_detail_execution_wording_disambiguates_coverage_list_from_count_metric():
    detail = CanonicalAnalysisRequest(
        conversation_id="hospital-coverage-wording",
        tenant_id="t1",
        user_id="u1",
        original_question="列出上海市某产品医院覆盖明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
    )

    rendered = HttpDataRetrievalAdapter._detail_semantic_query(
        detail, detail.original_question
    )

    assert rendered == (
        "列出上海市某产品覆盖到的医院名称明细；"
        "逐行明细必须返回字段：医院名称"
    )
    assert "医院覆盖" not in rendered


def test_detail_retrieval_query_expands_catalog_semantics_for_consumable_filter():
    detail = CanonicalAnalysisRequest(
        conversation_id="detail-catalog-recall",
        tenant_id="t1",
        user_id="u1",
        original_question=(
            "查询上海市江苏苏云品牌低值耗材的经销商清单，"
            "并附带经销商对应销售总额"
        ),
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        dimensions=["经销商", "城市", "商品品牌", "商品品类"],
        filters=[
            {"field": "城市", "operator": "EQ", "value": "上海市"},
            {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
            {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
        ],
    )

    query = HttpDataRetrievalAdapter._detail_retrieval_query(
        detail, detail.original_question
    )

    assert all(
        value in query
        for value in (
            "城市", "商品品牌", "商品品类",
            "商品分类", "产品分类", "品类", "类目", "耗材",
        )
    )
    assert "dealer_result" not in query


def test_transaction_active_partner_recall_includes_catalog_and_registered_time_semantics():
    detail = CanonicalAnalysisRequest(
        conversation_id="transaction-active-recall",
        tenant_id="t1",
        user_id="u1",
        original_question="近一年内，哪些活跃经销商在销售费森尤斯产品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        assumptions=[
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
        ],
    )

    query = HttpDataRetrievalAdapter._detail_retrieval_query(
        detail, detail.original_question
    )

    assert all(
        value in query
        for value in (
            "品牌", "制造商", "生产厂家", "销售记录",
            "交易日期", "销售日期", "订单日期",
        )
    )
    assert "sales_order.created_date" not in query


def test_time_ranged_relationship_detail_recall_includes_transaction_time_semantics():
    detail = CanonicalAnalysisRequest(
        conversation_id="report-hospital-coverage-recall",
        tenant_id="t1",
        user_id="u1",
        original_question="列出上海市某产品医院覆盖明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
        filters=[
            {"field": "商品名称", "operator": "EQ", "value": "某产品"}
        ],
        time_range=TimeRange(
            start=date(2025, 10, 17),
            end_exclusive=date(2025, 12, 31),
        ),
        assumptions=["TRANSACTION_TIME_SCOPE=SALES_RECORD"],
    )

    # The report splitter rewrites coverage children into a generic detail
    # shape; the original question must still preserve the sales relationship.
    rewritten = "查询明细；对象：医院；返回字段：医院名称"
    query = HttpDataRetrievalAdapter._detail_retrieval_query(detail, rewritten)

    assert all(
        value in query
        for value in (
            "销售记录", "交易日期", "销售日期", "订单日期",
            "TRANSACTION_TIME_SCOPE=SALES_RECORD",
        )
    )
    assert "sales_order.created_date" not in query


def test_non_sales_detail_time_range_does_not_invent_transaction_semantics():
    detail = CanonicalAnalysisRequest(
        conversation_id="employee-created-range",
        tenant_id="t1",
        user_id="u1",
        original_question="列出近一年入职的销售人员姓名",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="销售人员",
        fields=["销售人员姓名"],
        time_range=TimeRange(
            start=date(2025, 1, 1),
            end_exclusive=date(2026, 1, 1),
        ),
    )

    query = HttpDataRetrievalAdapter._detail_retrieval_query(
        detail, detail.original_question
    )

    assert all(
        value not in query
        for value in (
            "销售记录", "销售订单", "订单", "交易", "交易日期", "销售日期", "订单日期",
            "TRANSACTION_TIME_SCOPE=SALES_RECORD",
        )
    )
    assert not HttpDataRetrievalAdapter._detail_time_uses_sales_records(
        detail, detail.original_question
    )


@pytest.mark.asyncio
async def test_ranked_partner_sales_record_range_uses_transaction_time_scope():
    question = (
        "帮我找出上海地区正在销售振德医疗品牌的医用外科口罩产品的"
        "经销商名单，并按整体业务规模排序；按2025年10月17日至"
        "2025年12月30日期间有销售记录的经销商筛选"
    )
    ranked = CanonicalAnalysisRequest(
        conversation_id="ranked-sales-record-range",
        tenant_id="t1",
        user_id="u1",
        original_question=question,
        rewritten_question=question,
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        metrics=[MetricRef(input="整体业务规模")],
        entity="经销商",
        dimensions=["经销商"],
        filters=[
            {"field": "地区", "operator": "EQ", "value": "上海市"},
            {"field": "品牌名称", "operator": "EQ", "value": "振德医疗"},
            {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
        ],
        time_range=TimeRange(
            start=date(2025, 10, 17),
            end_exclusive=date(2025, 12, 31),
        ),
        comparison_type="对象间比较",
    )
    client = StubClient([{"success": False}])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(ranked, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert exc.value.code == "ASL_GENERATION_FAILED"
    payload = client.calls[0][2]
    assert "TRANSACTION_TIME_SCOPE=SALES_RECORD" in payload["retrieval_query"]
    assert all(
        value in payload["retrieval_query"]
        for value in ("销售记录", "交易日期", "销售日期", "订单日期")
    )
    assert "已注册销售交易时间维度" in payload["query"]


@pytest.mark.asyncio
async def test_manufacturer_exclusion_recalls_current_registered_name_attributes():
    ranked = CanonicalAnalysisRequest(
        conversation_id="ranked-manufacturer-exclusion",
        tenant_id="t1",
        user_id="u1",
        original_question=(
            "查询上海市医用外科口罩产品的经销商，排除上海洁安厂家，"
            "并按整体业务规模排序"
        ),
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="整体业务规模")],
        entity="经销商",
        dimensions=["经销商"],
        filters=[
            {"field": "厂家名称", "operator": "NE", "value": "上海洁安"},
        ],
    )
    client = StubClient([{"success": False}])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(ranked, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert exc.value.code == "ASL_GENERATION_FAILED"
    payload = client.calls[0][2]
    assert "manufacturer.manufacturer_name" in payload["retrieval_query"]
    assert "名称属性进行精确名称过滤" in payload["query"]


@pytest.mark.asyncio
async def test_dealer_recommendation_uses_relationship_aware_retrieval_scope():
    metric_codes = (
        "dealer_recent_year_sales",
        "dealer_growth_rate_3m",
        "dealer_cooperation_months",
        "dealer_cooperation_count",
    )
    recommendation = CanonicalAnalysisRequest(
        conversation_id="dealer-recommendation-recall",
        tenant_id="t1",
        user_id="u1",
        original_question=(
            "针对一次性使用医用外科口罩产品及对应科室，"
            "上海仅保留三级医院渠道，输出 TOP3 经销商"
        ),
        rewritten_question=(
            "针对一次性使用医用外科口罩产品及对应科室，"
            "上海仅保留三级医院渠道，输出 TOP3 经销商"
        ),
        primary_intent=PrimaryIntent.COMPARISON_ANALYSIS,
        comparison_type="对象间比较",
        dimensions=["经销商"],
        metrics=[
            MetricRef(input=code, metric_id=f"81:{code}")
            for code in metric_codes
        ],
        ranking_limit=3,
        assumptions=[
            "DEALER_RECOMMENDATION_DEFAULT_RANKING=经销商近一年销售额"
        ],
    )
    client = StubClient([{
        "success": True,
        "result": json.dumps({
            "version": "2.0",
            "metrics": [{"name": code} for code in metric_codes],
            "dimensions": [{"name": "dealer.dealer_name"}],
            "ambiguity": [{
                "type": "filter",
                "question": "test stop",
                "candidates": [],
            }],
        }, ensure_ascii=False),
    }])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(
            recommendation,
            IDENTITY,
            semantic_model_id=81,
            business_domain_id=205,
        )

    assert exc.value.code == "ASL_AMBIGUOUS"
    payload = client.calls[0][2]
    assert all(
        value in payload["retrieval_query"]
        for value in ("产品", "商品", "科室", "部门", "医院", "经销商")
    )
    assert "沿注册路径执行" in payload["query"]
    assert "dimensions 必须" not in payload["retrieval_query"]


@pytest.mark.asyncio
async def test_relation_detail_sends_relationship_aware_projection_contract():
    detail = CanonicalAnalysisRequest(
        conversation_id="detail-related-field",
        tenant_id="t1",
        user_id="u1",
        original_question="超声血管导引穿刺套件适用于哪些科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="产品",
        fields=["商品名称", "适用科室"],
    )
    asl = {
        "version": "2.0",
        "subject": {"entity": "product"},
        "metrics": [],
        "dimensions": [
            {"name": "product.product_name"},
            {"name": "department.dept_name"},
        ],
        "filters": [],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {
            "success": True,
            "sql": "SELECT product_name, dept_name FROM product JOIN department",
        },
        {
            "success": True,
            "sql": "SELECT product_name, dept_name FROM product JOIN department",
            "data": [{"product_name": "穿刺套件", "dept_name": "超声科"}],
            "columns": ["product_name", "dept_name"],
            "row_count": 1,
        },
    ])

    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        detail, IDENTITY, semantic_model_id=81, business_domain_id=205
    )

    payload = client.calls[0][2]
    assert "沿召回的语义关系路径" in payload["query"]
    assert "不得用主体上的摘要字段" in payload["query"]
    assert all(
        value in payload["retrieval_query"]
        for value in ("产品", "商品", "科室", "部门")
    )
    assert "product.main_department" not in payload["query"]
    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["projection_mode"] == "DISTINCT"


def test_transaction_detail_remains_row_shaped() -> None:
    detail = CanonicalAnalysisRequest(
        conversation_id="transaction-rows",
        tenant_id="t1",
        user_id="u1",
        original_question="查询订单关联的商品明细，逐笔显示订单号和商品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="订单",
        fields=["订单号", "商品名称"],
    )

    assert requires_distinct_relationship_projection(detail) is False


def test_dependency_filtered_object_list_is_set_shaped() -> None:
    detail = CanonicalAnalysisRequest(
        conversation_id="dag-set",
        tenant_id="t1",
        user_id="u1",
        original_question="根据适用科室筛选出经销商",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称", "适用科室"],
        dependency_constraints=[dependency_constraint()],
    )

    assert requires_distinct_relationship_projection(detail) is True


def test_product_dealer_list_is_set_shaped_without_explicit_relationship_word() -> None:
    detail = CanonicalAnalysisRequest(
        conversation_id="implicit-product-dealer-relation",
        tenant_id="t1",
        user_id="u1",
        original_question="查询上海市医用外科口罩产品的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
    )

    assert requires_distinct_relationship_projection(detail) is True


def test_product_applicable_department_is_set_shaped_without_list_word() -> None:
    detail = CanonicalAnalysisRequest(
        conversation_id="implicit-product-department-relation",
        tenant_id="t1",
        user_id="u1",
        original_question="查询超声血管导引穿刺套件适用的科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="产品",
        fields=["商品名称", "适用科室"],
    )

    assert requires_distinct_relationship_projection(detail) is True


def test_named_dealer_product_lookup_is_set_shaped() -> None:
    detail = CanonicalAnalysisRequest(
        conversation_id="implicit-dealer-product-relation",
        tenant_id="t1",
        user_id="u1",
        original_question="查询最近一年上海东松医疗科技股份有限公司销售过的产品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="产品",
        fields=["产品名称"],
    )

    assert requires_distinct_relationship_projection(detail) is True


def test_canonical_time_range_rebinds_asl_end_as_inclusive_date():
    scoped = request().model_copy(update={
        "time_range": TimeRange(
            start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1)
        )
    })
    asl = {
        "time_context": {
            "type": "custom", "start": "2026-08-01", "end": "2026-09-01",
            "anchor": "order_info.pay_time",
        }
    }

    HttpDataRetrievalAdapter._bind_canonical_time_range(asl, scoped)

    assert asl["time_context"]["start"] == "2026-08-01"
    assert asl["time_context"]["end"] == "2026-08-31"


@pytest.mark.asyncio
async def test_precomputed_metric_window_cannot_add_fact_time_scope():
    metric_request = request().model_copy(update={
        "primary_intent": PrimaryIntent.COMPARISON_ANALYSIS,
        "comparison_type": "对象间比较",
        "dimensions": ["经销商"],
        "metrics": [
            MetricRef(
                input="近三月业绩增长率",
                metric_id="81:dealer_growth_rate_3m",
            ),
            MetricRef(
                input="合作时长",
                metric_id="81:dealer_cooperation_months",
            ),
        ],
        "assumptions": ["METRIC_WINDOW_IS_DEFINITION=近三月业绩增长率"],
    })
    generated_asl = {
        "version": "2.0",
        "subject": {"entity": "dealer_profile"},
        "metrics": [
            {"name": "dealer_growth_rate_3m"},
            {"name": "dealer_cooperation_months"},
        ],
        "dimensions": [
            {"name": "dealer.dealer_name"},
            {"name": "sales_order.created_date", "granularity": "hour"},
        ],
        "filters": [
            {"field": "dealer.province", "operator": "=", "value": "上海"},
            {
                "field": "sales_order.created_date",
                "operator": ">=",
                "value": "2026-05-28",
            },
        ],
        "time_context": {
            "type": "range",
            "start": "2026-05-28",
            "end": "2026-08-28",
            "anchor": "sales_order.created_date",
        },
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(generated_asl, ensure_ascii=False)},
        {
            "success": True,
            "sql": "SELECT dealer_name, growth_rate, cooperation_months FROM dealer_result",
        },
        {
            "success": True,
            "sql": "SELECT dealer_name, growth_rate, cooperation_months FROM dealer_result",
            "data": [
                {"dealer_name": "经销商A", "growth_rate": 0.1, "cooperation_months": 24},
                {"dealer_name": "经销商B", "growth_rate": 0.08, "cooperation_months": 18},
            ],
            "columns": ["dealer_name", "growth_rate", "cooperation_months"],
            "row_count": 2,
        },
    ])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(
        metric_request,
        IDENTITY,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert result.dataset.row_count == 2
    assert "time_context必须为null" in client.calls[0][2]["query"]
    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["time_context"] is None
    assert translated_asl["dimensions"] == [{"name": "dealer.dealer_name"}]
    assert translated_asl["filters"] == [
        {"field": "dealer.province", "operator": "=", "value": "上海"}
    ]


@pytest.mark.asyncio
async def test_semantic_metric_binding_must_match_requested_model_scope():
    client = StubClient([{
        "status": "RESOLVED",
        "metrics": [{
            "input": "销售额", "metric_id": "9:sales_amount",
            "version": "current", "canonical_name": "销售额",
        }],
    }])
    metric_request = request().model_copy(update={"metrics": [MetricRef(input="销售额")]})
    with pytest.raises(AdapterError, match="unscoped metric binding"):
        await HttpSemanticAdapter(Settings(adapter_mode="http"), client).resolve_metrics(
            metric_request, 8
        )


@pytest.mark.asyncio
async def test_time_only_followup_reuses_verified_asl_without_oagnet_call():
    template = {
        "version": "2.0",
        "subject": {"entity": "ent_order"},
        "metrics": [{"name": "sales_amount", "alias": "销售额"}],
        "dimensions": [],
        "time_context": {
            "type": "custom", "start": "2026-07-01", "end": "2026-07-31",
            "anchor": "order_info.pay_time",
        },
        "ambiguity": [],
    }
    followup = request().model_copy(update={
        "metrics": [MetricRef(input="销售额", metric_id="8:sales_amount")],
        "time_range": TimeRange(start=date(2026, 6, 1), end_exclusive=date(2026, 7, 1)),
        "asl_template": template,
        "assumptions": ["DETERMINISTIC_TIME_FAST_PATH"],
    })
    client = StubClient([
        {"success": True, "sql": "SELECT SUM(pay_amount) FROM order_info"},
        {
            "success": True, "sql": "SELECT SUM(pay_amount) FROM order_info",
            "data": [{"销售额": 100}], "columns": ["销售额"], "row_count": 1,
        },
    ])
    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        followup, IDENTITY, semantic_model_id=8, business_domain_id=13
    )
    assert [call[1] for call in client.calls] == ["/api/translate", "/api/execute"]
    reused = json.loads(client.calls[0][2]["asl"])
    assert reused["time_context"] == {
        "type": "custom", "start": "2026-06-01", "end": "2026-06-30",
        "anchor": "order_info.pay_time",
    }


@pytest.mark.asyncio
async def test_time_only_detail_followup_preserves_verified_brand_filter():
    template = {
        "version": "2.0",
        "subject": {"entity": "sales_order"},
        "metrics": [],
        "dimensions": [{"name": "dealer.dealer_name"}],
        "filters": [{
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "费森尤斯",
        }],
        "time_context": {
            "type": "range", "start": "2025-08-29", "end": "2026-08-29",
            "anchor": "sales_order.created_date",
        },
        "projection_mode": "ROWS",
        "ambiguity": [],
    }
    followup = request().model_copy(update={
        "primary_intent": PrimaryIntent.DETAIL_QUERY,
        "metrics": [],
        "fields": ["经销商名称"],
        "time_range": TimeRange(
            start=date(2025, 12, 1), end_exclusive=date(2026, 1, 1)
        ),
        "asl_template": template,
        "assumptions": ["DETERMINISTIC_TIME_FAST_PATH"],
    })
    client = StubClient([
        {"success": True, "sql": "SELECT dealer_name FROM sales_order"},
        {
            "success": True,
            "sql": "SELECT dealer_name FROM sales_order",
            "data": [{"dealer.dealer_name": "经销商甲"}],
            "columns": ["dealer.dealer_name"],
            "row_count": 1,
        },
    ])

    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        followup, IDENTITY, semantic_model_id=8, business_domain_id=13
    )

    assert [call[1] for call in client.calls] == ["/api/translate", "/api/execute"]
    reused = json.loads(client.calls[0][2]["asl"])
    assert reused["filters"] == template["filters"]
    assert reused["dimensions"] == template["dimensions"]
    assert reused["time_context"] == {
        "type": "custom",
        "start": "2025-12-01",
        "end": "2025-12-31",
        "anchor": "sales_order.created_date",
    }


def test_hospital_level_detail_field_matches_registered_dimension():
    assert HttpDataRetrievalAdapter._missing_detail_fields(
        ["医院等级"], [{"name": "hospital.hospital_level"}]
    ) == []


def test_model81_repairs_explicit_hospital_level_grouping():
    req = request().model_copy(update={"dimensions": ["医院", "医院等级"]})
    asl = {"dimensions": [{"name": "hospital.hospital_name"}]}

    HttpDataRetrievalAdapter._repair_required_model81_dimensions(asl, req, 81)

    assert {item["name"] for item in asl["dimensions"]} == {
        "hospital.hospital_name",
        "hospital.hospital_level",
    }


def test_relationship_identity_dimension_deduplication_prefers_business_relation():
    asl = {
        "dimensions": [
            {"name": "dealer", "alias": "经销商"},
            {"name": "dealer.dealer_name"},
            {"name": "dealer.province", "alias": "省份"},
        ],
        "sort": {
            "field": "dealer.dealer_name",
            "direction": "ASC",
            "field_type": "dimension",
        },
    }

    HttpDataRetrievalAdapter._deduplicate_relationship_identity_dimensions(asl)

    assert asl["dimensions"] == [
        {"name": "dealer", "alias": "经销商"},
        {"name": "dealer.province", "alias": "省份"},
    ]
    assert asl["sort"]["field"] == "dealer"


def test_relationship_identity_dimension_deduplication_keeps_explicit_name_alone():
    asl = {"dimensions": [{"name": "dealer.dealer_name"}]}

    HttpDataRetrievalAdapter._deduplicate_relationship_identity_dimensions(asl)

    assert asl["dimensions"] == [{"name": "dealer.dealer_name"}]


def test_grouped_scope_requires_all_current_semantic_dimension_roles():
    grouped = CanonicalAnalysisRequest(
        conversation_id="semantic-role-contract",
        tenant_id="t1",
        user_id="u1",
        original_question="查询品牌品类经销商清单并显示销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="经销商",
        dimensions=["经销商", "城市", "商品品牌", "商品品类"],
    )
    asl = {
        "dimensions": [
            {"name": "dealer", "alias": "经销商"},
            {"name": "dealer.city", "alias": "城市"},
            {"name": "product.brand", "alias": "商品品牌"},
            {"name": "product_category.product_type", "alias": "商品品类"},
        ]
    }

    HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(asl, grouped)

    asl["dimensions"].pop()
    with pytest.raises(AdapterError) as missing:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(asl, grouped)
    assert missing.value.code == "ASL_REQUIRED_DIMENSION_MISSING"
    assert missing.value.details["missing_dimensions"] == ["商品品类"]


def test_strict_grouped_scope_rejects_an_extra_dimension_that_changes_grain():
    grouped = CanonicalAnalysisRequest(
        conversation_id="strict-hospital-level-grain",
        tenant_id="t1",
        user_id="u1",
        original_question="各医院等级对应的医院数量是多少",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="医院数量")],
        dimensions=["医院等级"],
        assumptions=["STRICT_GROUPING_DIMENSIONS"],
    )

    with pytest.raises(AdapterError) as unexpected:
        HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
            {
                "dimensions": [
                    {"name": "hospital.hospital_level", "alias": "医院等级"},
                    {"name": "hospital.hospital_name", "alias": "医院名称"},
                ]
            },
            grouped,
        )
    assert unexpected.value.code == "ASL_UNREQUESTED_DIMENSION"


def test_grouped_scope_keeps_filter_only_roles_out_of_group_by():
    grouped = CanonicalAnalysisRequest(
        conversation_id="semantic-filter-role-repair",
        tenant_id="t1",
        user_id="u1",
        original_question=(
            "查询上海市江苏苏云品牌低值耗材的经销商清单，"
            "并显示各经销商含税销售总额"
        ),
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="经销商",
        dimensions=["经销商", "城市", "商品品牌", "商品品类"],
        filters=[
            {"field": "城市", "operator": "EQ", "value": "上海市"},
            {
                "field": "商品品牌",
                "operator": "EQ",
                "value": "江苏苏云",
            },
            {
                "field": "商品品类",
                "operator": "EQ",
                "value": "低值耗材",
            },
        ],
    )
    assert HttpDataRetrievalAdapter._required_grouped_dimension_roles(
        grouped
    ) == ["经销商"]
    HttpDataRetrievalAdapter._validate_grouped_semantic_dimensions(
        {"dimensions": [{"name": "dealer", "alias": "经销商"}]},
        grouped,
    )


def test_city_dimension_is_not_satisfied_by_a_province_field():
    assert not HttpDataRetrievalAdapter._semantic_dimension_role_matches(
        "城市", "dim_province.province_name"
    )
    assert HttpDataRetrievalAdapter._semantic_dimension_role_matches(
        "城市", "dim_city.city_name"
    )


def test_brand_category_scope_rejects_an_invented_product_name_filter():
    grouped = CanonicalAnalysisRequest(
        conversation_id="no-synthetic-product",
        tenant_id="t1",
        user_id="u1",
        original_question="查询江苏苏云品牌低值耗材的经销商清单",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[
            {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
            {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
        ],
    )
    valid = {
        "filters": [
            {"field": "manufacturer.parent_brand", "operator": "=", "value": "江苏苏云"},
            {"field": "product_category.product_type", "operator": "=", "value": "低值耗材"},
        ]
    }
    HttpDataRetrievalAdapter._validate_no_synthetic_product_filter(valid, grouped)

    invalid = {"filters": [
        *valid["filters"],
        {
            "field": "product.product_name",
            "operator": "=",
            "value": "江苏苏云品牌低值耗材",
        },
    ]}
    with pytest.raises(AdapterError) as synthetic:
        HttpDataRetrievalAdapter._validate_no_synthetic_product_filter(
            invalid, grouped
        )
    assert synthetic.value.code == "ASL_SYNTHETIC_PRODUCT_FILTER"


def test_geographic_filter_must_share_the_grouped_city_hierarchy():
    trend = CanonicalAnalysisRequest(
        conversation_id="geographic-hierarchy",
        tenant_id="t1",
        user_id="u1",
        original_question="查看2025年安徽省下各个城市每月销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        dimensions=["城市"],
        filters=[{"field": "地区", "operator": "EQ", "value": "安徽省"}],
    )
    asl = {
        "filters": [
            {
                "field": "dim_province.province_name",
                "operator": "=",
                "value": "安徽省",
            }
        ]
    }
    wrong_branch = (
        "SELECT dim_city.city_name AS 城市, "
        "DATE_FORMAT(sales_order.created_date, '%Y-%m') AS 交易月份, "
        "SUM(sales_order.amount_with_tax) AS 含税销售总额 "
        "FROM sales_order "
        "LEFT JOIN dealer ON sales_order.dealer_code = dealer.dealer_code "
        "LEFT JOIN dim_city ON dealer.city_id = dim_city.city_id "
        "LEFT JOIN hospital ON sales_order.hospital_id = hospital.hospital_id "
        "LEFT JOIN dim_province ON hospital.province_id = dim_province.province_id "
        "WHERE dim_province.province_name = '安徽省' "
        "GROUP BY dim_city.city_name, DATE_FORMAT(sales_order.created_date, '%Y-%m')"
    )

    with pytest.raises(AdapterError) as mismatch:
        HttpDataRetrievalAdapter._validate_geographic_hierarchy_alignment(
            asl, trend, wrong_branch
        )
    assert mismatch.value.code == "SQL_GEOGRAPHIC_HIERARCHY_MISMATCH"
    assert mismatch.value.details["mismatches"][0]["join_path"] == [
        "dim_city", "dealer", "sales_order", "hospital", "dim_province",
    ]

    same_hierarchy = wrong_branch.replace(
        "LEFT JOIN hospital ON sales_order.hospital_id = hospital.hospital_id "
        "LEFT JOIN dim_province ON hospital.province_id = dim_province.province_id ",
        "LEFT JOIN dim_province ON dim_city.province_id = dim_province.province_id ",
    )
    HttpDataRetrievalAdapter._validate_geographic_hierarchy_alignment(
        asl, trend, same_hierarchy
    )


def test_current_metric_formula_replaces_stale_same_table_distinct_field():
    sql = (
        "SELECT dealer.dealer_name AS 经销商, "
        "COUNT(DISTINCT hospital.hospital_name) AS 已合作医院数 "
        "FROM sales_order LEFT JOIN hospital ON sales_order.hospital_id = hospital.hospital_id"
    )

    repaired = HttpDataRetrievalAdapter._apply_current_metric_formulas(sql, [{
        "metric_id": "81:cooperating_hospital_count",
        "version": "current",
        "formula": "cooperating_hospital_count=COUNT(DISTINCT hospital.hospital_code)",
    }])

    assert "COUNT(DISTINCT hospital.hospital_code)" in repaired
    assert "COUNT(DISTINCT hospital.hospital_name)" not in repaired


def test_current_metric_formula_rejects_unprovable_stale_sql():
    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._apply_current_metric_formulas(
            "SELECT COUNT(DISTINCT customer.customer_name) FROM sales_order",
            [{
                "metric_id": "81:cooperating_hospital_count",
                "version": "current",
                "formula": "COUNT(DISTINCT hospital.hospital_code)",
            }],
        )

    assert exc.value.code == "METRIC_FORMULA_STALE"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 10])
async def test_grouped_metric_removes_duplicate_identity_before_sql_translation(limit):
    asl = {
        "version": "2.0",
        "subject": {"entity": "sales_order"},
        "metrics": [{
            "name": "cooperating_hospital_count",
            "alias": "已合作医院数",
        }],
        "dimensions": [
            {"name": "dealer", "alias": "经销商"},
            {"name": "dealer.dealer_name"},
        ],
        "sort": ({
            "field": "cooperating_hospital_count",
            "direction": "DESC",
            "field_type": "metric",
        } if limit else None),
        "limit": limit,
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {
            "success": True,
            "sql": "SELECT dealer_name AS 经销商, COUNT(*) AS 已合作医院数 FROM sales_order",
        },
        {
            "success": True,
            "sql": "SELECT dealer_name AS 经销商, COUNT(*) AS 已合作医院数 FROM sales_order",
            "data": [{"经销商": "经销商A", "已合作医院数": 2}],
            "columns": ["经销商", "已合作医院数"],
            "row_count": 1,
        },
    ])
    question = "统计每个经销商已合作医院数量" + ("，取Top10" if limit else "")
    grouped = CanonicalAnalysisRequest(
        conversation_id=f"dealer-hospital-count-{limit}",
        tenant_id="t1",
        user_id="u1",
        original_question=question,
        rewritten_question=question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="经销商",
        dimensions=["经销商"],
        metrics=[MetricRef(
            input="已合作医院数",
            canonical_name="已合作医院数",
            metric_id="81:cooperating_hospital_count",
        )],
    )

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(grouped, IDENTITY, semantic_model_id=81, business_domain_id=205)

    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["dimensions"] == [
        {"name": "dealer", "alias": "经销商"}
    ]
    assert result.dataset.columns == ["经销商", "已合作医院数"]


@pytest.mark.asyncio
async def test_preview_limit_followup_reuses_all_grounded_asl_filters():
    template = {
        "version": "2.0",
        "subject": {"entity": "sales_order"},
        "metrics": [],
        "dimensions": [{"name": "dealer.dealer_name"}],
        "filters": [{
            "field": "manufacturer.parent_brand",
            "operator": "=",
            "value": "费森尤斯",
        }],
        "time_context": {
            "type": "range", "start": "2025-08-28", "end": "2026-08-28",
            "anchor": "sales_order.created_date",
        },
        "limit": None,
        "ambiguity": [],
    }
    followup = request().model_copy(update={
        "primary_intent": PrimaryIntent.DETAIL_QUERY,
        "metrics": [],
        "fields": ["经销商名称"],
        "ranking_limit": 20,
        "asl_template": template,
        "assumptions": ["DETERMINISTIC_LIMIT_FAST_PATH"],
    })
    client = StubClient([
        {"success": True, "sql": "SELECT dealer_name LIMIT 20"},
        {
            "success": True, "sql": "SELECT dealer_name LIMIT 20",
            "data": [{"dealer.dealer_name": "甲"}],
            "columns": ["dealer.dealer_name"], "row_count": 1,
        },
    ])

    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        followup, IDENTITY, semantic_model_id=8, business_domain_id=13
    )

    assert [call[1] for call in client.calls] == ["/api/translate", "/api/execute"]
    reused = json.loads(client.calls[0][2]["asl"])
    assert reused["filters"] == template["filters"]
    assert reused["dimensions"] == template["dimensions"]
    assert reused["limit"] == 20

@pytest.mark.asyncio
async def test_detail_intent_removes_hallucinated_aggregate_metric_before_translation():
    detail = CanonicalAnalysisRequest(
        conversation_id="detail-1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询订单明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        fields=["订单号", "商品名称"],
    )
    asl = {
        "version": "2.0",
        "subject": {"entity": "ent_order_item"},
        "metrics": [{"name": "invented_detail_metric", "alias": "订单明细"}],
        "dimensions": [
            {"name": "order_item_detail.order_id"},
            {"name": "order_item_detail.goods_name"},
        ],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {"success": True, "sql": "SELECT order_id, goods_name FROM order_item_detail"},
        {
            "success": True,
            "sql": "SELECT order_id, goods_name FROM order_item_detail",
            "data": [{"order_id": "O-1", "goods_name": "口罩"}],
            "columns": ["order_id", "goods_name"],
            "row_count": 1,
        },
    ])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(detail, IDENTITY, semantic_model_id=6, business_domain_id=7)

    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["metrics"] == []
    assert result.dataset.row_count == 1


@pytest.mark.asyncio
async def test_detail_intent_rejects_asl_that_omits_requested_field():
    detail = CanonicalAnalysisRequest(
        conversation_id="detail-missing", tenant_id="t1", user_id="u1",
        original_question="查询订单明细，显示订单号、金额和状态",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        fields=["订单号", "金额", "状态"],
    )
    asl = {
        "version": "2.0", "subject": {"entity": "ent_order"}, "metrics": [],
        "dimensions": [
            {"name": "order_info.order_id"},
            {"name": "order_info.order_status"},
        ],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            detail, IDENTITY, semantic_model_id=6, business_domain_id=7
        )

    assert exc.value.code == "ASL_DETAIL_FIELDS_INCOMPLETE"
    assert exc.value.details["missing_fields"] == ["金额"]
    assert "dimensions 必须完整投影" in client.calls[0][2]["query"]
    assert len(client.calls) == 1


def test_manufacturer_name_family_is_distinct_from_relationship_code():
    adapter = HttpDataRetrievalAdapter

    assert adapter._constraint_field_family("厂家名称") == "manufacturer"
    assert (
        adapter._constraint_field_family("manufacturer.manufacturer_name")
        == "manufacturer"
    )
    assert adapter._constraint_field_matches(
        "厂家名称", "manufacturer.manufacturer_name"
    )
    assert not adapter._constraint_field_matches(
        "厂家名称", "product.manufacturer_code"
    )


@pytest.mark.asyncio
async def test_real_two_stage_contract_and_double_encoded_result():
    asl = {"version": "2.0", "metrics": [{"name": "average_transaction_value", "alias": "客单价"}], "ambiguity": []}
    inner = {"sql": "SELECT ...", "success": True, "data": [{"会员等级": "普通会员", "客单价": 322.75}], "columns": ["会员等级", "客单价"], "row_count": 1, "error": None, "data_source": {"id": "6", "host": "private"}}
    client = StubClient([{"success": True, "result": json.dumps(asl, ensure_ascii=False)}, {"success": True, "sql": "SELECT ...", "modelId": "8"}, inner])
    result = await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)
    assert result.dataset.rows[0]["客单价"] == 322.75
    assert result.data_source_id == "6"
    assert [call[1] for call in client.calls] == ["/agent/query", "/api/translate", "/api/execute"]
    assert client.calls[0][3]["idempotency_key"].endswith(":asl")
    assert client.calls[0][3]["retryable"] is True
    assert client.calls[0][3]["timeout"] == 90
    assert client.calls[1][3]["idempotency_key"].endswith(":sql-translate")
    assert client.calls[2][3]["idempotency_key"].endswith(":sql-execute")
    assert client.calls[0][3]["idempotency_key"] != client.calls[1][3]["idempotency_key"]
    assert "host" not in result.model_dump()


@pytest.mark.asyncio
async def test_validated_asl_plan_is_reused_but_sql_is_executed_again():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "sales_total", "alias": "销售总额"}],
        "ambiguity": [],
    }
    translated = {"success": True, "sql": "SELECT SUM(amount) AS sales_total"}
    executed = {
        "success": True,
        "sql": "SELECT SUM(amount) AS sales_total",
        "data": [{"销售总额": 100.0}],
        "columns": ["销售总额"],
        "row_count": 1,
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        translated, executed,
        translated, executed,
    ])
    adapter = HttpDataRetrievalAdapter(
        Settings(
            adapter_mode="http",
            asl_plan_cache_ttl_seconds=300,
            asl_plan_cache_max_items=8,
        ),
        client,
    )

    await adapter.query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)
    await adapter.query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)

    paths = [call[1] for call in client.calls]
    assert paths.count("/agent/query") == 1
    assert paths.count("/api/translate") == 2
    assert paths.count("/api/execute") == 2


@pytest.mark.asyncio
async def test_dimension_scoped_plan_is_regenerated_after_semantic_metadata_update():
    first_asl = {
        "version": "2.0",
        "metrics": [],
        "dimensions": [{"name": "member.level", "alias": "会员等级"}],
        "ambiguity": [],
    }
    second_asl = {
        "version": "2.0",
        "metrics": [],
        "dimensions": [{"name": "customer.member_level", "alias": "会员等级"}],
        "ambiguity": [],
    }
    translated = {"success": True, "sql": "SELECT member_level FROM customer"}
    executed = {
        "success": True,
        "sql": "SELECT member_level FROM customer",
        "data": [{"会员等级": "普通"}],
        "columns": ["会员等级"],
        "row_count": 1,
    }
    client = StubClient([
        {"success": True, "result": json.dumps(first_asl, ensure_ascii=False)},
        translated,
        executed,
        {"success": True, "result": json.dumps(second_asl, ensure_ascii=False)},
        translated,
        executed,
    ])
    adapter = HttpDataRetrievalAdapter(
        Settings(
            adapter_mode="http",
            asl_plan_cache_ttl_seconds=300,
            asl_plan_cache_max_items=8,
        ),
        client,
    )
    scoped = request().model_copy(update={"dimensions": ["会员等级"]})

    first = await adapter.query(
        scoped, IDENTITY, semantic_model_id=8, business_domain_id=13
    )
    second = await adapter.query(
        scoped, IDENTITY, semantic_model_id=8, business_domain_id=13
    )

    assert first.asl["dimensions"][0]["name"] == "member.level"
    assert second.asl["dimensions"][0]["name"] == "customer.member_level"
    paths = [call[1] for call in client.calls]
    assert paths.count("/agent/query") == 2


@pytest.mark.asyncio
async def test_backend_database_id_is_loaded_into_sql_execution_request():
    scoped_request = request().model_copy(update={"database_id": 5})
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT 1"},
        {
            "success": True,
            "sql": "SELECT 1",
            "data": [{"value": 1}],
            "columns": ["value"],
            "row_count": 1,
            "data_source": {"id": "5"},
        },
    ])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(scoped_request, IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert client.calls[2][2]["dataSourceId"] == "5"
    assert result.data_source_id == "5"


@pytest.mark.asyncio
async def test_backend_database_id_rejects_translator_scope_mismatch():
    scoped_request = request().model_copy(update={"database_id": 5})
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT 1", "dataSourceId": "6"},
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(scoped_request, IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "DATA_SOURCE_SCOPE_MISMATCH"
    assert [call[1] for call in client.calls] == ["/agent/query", "/api/translate"]


@pytest.mark.asyncio
async def test_large_result_contract_accepts_preview_and_minio_url():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    inner = {
        "sql": "SELECT ...", "success": True,
        "preview_data": [{"订单号": "O-1", "金额": 10}],
        "columns": ["订单号", "金额"],
        "preview_row_count": 1, "row_count": 50000,
        "truncated": True,
        "minio_url": "http://192.168.1.10/results/large.parquet",
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT ..."}, inner,
    ])
    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=None)

    assert result.dataset.row_count == 1
    assert result.dataset.truncated is True
    assert result.result_file_url.endswith("large.parquet")


@pytest.mark.asyncio
async def test_sql_translator_data_preview_with_download_url_is_accepted():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    preview = [{"经销商": f"经销商-{index}"} for index in range(20)]
    inner = {
        "sql": "SELECT ...",
        "success": True,
        "data": preview,
        "columns": ["经销商"],
        "row_count": 2154,
        "preview_count": 20,
        "preview_truncated": True,
        "download_url": "http://files.example/dealers.xlsx",
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT ..."},
        inner,
    ])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)

    assert result.dataset.rows == preview
    assert result.dataset.row_count == 20
    assert result.dataset.total_row_count == 2154
    assert result.dataset.truncated is True
    assert result.result_file_url == inner["download_url"]


@pytest.mark.asyncio
async def test_new_sql_translator_download_only_excel_contract_is_accepted():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "actual_payment_amount", "alias": "销售额"}],
        "ambiguity": [],
    }
    inner = {
        "sql": "SELECT ...",
        "success": True,
        "data": None,
        "columns": ["订单号", "销售额"],
        "row_count": 201,
        "download_url": "http://192.168.1.223:29000/bam/result.xlsx",
        "message": "数据量超过阈值，已导出",
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {"success": True, "sql": "SELECT ..."}, inner,
    ])

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(request(), IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert result.dataset.rows == []
    assert result.dataset.row_count == 0
    assert result.dataset.total_row_count == 201
    assert result.dataset.truncated is True
    assert result.result_file_url == inner["download_url"]


@pytest.mark.asyncio
async def test_double_encoded_execution_failure_is_classified_as_execution_error():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    failed = {
        "success": False,
        "sql": "SELECT 1",
        "error": "数据库执行错误",
        "error_code": "QUERY_TIMEOUT",
        "retryable": True,
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT 1"}, failed,
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request(), IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "SQL_EXECUTION_FAILED"
    assert exc.value.upstream_code == "QUERY_TIMEOUT"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_execution_http_500_is_mapped_to_execution_stage() -> None:
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT 1"},
        AdapterError(
            "DEPENDENCY_UNAVAILABLE",
            "HTTP 500",
            status_code=500,
            upstream_code="UNKNOWN_COLUMN",
            retryable=False,
            details={"path": "/api/execute"},
        ),
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)

    assert exc.value.code == "SQL_EXECUTION_FAILED"
    assert exc.value.upstream_code == "UNKNOWN_COLUMN"
    assert exc.value.details == {"path": "/api/execute"}


@pytest.mark.asyncio
async def test_translation_failure_stops_before_execute():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": False, "error": "metric mapping missing", "code": "METRIC_NOT_FOUND"},
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request(), IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "SQL_TRANSLATION_FAILED"
    assert exc.value.upstream_code == "METRIC_NOT_FOUND"
    assert [call[1] for call in client.calls] == ["/agent/query", "/api/translate"]


@pytest.mark.asyncio
async def test_missing_split_translation_route_has_specific_deployment_error():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        AdapterError("DEPENDENCY_CONTRACT_REJECTED", "HTTP 404", status_code=404),
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request(), IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "SQL_TRANSLATION_ENDPOINT_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DELETE FROM order_info",
        "SELECT 1; DROP TABLE order_info",
        "SELECT * FROM order_info INTO OUTFILE '/tmp/orders.csv'",
        "WITH x AS (DELETE FROM order_info RETURNING *) SELECT * FROM x",
        "SELECT GET_LOCK('agent', 60)",
    ],
)
async def test_generated_non_read_only_sql_is_rejected_before_execute(unsafe_sql):
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": unsafe_sql},
    ])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request(), IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "SQL_SAFETY_REJECTED"
    assert [call[1] for call in client.calls] == ["/agent/query", "/api/translate"]


@pytest.mark.asyncio
async def test_comparison_query_requires_grouped_asl_and_adds_deterministic_hint():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "actual_payment_amount"}],
        "dimensions": [{"name": "dim_date", "granularity": "month"}],
        "ambiguity": [],
    }
    inner = {
        "success": True,
        "sql": "SELECT month, SUM(amount) FROM orders GROUP BY month",
        "data": [{"month": "2026-06", "sales": 10}, {"month": "2026-07", "sales": 20}],
        "columns": ["month", "sales"],
        "row_count": 2,
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": inner["sql"]},
        inner,
    ])
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.COMPARISON_ANALYSIS,
        "original_question": "查询2026年7月销售额，并和6月对比",
        "rewritten_question": "查询2026年7月销售额，并和6月对比",
        "comparison_type": "指定时段对比",
    })

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(req, IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert result.dataset.row_count == 2
    assert "至少两个可比较分组" in client.calls[0][2]["query"]


@pytest.mark.asyncio
async def test_unbounded_entity_sort_requires_metric_order_without_limit():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "business_scale", "alias": "整体业务规模"}],
        "dimensions": [{"name": "dealer.dealer_name", "alias": "经销商"}],
        "sort": {"field": "business_scale", "field_type": "metric", "direction": "DESC"},
        "limit": None,
        "ambiguity": [],
    }
    inner = {
        "success": True,
        "sql": "SELECT dealer_name, business_scale FROM dealer ORDER BY business_scale DESC",
        "data": [
            {"经销商名称": "甲", "整体业务规模": 20},
            {"经销商名称": "乙", "整体业务规模": 10},
        ],
        "columns": ["经销商名称", "整体业务规模"],
        "row_count": 2,
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {"success": True, "sql": inner["sql"]},
        inner,
    ])
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.COMPARISON_ANALYSIS,
        "original_question": "列出经销商并按整体业务规模排序",
        "rewritten_question": "列出经销商并按整体业务规模排序",
        "comparison_type": "对象间比较",
        "metrics": [MetricRef(input="整体业务规模")],
        "dimensions": ["经销商"],
        "operators": [
            AnalysisOperator.COMPARE,
            AnalysisOperator.GROUP_BY,
            AnalysisOperator.SORT,
        ],
    })

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(req, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert result.dataset.row_count == 2
    prompt = client.calls[0][2]["query"]
    assert "不是基期/当前期对比" in prompt
    assert "不得只返回两行" in prompt


@pytest.mark.asyncio
async def test_unbounded_entity_sort_rejects_hidden_limit_before_sql():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "business_scale", "alias": "整体业务规模"}],
        "dimensions": [{"name": "dealer.dealer_name", "alias": "经销商名称"}],
        "sort": {"field": "business_scale", "field_type": "metric", "direction": "DESC"},
        "limit": 10,
        "ambiguity": [],
    }
    client = StubClient([{"success": True, "result": json.dumps(asl, ensure_ascii=False)}])
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.COMPARISON_ANALYSIS,
        "comparison_type": "对象间比较",
        "metrics": [MetricRef(input="整体业务规模")],
        "dimensions": ["经销商"],
        "operators": [
            AnalysisOperator.COMPARE,
            AnalysisOperator.GROUP_BY,
            AnalysisOperator.SORT,
        ],
    })

    with pytest.raises(AdapterError, match="must not add") as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            req, IDENTITY, semantic_model_id=81, business_domain_id=205
        )

    assert exc.value.code == "ASL_ANALYSIS_SHAPE_INVALID"
    assert [call[1] for call in client.calls] == ["/agent/query"]


@pytest.mark.asyncio
async def test_comparison_aggregate_without_group_dimension_fails_before_sql():
    asl = {
        "version": "2.0",
        "metrics": [{"name": "actual_payment_amount"}],
        "dimensions": [],
        "ambiguity": [],
    }
    client = StubClient([{"success": True, "result": json.dumps(asl)}])
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.COMPARISON_ANALYSIS,
        "comparison_type": "指定时段对比",
    })

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(req, IDENTITY, semantic_model_id=6, business_domain_id=7)

    assert exc.value.code == "ASL_ANALYSIS_SHAPE_INVALID"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_missing_business_domain_uses_semantic_model_wide_asl_routing():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    inner = {
        "sql": "SELECT 1",
        "success": True,
        "data": [{"value": 1}],
        "columns": ["value"],
        "row_count": 1,
    }
    client = StubClient(
        [
            {"success": True, "result": json.dumps(asl)},
            {"success": True, "sql": "SELECT 1"}, inner,
        ]
    )

    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        request(), IDENTITY, semantic_model_id=8, business_domain_id=None
    )
    assert client.calls[0][2]["semantic_model_id"] == 8
    assert client.calls[0][2]["business_domain_id"] is None
    assert client.calls[0][2]["business_domain_ids"] == []


@pytest.mark.asyncio
async def test_detail_execution_constraints_do_not_pollute_semantic_retrieval():
    asl = {
        "version": "2.0",
        "metrics": [],
        "dimensions": [{"name": "supplier_info.supplier_name"}],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT supplier_name FROM supplier_info"},
        {
            "success": True,
            "sql": "SELECT supplier_name FROM supplier_info",
            "data": [{"supplier_name": "supplier-a"}],
            "columns": ["supplier_name"],
            "row_count": 1,
        },
    ])
    business_question = "提供SoundPro无线蓝牙耳机的供应商"
    req = request().model_copy(update={
        "original_question": business_question,
        "rewritten_question": business_question,
        "primary_intent": PrimaryIntent.DETAIL_QUERY,
        "fields": ["供应商名称"],
        "business_domain_ids": [7],
    })

    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        req, IDENTITY, semantic_model_id=6, business_domain_id=7
    )

    payload = client.calls[0][2]
    assert all(
        value in payload["retrieval_query"]
        for value in ("SoundPro", "无线蓝牙耳机", "供应商名称", "供应商")
    )
    assert "dimensions 必须" not in payload["retrieval_query"]
    assert payload["query"].startswith(business_question)
    assert payload["query"] != payload["retrieval_query"]


@pytest.mark.asyncio
async def test_grouped_partner_metric_keeps_metric_and_uses_catalog_relationship_recall():
    asl = {
        "version": "2.0",
        "subject": {"entity": "sales_order"},
        "metrics": [{"name": "tax_included_sales_amount", "alias": "销售总额"}],
        "dimensions": [
            {"name": "dealer.dealer_name", "alias": "经销商"},
            {"name": "guoyao_company.city", "alias": "城市"},
            {"name": "manufacturer.parent_brand", "alias": "商品品牌"},
            {"name": "product_category.product_type", "alias": "商品品类"},
        ],
        "filters": [
            {"field": "dealer.province", "operator": "=", "value": "上海市"},
            {
                "field": "manufacturer.manufacturer_name",
                "operator": "=",
                "value": "江苏苏云医疗器材有限公司",
            },
            {
                "field": "product_category.product_type",
                "operator": "=",
                "value": "低值耗材",
            },
        ],
        "ambiguity": [],
    }
    client = StubClient([
        {"success": True, "result": json.dumps(asl, ensure_ascii=False)},
        {
            "success": True,
            "sql": "SELECT dealer_name, SUM(amount) FROM sales_order GROUP BY dealer_name",
        },
        {
            "success": True,
            "sql": "SELECT dealer_name, SUM(amount) FROM sales_order GROUP BY dealer_name",
            "data": [{"dealer_name": "经销商A", "销售总额": 100}],
            "columns": ["dealer_name", "销售总额"],
            "row_count": 1,
        },
    ])
    business_question = (
        "查询上海市江苏苏云品牌低值耗材的经销商清单，"
        "并附带经销商对应销售总额"
    )
    grouped = CanonicalAnalysisRequest(
        conversation_id="grouped-partner-metric",
        tenant_id="t1",
        user_id="u1",
        original_question=business_question,
        rewritten_question=business_question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="经销商",
        dimensions=["经销商", "城市", "商品品牌", "商品品类"],
        metrics=[MetricRef(input="销售总额")],
        filters=[
            {"field": "城市", "operator": "EQ", "value": "上海市"},
            {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
            {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
        ],
    )

    result = await HttpDataRetrievalAdapter(
        Settings(adapter_mode="http"), client
    ).query(grouped, IDENTITY, semantic_model_id=81, business_domain_id=205)

    payload = client.calls[0][2]
    assert all(
        value in payload["retrieval_query"]
        for value in (
            "经销商", "品牌", "商品分类", "产品分类", "品类", "类目", "耗材", "订单",
        )
    )
    assert "按经销商分组的指标清单" in payload["query"]
    assert all(
        value in payload["query"]
        for value in ("经销商", "城市", "商品品牌", "商品品类")
    )
    translated_asl = json.loads(client.calls[1][2]["asl"])
    assert translated_asl["metrics"] == asl["metrics"]
    assert result.dataset.row_count == 1


@pytest.mark.asyncio
async def test_grouped_partner_metric_rejects_missing_metric_before_sql_translation():
    grouped = CanonicalAnalysisRequest(
        conversation_id="grouped-partner-metric-invalid",
        tenant_id="t1",
        user_id="u1",
        original_question="查询供应商清单并附带销售总额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="供应商",
        dimensions=["供应商"],
        metrics=[MetricRef(input="销售总额")],
    )
    client = StubClient([{
        "success": True,
        "result": json.dumps({
            "version": "2.0",
            "subject": {"entity": "supplier"},
            "metrics": [],
            "dimensions": [{"name": "supplier.supplier_name"}],
            "ambiguity": [],
        }, ensure_ascii=False),
    }])

    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(grouped, IDENTITY, semantic_model_id=81, business_domain_id=205)

    assert exc.value.code == "ASL_ANALYSIS_SHAPE_INVALID"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_multiple_business_domains_remain_strict_in_asl_request():
    asl = {"version": "2.0", "metrics": [], "ambiguity": []}
    inner = {
        "sql": "SELECT 1", "success": True, "data": [{"value": 1}],
        "columns": ["value"], "row_count": 1,
    }
    client = StubClient([
        {"success": True, "business_domain_ids": [7, 10], "result": json.dumps(asl)},
        {"success": True, "sql": "SELECT 1"}, inner,
    ])
    req = request().model_copy(update={"business_domain_ids": [7, 10]})
    await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
        req, IDENTITY, semantic_model_id=8, business_domain_id=None
    )
    assert client.calls[0][2]["business_domain_id"] is None
    assert client.calls[0][2]["business_domain_ids"] == [7, 10]


@pytest.mark.asyncio
async def test_multiple_business_domains_fail_closed_when_oagnet_does_not_echo_scope():
    client = StubClient([
        {"success": True, "result": json.dumps({"version": "2.0", "metrics": [], "ambiguity": []})},
    ])
    req = request().model_copy(update={"business_domain_ids": [7, 10]})
    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            req, IDENTITY, semantic_model_id=8, business_domain_id=None
        )
    assert exc.value.code == "ASL_SCOPE_INVALID"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_query_health_checks_required_openapi_contracts_not_health_route():
    client = ContractClient([True, True, True])
    settings = Settings(adapter_mode="http")
    adapter = HttpDataRetrievalAdapter(settings, client)
    assert await adapter.health() is True
    assert client.calls == [
        (
            settings.asl_generator_base_url,
            [settings.asl_generator_path],
        ),
        (
            settings.sql_translator_base_url,
            [settings.sql_translate_path, settings.sql_execute_path],
        ),
    ]
    assert await adapter.rewrite_health() is True
    assert client.calls[-1] == (
        settings.asl_generator_base_url,
        [settings.entity_attribute_search_path],
    )

@pytest.mark.asyncio
async def test_semantic_model_is_still_required_for_query():
    client = StubClient([])
    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(
            request(), IDENTITY, semantic_model_id=None, business_domain_id=None
        )
    assert exc.value.code == "SEMANTIC_CONTEXT_MISSING"
    assert client.calls == []

@pytest.mark.asyncio
async def test_asl_ambiguity_stops_before_sql_execution():
    client = StubClient([{"success": True, "result": json.dumps({"ambiguity": [{"question": "请选择销售额口径"}]}, ensure_ascii=False)}])
    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)
    assert exc.value.code == "ASL_AMBIGUOUS"
    assert len(client.calls) == 1

@pytest.mark.asyncio
async def test_row_count_mismatch_is_rejected():
    client = StubClient([{"success": True, "result": json.dumps({"metrics": [], "ambiguity": []})}, {"success": True, "sql": "SELECT 1"}, {"sql": "SELECT 1", "success": True, "data": [{"x": 1}], "columns": ["x"], "row_count": 2}])
    with pytest.raises(AdapterError) as exc:
        await HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client).query(request(), IDENTITY, semantic_model_id=8, business_domain_id=13)
    assert exc.value.code == "SQL_RESPONSE_INVALID"

@pytest.mark.asyncio
async def test_analysis_knowledge_parses_real_document_contract_without_sending_rows():
    client = StubClient([[{"page_content": "促销策略调整可能影响客单价。", "score": 0.18, "id": "vs-1", "kb_name": "KB_SALES", "retrieval_method": "hybrid", "metadata": {"source": "policy.md", "block_id": "b1"}}]])
    adapter = HttpKnowledgeAdapter(Settings(adapter_mode="http", knowledge_base_names=["KB_SALES"]), client)
    req = request().model_copy(update={"primary_intent": PrimaryIntent.ROOT_CAUSE_ANALYSIS, "knowledge_base_names": ["KB_SALES"]})
    dataset = Dataset(columns=["月份", "销售额"], rows=[{"月份": "2026-07", "销售额": 10}], row_count=1, snapshot_id="s1", data_as_of=datetime.now(timezone.utc))
    context = await adapter.retrieve_analysis_context(req, dataset, IDENTITY)
    assert context.documents[0].source == "policy.md"
    payload_text = json.dumps(client.calls[0][2], ensure_ascii=False)
    assert "2026-07" not in payload_text
    assert "结果行数：1" in payload_text


@pytest.mark.asyncio
async def test_empty_request_kb_scope_does_not_use_process_default():
    client = StubClient([])
    adapter = HttpKnowledgeAdapter(
        Settings(adapter_mode="http", knowledge_base_names=["GLOBAL_DEFAULT"]), client
    )
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        "knowledge_base_names": [],
    })
    dataset = Dataset(
        columns=["销售额"], rows=[{"销售额": 10}], row_count=1,
        snapshot_id="s1", data_as_of=datetime.now(timezone.utc),
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.retrieve_analysis_context(req, dataset, IDENTITY)
    assert exc.value.code == "KNOWLEDGE_SCOPE_MISSING"
    assert client.calls == []


def test_dataset_fingerprint_is_stable_across_request_retries():
    payload = {
        "success": True,
        "data": [{"月份": "2026-07", "销售额": 10}],
        "columns": ["月份", "销售额"],
        "row_count": 1,
    }
    first = HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    second = HttpDataRetrievalAdapter._dataset(payload, request_id="request-b")
    assert first.snapshot_id == second.snapshot_id


def test_relationship_projection_normalizes_and_deduplicates_visible_rows():
    payload = {
        "success": True,
        "data": [
            {"商品名称": "超声血管导引穿刺套件", "适用科室": "麻醉科"},
            {"商品名称": "超声血管导引穿刺套件 ", "适用科室": " 麻醉科"},
            {"商品名称": "超声血管导引穿刺套件", "适用科室": "肾内科"},
        ],
        "columns": ["商品名称", "适用科室"],
        "row_count": 3,
    }

    dataset = HttpDataRetrievalAdapter._dataset(
        payload,
        request_id="relationship-dedupe",
        distinct_projection=True,
    )

    assert dataset.rows == [
        {"商品名称": "超声血管导引穿刺套件", "适用科室": "麻醉科"},
        {"商品名称": "超声血管导引穿刺套件", "适用科室": "肾内科"},
    ]
    assert dataset.row_count == 2
    assert dataset.total_row_count == 2


class _ConcurrentRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _keys, key, token, *_args):
        if self.values.get(key) == token:
            if "EXPIRE" in script:
                return 1
            self.values.pop(key, None)
            return 1
        return 0


class _SlowKnowledgeClient:
    def __init__(self):
        self.call_count = 0

    async def post(self, *_args, **_kwargs):
        self.call_count += 1
        await asyncio.sleep(0.05)
        return [{
            "page_content": "metric definition",
            "score": 0.1,
            "kb_name": "KB_SALES",
            "metadata": {"source": "metric.md", "block_id": "sales"},
        }]


@pytest.mark.asyncio
async def test_knowledge_cache_collapses_concurrent_identical_misses():
    client = _SlowKnowledgeClient()
    cache = RedisKnowledgeSearchCache(
        _ConcurrentRedis(),
        ttl_seconds=300,
        prefix="test",
        lock_seconds=10,
        wait_seconds=0.3,
    )
    adapter = HttpKnowledgeAdapter(
        Settings(adapter_mode="http"), client, cache=cache
    )
    req = request().model_copy(update={
        "primary_intent": PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        "knowledge_base_names": ["KB_SALES"],
    })
    dataset = Dataset(
        columns=["month", "sales"],
        rows=[{"month": "2026-07", "sales": 10}],
        row_count=1,
        snapshot_id="s1",
        data_as_of=datetime.now(timezone.utc),
    )
    results = await asyncio.gather(*[
        adapter.retrieve_analysis_context(req, dataset, IDENTITY)
        for _ in range(20)
    ])
    assert client.call_count == 1
    assert all(result.documents for result in results)


def test_dataset_preserves_complete_upstream_snapshot_metadata():
    payload = {
        "success": True,
        "data": [{"月份": "2026-07", "销售额": 10}],
        "columns": ["月份", "销售额"],
        "row_count": 1,
        "snapshot_id": "warehouse-snapshot-42",
        "data_as_of": "2026-08-20T10:30:00+08:00",
        "quality_status": "PASS",
    }
    dataset = HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    assert dataset.snapshot_id == "warehouse-snapshot-42"
    assert dataset.data_as_of.isoformat() == "2026-08-20T10:30:00+08:00"
    assert dataset.quality_status == "PASS"


def test_dataset_preserves_valid_business_source_watermark():
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "snapshot_id": "warehouse-snapshot-42",
        "data_as_of": "2026-08-20T10:30:00+08:00",
        "quality_status": "PASS",
        "source_data_as_of": "2025-12-30T23:59:58.000000",
        "source_watermark_field": "sales_order.created_date",
    }
    dataset = HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    assert dataset.source_data_as_of == datetime(2025, 12, 30, 23, 59, 58)
    assert dataset.source_watermark_field == "sales_order.created_date"


@pytest.mark.parametrize(
    ("source_data_as_of", "source_watermark_field"),
    [
        ("not-a-date", "sales_order.created_date"),
        ("2025-12-30", None),
        (None, "sales_order.created_date"),
        ("2025-12-30", "sales_order.created_date;DROP TABLE x"),
    ],
)
def test_dataset_rejects_invalid_source_watermark_response(
    source_data_as_of, source_watermark_field
):
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "snapshot_id": "warehouse-snapshot-42",
        "data_as_of": "2026-08-20T10:30:00+08:00",
        "quality_status": "PASS",
        "source_data_as_of": source_data_as_of,
        "source_watermark_field": source_watermark_field,
    }
    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    assert exc.value.code == "SQL_RESPONSE_INVALID"


def test_dataset_rejects_source_watermark_without_pass_snapshot_contract():
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "source_data_as_of": "2025-12-30",
        "source_watermark_field": "sales_order.created_date",
    }
    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    assert exc.value.code == "SQL_RESPONSE_INVALID"


def test_dataset_rejects_malformed_upstream_snapshot_time():
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "snapshot_id": "warehouse-snapshot-42",
        "data_as_of": "not-a-time",
        "quality_status": "PASS",
    }
    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._dataset(payload, request_id="request-a")
    assert exc.value.code == "SQL_RESPONSE_INVALID"


def test_dataset_preserves_upstream_truncation_signal():
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "total_count": 5000,
        "truncated": True,
        "snapshot_id": "warehouse-snapshot-43",
        "data_as_of": "2026-08-20T10:30:00+08:00",
        "quality_status": "PASS",
    }
    dataset = HttpDataRetrievalAdapter._dataset(payload, request_id="request-b")
    assert dataset.truncated is True


def test_dataset_rejects_inconsistent_truncation_contract():
    payload = {
        "success": True,
        "data": [{"销售额": 10}],
        "columns": ["销售额"],
        "row_count": 1,
        "total_count": 5000,
        "truncated": False,
    }
    with pytest.raises(AdapterError) as exc:
        HttpDataRetrievalAdapter._dataset(payload, request_id="request-c")
    assert exc.value.code == "SQL_RESPONSE_INVALID"
def test_model81_trend_repair_keeps_sort_on_physical_time_dimension() -> None:
    request = CanonicalAnalysisRequest(
        conversation_id="product-monthly-sales",
        tenant_id="t1",
        user_id="u1",
        original_question="按月统计空心纤维血液透析器产品的含税销售总额",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        time_range=TimeRange(
            start=date(2025, 8, 29),
            end_exclusive=date(2026, 8, 30),
        ),
    )
    asl = {
        "dimensions": [
            {"name": "product", "granularity": None},
            {"name": "sales_order.created_date", "granularity": "month"},
        ],
        "sort": {
            "field": "transaction_date",
            "direction": "ASC",
            "field_type": "dimension",
        },
    }

    HttpDataRetrievalAdapter._repair_model81_trend_time(asl, request, 81)

    assert asl["sort"]["field"] == "sales_order.created_date"
    assert [item["name"] for item in asl["dimensions"]] == [
        "product",
        "sales_order.created_date",
    ]
