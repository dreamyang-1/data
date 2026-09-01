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
    assert "当前条件" in message
    assert "FILTER_NOT_SUPPORTED" not in message


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
