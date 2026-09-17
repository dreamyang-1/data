from app.adapters.base import AdapterError
from app.services.orchestrator import DataAnalysisOrchestrator


def test_dependency_message_uses_http_status_category() -> None:
    error = AdapterError(
        "DEPENDENCY_CONTRACT_REJECTED",
        "sanitized",
        status_code=422,
        upstream_code="FILTER_NOT_SUPPORTED",
    )
    message = DataAnalysisOrchestrator._dependency_message(error)
    assert "拒绝了当前查询合同" in message
    assert "FILTER_NOT_SUPPORTED" in message
    assert "无需盲目改写问题" in message


def test_dependency_message_keeps_stable_upstream_code_for_unknown_failure() -> None:
    error = AdapterError(
        "DEPENDENCY_CONTRACT_REJECTED",
        "sanitized",
        status_code=400,
        upstream_code="BUSINESS_SCOPE_INVALID",
    )
    message = DataAnalysisOrchestrator._dependency_message(error)
    assert "BUSINESS_SCOPE_INVALID" in message


def test_time_anchor_configuration_error_does_not_tell_user_to_blindly_retry() -> None:
    error = AdapterError(
        "DEPENDENCY_UNAVAILABLE",
        "sanitized",
        status_code=502,
        upstream_code="ASL_TIME_ANCHOR_INVALID",
    )
    message = DataAnalysisOrchestrator._dependency_message(error)
    assert "时间字段绑定" in message
    assert "稍后重试" not in message


def test_intent_contract_error_is_not_reported_as_upstream_outage() -> None:
    error = AdapterError(
        "DEPENDENCY_CONTRACT_REJECTED",
        "sanitized",
        status_code=422,
        upstream_code="INTENT_ASL_CONTRACT_INCOMPLETE",
    )

    message = DataAnalysisOrchestrator._dependency_message(error)

    assert "没有完整保留" in message
    assert "停止执行" in message
    assert "上游数据服务" not in message
    assert "稍后重试" not in message


def test_unresolved_entity_message_names_the_exact_unresolved_value() -> None:
    error = AdapterError(
        "DEPENDENCY_CONTRACT_REJECTED",
        "sanitized",
        status_code=502,
        upstream_code="ASL_ENTITY_MENTION_UNRESOLVED",
        details={"path": "/agent/query", "mention": "费森尤斯"},
    )

    message = DataAnalysisOrchestrator._dependency_message(error)

    assert "“费森尤斯”" in message
    assert "它属于" not in message
    assert "用户可补充" in message
    assert "语义层需配置" in message


def test_filter_error_reports_actual_filter_and_candidates() -> None:
    error = AdapterError(
        "DEPENDENCY_CONTRACT_REJECTED",
        "sanitized",
        status_code=502,
        upstream_code="ASL_FILTER_INVALID",
        details={
            "semantic_field": "商品品类",
            "candidates": ["product.product_name", "product.product_code"],
        },
    )

    message = DataAnalysisOrchestrator._dependency_message(error)

    assert "“商品品类”" in message
    assert "产品名称（product.product_name）" in message
    assert "产品编码（product.product_code）" in message
    assert "字段角色和关系路径" in message


def test_sql_operator_error_explains_the_preserved_filter() -> None:
    error = AdapterError(
        "SQL_QUERY_FILTER_OPERATOR_FAILED",
        "sanitized",
        details={
            "filters": [{
                "field": "manufacturer.parent_brand",
                "value": "费森尤斯",
                "expected_operator": "EQ",
            }],
        },
    )

    message = DataAnalysisOrchestrator._dependency_message(error)

    assert "母厂牌（manufacturer.parent_brand）=费森尤斯" in message
    assert "改变了精确匹配方式" in message
    assert "用户无需反复改写" in message
