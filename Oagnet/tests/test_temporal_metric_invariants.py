import json

import pytest

from agent import (
    _normalize_caller_bound_metrics,
    _normalize_semantic_references,
    _query_date_bounds,
    _uses_transaction_activity_definition,
    _validate_asl_output,
)
from prompt_build import PromptBuilder
from vector_store import SearchResult


def _result(kind: str, code: str, **metadata) -> SearchResult:
    key = {
        "metric": "metric_code",
        "entity": "entity_code",
        "attribute": "attr_code",
        "dimension": "dim_code",
        "relation": "relation_code",
    }[kind]
    return SearchResult(
        id=f"{kind}:{code}",
        score=float(metadata.pop("score", 0.9)),
        text=code,
        metadata={"type": kind, "semantic_model_id": 81, "business_domain_id": 205, key: code, **metadata},
    )


def _metric(code: str, name: str, synonyms: list[str], *, score: float = 0.9):
    return _result(
        "metric",
        code,
        score=score,
        metric_name=name,
        synonyms=synonyms,
        source_dependency={"bind_entity": ["sales_order"]},
        time_caliber={"time_anchor": "sales_order.created_date"},
        calculation_rule={
            "calc_formula": f"SUM(sales_order.amount_with_tax) AS {code}",
        },
    )


def _entity(code: str, name: str, attributes: list[dict] | None = None):
    return _result(
        "entity",
        code,
        entity_name=name,
        attributes=attributes or [],
    )


def _asl(**updates) -> dict:
    value = {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "sales_order"},
        "metrics": [{"name": "annual_total_sales", "alias": "销售"}],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }
    value.update(updates)
    return value


def _sales_knowledge(*, activity: bool = False) -> dict:
    entities = [_entity("sales_order", "销售订单")]
    if activity:
        entities.extend([
            _entity("dealer", "经销商", [{
                "attr_code": "dealer_name",
                "attr_name": "经销商名称",
                "field_mapping": "dealer.dealer_name",
                "is_main_attribute": True,
            }]),
            _entity("dealer_profile", "经销商画像", [{
                "attr_code": "activity_level",
                "attr_name": "活跃状态",
                "field_mapping": "dealer_result.activity_level",
                "data_type": "varchar",
                "description": "最近一次订单时间",
            }]),
        ])
    return {
        "metrics": [
            _metric("annual_total_sales", "含税销售总额", ["销售额"]),
            _metric("sales_total_quantity", "销售总数量", ["销量", "销售数量"], score=0.7),
        ],
        "dimensions": [],
        "entities": entities,
        "attributes": [],
        "relations": [],
    }


def test_ongoing_sales_requires_a_real_time_range_instead_of_invalid_type():
    ast = _asl(time_context={
        "type": "current",
        "unit": "day",
        "anchor": "sales_order.created_date",
    })

    normalized = json.loads(_normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        _sales_knowledge(),
        "上海地区正在销售某品牌口罩的经销商",
    ))

    assert normalized["time_context"] is None
    assert any(item["type"] == "time_anchor" for item in normalized["ambiguity"])


def test_explicit_dates_repair_invalid_time_type_and_use_retrieved_anchor():
    ast = _asl(time_context={
        "type": "period",
        "start": "2025-10-17",
        "end": "2025-12-30",
        "unit": "year",
        "anchor": "invented.time",
    })
    query = "查看2025年10月17日至2025年12月30日含税销售总额按月趋势"
    knowledge = _sales_knowledge()

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert result["time_context"] == {
        "type": "custom",
        "start": "2025-10-17",
        "end": "2025-12-30",
        "value": None,
        "unit": "month",
        "anchor": "sales_order.created_date",
    }
    _validate_asl_output(normalized, knowledge, query)


def test_generic_sales_trend_keeps_retrieved_metric_ambiguity():
    query = "上海地区某产品近一年销售趋势如何"
    knowledge = _sales_knowledge()

    normalized = _normalize_semantic_references(
        json.dumps(_asl(), ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    metric_ambiguity = [
        item for item in result["ambiguity"] if item["type"] == "metric"
    ]
    assert len(metric_ambiguity) == 1
    assert metric_ambiguity[0]["candidates"] == [
        "annual_total_sales（含税销售总额）",
        "sales_total_quantity（销售总数量）",
    ]
    _validate_asl_output(normalized, knowledge, query)


def test_explicit_sales_metric_and_monthly_range_needs_no_metric_clarification():
    query = "查看2025年10月17日至2025年12月30日含税销售总额按月趋势"
    knowledge = _sales_knowledge()

    normalized = _normalize_semantic_references(
        json.dumps(_asl(), ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert not any(item["type"] == "metric" for item in result["ambiguity"])
    assert result["metrics"][0]["name"] == "annual_total_sales"
    _validate_asl_output(normalized, knowledge, query)


def test_grouped_distinct_count_discards_model_invented_time_scope_and_anchor():
    knowledge = _sales_knowledge(activity=True)
    knowledge["metrics"].append(
        _metric(
            "cooperating_hospital_count",
            "已合作医院数",
            ["合作医院数量", "医院覆盖数"],
        )
    )
    ast = _asl(
        metrics=[{
            "name": "cooperating_hospital_count",
            "alias": "已合作医院数",
            "time_anchor": "invented.created_at",
        }],
        dimensions=[{"name": "dealer.dealer_name"}],
        filters=[{
            "field": "product.product_name",
            "operator": "=",
            "value": "每个经销商",
        }],
        time_context={
            "type": "range",
            "start": "2025-01-01",
            "end": "2025-12-31",
            "unit": "month",
            "anchor": "invented.created_at",
        },
        ambiguity=[{
            "type": "time_anchor",
            "question": "请确认时间字段。",
            "candidates": [],
        }, {
            "type": "filter",
            "question": "过滤值 每个经销商 使用了未注册字段 product.product_name。",
            "candidates": [],
        }],
    )
    query = "统计每个经销商已合作医院数量"

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert result["metrics"] == [{
        "name": "cooperating_hospital_count", "alias": "已合作医院数",
    }]
    assert result["dimensions"] == [{"name": "dealer.dealer_name"}]
    assert result["filters"] == []
    assert result["time_context"] is None
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, query)


def test_grouped_distinct_count_restores_missing_explicit_group_dimension():
    knowledge = _sales_knowledge(activity=True)
    knowledge["metrics"].append(
        _metric(
            "cooperating_hospital_count",
            "已合作医院数",
            ["合作医院数量", "医院覆盖数"],
        )
    )
    ast = _asl(
        metrics=[{
            "name": "cooperating_hospital_count",
            "alias": "已合作医院数",
        }],
        dimensions=[],
        filters=[{
            "field": "product.product_name",
            "operator": "=",
            "value": "所有经销商",
        }],
        ambiguity=[{
            "type": "filter",
            "question": "过滤值所有经销商使用了未注册字段。",
            "candidates": [],
        }],
    )
    query = "统计所有经销商已合作医院数量"

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert result["dimensions"] == [{
        "name": "dealer.dealer_name",
        "attr": None,
        "level": None,
        "granularity": None,
    }]
    assert result["filters"] == []
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, query)


def test_logical_dimension_removes_its_duplicate_bound_physical_field():
    knowledge = _sales_knowledge(activity=True)
    knowledge["dimensions"] = [_result(
        "dimension",
        "dealer",
        dim_name="经销商",
        synonyms=["代理商", "渠道商"],
        bind_entities=[{
            "mappingTable": "dealer",
            "mappingColumn": "dealer_name",
            "attrName": "经销商名称",
        }, {
            "mappingTable": "dealer",
            "mappingColumn": "dealer_code",
            "attrName": "经销商编码",
        }],
    )]
    knowledge["metrics"].append(
        _metric(
            "cooperating_hospital_count",
            "已合作医院数",
            ["合作医院数量", "医院覆盖数"],
        )
    )
    ast = _asl(
        metrics=[{
            "name": "cooperating_hospital_count",
            "alias": "已合作医院数",
        }],
        dimensions=[
            {"name": "dealer", "attr": None, "level": None, "granularity": None},
            {"name": "dealer.dealer_name", "attr": None, "level": None, "granularity": None},
        ],
    )

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "统计每个经销商已合作医院数量",
    )
    result = json.loads(normalized)

    assert result["dimensions"] == [{
        "name": "dealer", "attr": None, "level": None, "granularity": None,
    }]
    _validate_asl_output(
        normalized, knowledge, "统计每个经销商已合作医院数量"
    )


def test_explicit_date_scope_repairs_metric_override_to_registered_anchor():
    knowledge = _sales_knowledge()
    ast = _asl(
        metrics=[{
            "name": "annual_total_sales",
            "alias": "含税销售总额",
            "time_anchor": "invented.created_at",
        }],
    )
    query = "统计2025年1月1日至2025年12月31日含税销售总额"

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert result["metrics"][0]["time_anchor"] == "sales_order.created_date"
    assert result["time_context"]["anchor"] == "sales_order.created_date"
    _validate_asl_output(normalized, knowledge, query)


def test_sales_record_activity_uses_fact_window_not_profile_activity_threshold():
    query = "近一年内，哪些活跃经销商在销售费森尤斯产品"
    knowledge = _sales_knowledge(activity=True)
    ast = _asl(
        dimensions=[{"name": "dealer.dealer_name"}],
        filters=[{
            "field": "dealer.active_status",
            "operator": "=",
            "value": "活跃",
        }, {
            "field": "dealer.business_status",
            "operator": "=",
            "value": "正常经营",
        }],
        time_context={"type": "last_year"},
    )

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)
    assert result["filters"] == []
    assert result["time_context"]["type"] == "range"
    assert result["time_context"]["anchor"] == "sales_order.created_date"
    assert not any(
        "活跃" in str(item.get("question") or "")
        for item in result["ambiguity"]
    )
    _validate_asl_output(normalized, knowledge, query)


def test_active_label_is_never_accepted_as_a_last_order_date():
    knowledge = _sales_knowledge(activity=True)
    ast = _asl(filters=[{
        "field": "dealer_result.activity_level",
        "operator": "=",
        "value": "活跃",
    }])

    with pytest.raises(ValueError, match="ISO dates"):
        _validate_asl_output(
            json.dumps(ast, ensure_ascii=False), knowledge, "查询活跃经销商",
        )


class _CandidateStore:
    def __init__(self, metrics: list[SearchResult]):
        self.metrics = metrics

    def count(self):
        return len(self.metrics)

    def search(self, _vector, top_k, where):
        clauses = where.get("$and", [where])
        kind = next(item["type"] for item in clauses if "type" in item)
        return list(self.metrics[:top_k]) if kind == "metric" else []


class _ScopeAwareCandidateStore(_CandidateStore):
    def search(self, _vector, top_k, where):
        clauses = where.get("$and", [where])
        kind = next(item["type"] for item in clauses if "type" in item)
        return list(self.metrics[:1]) if kind == "metric" else []

    def get_by_where(self, where):
        clauses = where.get("$and", [where])
        kind = next(item["type"] for item in clauses if "type" in item)
        return list(self.metrics) if kind == "metric" else []


class _ScopedTimeDimensionStore(_CandidateStore):
    def __init__(
        self,
        metrics: list[SearchResult],
        dimensions: list[SearchResult],
    ):
        super().__init__(metrics)
        self.dimensions = dimensions

    def search(self, _vector, top_k, where):
        clauses = where.get("$and", [where])
        kind = next(item["type"] for item in clauses if "type" in item)
        if kind == "metric":
            return list(self.metrics[:top_k])
        if kind == "dimension":
            return list(self.dimensions[:1])
        return []

    def get_by_where(self, where):
        clauses = where.get("$and", [where])
        kind = next(item["type"] for item in clauses if "type" in item)
        if kind == "metric":
            return list(self.metrics)
        if kind == "dimension":
            return list(self.dimensions)
        return []


def test_generic_sales_retrieval_keeps_amount_and_quantity_candidates():
    metrics = [
        _metric("unrelated", "注册资本", [], score=0.99),
        _metric("annual_total_sales", "含税销售总额", ["销售额"], score=0.8),
        _metric("sales_total_quantity", "销售总数量", ["销量"], score=0.4),
    ]
    builder = PromptBuilder(
        _CandidateStore(metrics),
        lambda _query: [0.1],
        top_k=1,
        semantic_model_id=81,
    )

    knowledge = builder.retrieve("某产品销售趋势如何")

    assert {
        item.metadata["metric_code"] for item in knowledge["metrics"]
    } >= {"annual_total_sales", "sales_total_quantity"}


def test_sales_report_wording_keeps_bounded_sales_metric_candidates():
    metrics = [
        _metric("unrelated", "注册资本", [], score=0.99),
        _metric("annual_total_sales", "含税销售总额", ["销售额"], score=0.8),
        _metric("sales_total_quantity", "销售总数量", ["销量"], score=0.4),
    ]
    builder = PromptBuilder(
        _CandidateStore(metrics),
        lambda _query: [0.1],
        top_k=1,
        semantic_model_id=81,
    )

    knowledge = builder.retrieve(
        "我在上海卖外周插管中心静脉导管，给我生成分析报告"
    )

    assert {
        item.metadata["metric_code"] for item in knowledge["metrics"]
    } >= {"annual_total_sales", "sales_total_quantity"}


def test_sales_report_loads_sales_metrics_outside_vector_candidate_pool():
    metrics = [
        _metric("unrelated", "注册资本", [], score=0.99),
        _metric("annual_total_sales", "含税销售总额", ["销售额"], score=0.8),
        _metric("sales_total_quantity", "销售总数量", ["销量"], score=0.4),
    ]
    builder = PromptBuilder(
        _ScopeAwareCandidateStore(metrics),
        lambda _query: [0.1],
        top_k=1,
        semantic_model_id=81,
    )

    knowledge = builder.retrieve(
        "我在上海卖外周插管中心静脉导管，给我生成分析报告"
    )

    assert {
        item.metadata["metric_code"] for item in knowledge["metrics"]
    } >= {"annual_total_sales", "sales_total_quantity"}


def test_transaction_scope_loads_registered_time_dimension_outside_vector_top_k():
    dimensions = [
        _result(
            "dimension",
            "profile_created_date",
            dim_name="档案创建日期",
            dim_type="时间维度",
            granularity_support=["day"],
        ),
        _result(
            "dimension",
            "transaction_date",
            dim_name="交易日期",
            synonyms=["销售日期", "订单日期"],
            dim_type="时间维度",
            granularity_support=["day", "month"],
            bind_entities=[{
                "mappingTable": "sales_order",
                "mappingColumn": "created_date",
            }],
        ),
    ]
    builder = PromptBuilder(
        _ScopedTimeDimensionStore(
            [_metric("annual_total_sales", "含税销售总额", ["销售额"])],
            dimensions,
        ),
        lambda _query: [0.1],
        top_k=1,
        semantic_model_id=81,
    )

    knowledge = builder.retrieve(
        "TRANSACTION_TIME_SCOPE=SALES_RECORD 销售订单 交易日期"
    )

    assert "transaction_date" in {
        item.metadata["dim_code"] for item in knowledge["dimensions"]
    }
    assert _uses_transaction_activity_definition(
        "TRANSACTION_TIME_SCOPE=SALES_RECORD"
    )


def _snapshot_metric(code: str, name: str, synonyms: list[str]):
    return _result(
        "metric",
        code,
        metric_name=name,
        synonyms=synonyms,
        source_dependency={"bind_entity": ["dealer_profile"]},
        time_caliber={"time_anchor": None},
        calculation_rule={
            "calc_formula": f"{code}=MAX(dealer_result.recent_year_amount)",
        },
    )


def test_caller_bound_metric_replaces_model_metric_and_metric_sort():
    knowledge = {
        "metrics": [
            _snapshot_metric(
                "dealer_cumulative_sales_amount",
                "经销商累计销售额",
                ["累计销售额", "整体业务规模"],
            )
        ],
    }
    ast = _asl(
        metrics=[{"name": "order_count", "alias": "订单数"}],
        sort={"field_type": "metric", "field": "order_count", "direction": "DESC"},
        ambiguity=[{
            "type": "metric",
            "question": "请选择订单数或销售额",
            "candidates": ["order_count"],
        }],
    )

    normalized = json.loads(_normalize_caller_bound_metrics(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        ["dealer_cumulative_sales_amount"],
        "按整体业务规模排序",
    ))

    assert normalized["metrics"] == [{
        "name": "dealer_cumulative_sales_amount",
        "alias": "经销商累计销售额",
    }]
    assert normalized["sort"] == {
        "field_type": "metric",
        "field": "dealer_cumulative_sales_amount",
        "direction": "DESC",
    }
    assert normalized["ambiguity"] == []


def test_snapshot_ranking_uses_unique_registered_connected_sales_time_dimension():
    knowledge = {
        "metrics": [_snapshot_metric(
            "dealer_cumulative_sales_amount",
            "经销商累计销售额",
            ["整体业务规模"],
        )],
        "dimensions": [_result(
            "dimension",
            "transaction_date",
            dim_name="交易日期",
            synonyms=["成交日期", "订单日期", "销售日期"],
            dim_type="时间维度",
            granularity_support=["day", "month"],
            business_definition={"description": "销售订单真实发生时间"},
            bind_entities=[{
                "entity": "sales-order-id",
                "entityName": "销售订单",
                "attrName": "交易日期",
                "mappingTable": "sales_order",
                "mappingColumn": "created_date",
            }],
        )],
        "entities": [
            _entity("dealer", "经销商", [{
                "attr_code": "dealer_name",
                "attr_name": "经销商名称",
                "field_mapping": "dealer.dealer_name",
                "is_main_attribute": True,
            }]),
            _entity("dealer_profile", "经销商画像"),
            _entity("sales_order", "销售订单"),
        ],
        "attributes": [],
        "relations": [
            _result(
                "relation", "dealer_profile_belongs_to_dealer",
                parent="dealer_profile", target_entity="dealer",
                join_key={
                    "source_field": "dealer_result.dealer_code",
                    "target_field": "dealer.dealer_code",
                },
            ),
            _result(
                "relation", "dealer_place_sales_order",
                parent="dealer", target_entity="sales_order",
                join_key={
                    "source_field": "dealer.dealer_code",
                    "target_field": "sales_order.dealer_code",
                },
            ),
        ],
    }
    ast = _asl(
        subject={"entity": "dealer_profile"},
        metrics=[{
            "name": "dealer_cumulative_sales_amount",
            "alias": "整体业务规模",
        }],
        dimensions=[{"name": "dealer.dealer_name"}],
        filters=[{
            "field": "dealer_result.activity_level",
            "operator": "=",
            "value": "活跃",
        }],
        ambiguity=[{
            "type": "time_anchor",
            "question": "已识别时间范围，但不能确定交易时间字段。",
            "candidates": [],
        }, {
            "type": "filter",
            "question": "请确认活跃阈值。",
            "candidates": ["dealer_result.activity_level"],
        }],
    )
    query = (
        "查询经销商并按整体业务规模排序；"
        "时间范围：2025-10-17至2025-12-30；"
        "资格口径：期间内存在销售记录。"
    )

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False), knowledge, query,
    )
    result = json.loads(normalized)

    assert result["time_context"]["anchor"] == "sales_order.created_date"
    assert result["time_context"]["start"] == "2025-10-17"
    assert result["time_context"]["end"] == "2025-12-30"
    assert result["filters"] == []
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, query)


def test_precomputed_rolling_metric_does_not_create_external_time_ambiguity():
    metric = _snapshot_metric(
        "dealer_recent_year_sales",
        "经销商近一年销售额",
        ["近一年销售额"],
    )
    knowledge = {
        "metrics": [metric],
        "dimensions": [],
        "entities": [_entity("dealer_profile", "经销商画像")],
        "attributes": [],
        "relations": [],
    }
    ast = _asl(
        subject={"entity": "dealer_profile"},
        metrics=[{"name": "dealer_recent_year_sales", "alias": "近一年销售额"}],
        time_context={
            "type": "range",
            "start": "2025-01-01",
            "end": "2025-12-31",
            "unit": "month",
            "anchor": "sales_order.created_date",
        },
        ambiguity=[{
            "type": "time_anchor",
            "question": "近一年是否需要指定交易日期？",
            "candidates": [],
        }],
    )

    normalized = _normalize_semantic_references(
        json.dumps(ast, ensure_ascii=False),
        knowledge,
        "按经销商近一年销售额推荐TOP3经销商",
    )
    result = json.loads(normalized)

    assert result["time_context"] is None
    assert result["ambiguity"] == []
    _validate_asl_output(normalized, knowledge, "按经销商近一年销售额推荐TOP3经销商")


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("统计2025年销售总额", ("2025-01-01", "2026-01-01", "custom")),
        ("统计2025年10月销售总额", ("2025-10-01", "2025-11-01", "custom")),
    ),
)
def test_calendar_year_and_month_are_executable_time_bounds(query, expected):
    assert _query_date_bounds(query) == expected
