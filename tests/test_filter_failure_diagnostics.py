"""ASL failure literals reach the user without blaming unrelated conditions."""
import httpx
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import PlatformHttpClient
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.dependency_error_messages import render_dependency_error


@pytest.mark.parametrize("operator,value,expected", [
    ("=", 1000, "order.amount=1000"),
    ("=", 0, "order.amount=0"),
    ("=", False, "order.amount=False"),
    (">", 1000, "order.amount>1000"),
    ("BETWEEN", [0, 1000], "order.amount BETWEEN 0,1000"),
    ("IN", ["甲", "乙"], "order.amount IN 甲,乙"),
])
def test_http_boundary_and_user_message_preserve_exact_failed_filter(operator, value, expected):
    response = httpx.Response(502, json={"detail": {
        "code": "ASL_FILTER_INVALID", "field": "order.amount",
        "details": {"validation_stage": "vector_grounding", "failed_filter": {
            "field": "order.amount", "operator": operator, "value": value,
        }},
    }})
    details = PlatformHttpClient._upstream_error_details(response)
    assert details["failed_filter"]["value"] == value
    exc = AdapterError("DEPENDENCY_UNAVAILABLE", "sanitized", status_code=502,
                       upstream_code="ASL_FILTER_INVALID", details=details)
    message = DataAnalysisOrchestrator._dependency_message(exc)
    assert expected in message
    assert "向量目录范围校验" in message
    assert "首先失败的条件" in message
    assert "也未执行数据查询" in message
    assert "上海" not in message and "费森尤斯" not in message


def test_old_error_without_subject_admits_missing_diagnostics():
    exc = AdapterError("ASL_FILTER_INVALID", "private upstream error")
    message = render_dependency_error(exc)
    assert "未返回具体失败字段和值" in message
    assert "暂时无法判断是哪一项" in message
    assert "筛选条件当前筛选条件" not in message
    assert "private upstream error" not in message


def test_legacy_expected_filter_preserves_real_operator_and_zero():
    exc = AdapterError("ASL_FILTER_INVALID", "sanitized", details={
        "expected_filter": {"field": "金额", "operator": ">", "value": 0},
    })
    message = render_dependency_error(exc)
    assert "金额>0" in message
    assert "金额=0" not in message
