import json
import pytest
from agent import _normalize_asl_compatibility, _validate_asl_output
from test_hardening import _asl, _result


def test_duplicate_metrics_and_dimensions_are_normalized_not_rejected():
    # STALE_TEST: duplicate-only rejection replaced by user-authorized repair.
    knowledge = {"metrics": [_result("metric", "metric_code", "sales")],
                 "dimensions": [_result("dimension", "dim_code", "stat_date")],
                 "entities": [_result("entity", "entity_code", "order")]}
    ast = _asl(metrics=[{"name": "sales"}] * 2,
               dimensions=[{"name": "stat_date"}] * 2)
    result = json.loads(_validate_asl_output(json.dumps(ast), knowledge))
    assert len(result["metrics"]) == len(result["dimensions"]) == 1


def test_format_repairs_preserve_filter_values_and_negative_operator():
    ast = _normalize_asl_compatibility({
        "metrics": None, "dimensions": [{"name": "orders.day", "attr": "day", "granularity": " MONTH "}],
        "filters": [{"field": "orders.name", "operator": " not in ", "value": [" A "]}],
        "limit": " 25 ", "intent": " QUERY ", "version": 2.0,
        "sort": {"direction": " desc ", "field_type": " METRIC "},
    })
    assert ast["metrics"] == ast["having"] == ast["ambiguity"] == []
    assert ast["limit"] == 25 and ast["version"] == "2.0"
    assert ast["dimensions"][0]["attr"] is None
    assert ast["filters"][0]["operator"] == "NOT IN"
    assert ast["filters"][0]["value"] == [" A "]


@pytest.mark.parametrize("value", [True, -1, "all", "1; DROP TABLE t"])
def test_invalid_limits_not_repaired(value):
    assert _normalize_asl_compatibility({"limit": value})["limit"] == value


def test_conflicting_dimensions_are_not_silently_deduplicated():
    items = [{"name": "day", "granularity": "month"}, {"name": "day", "granularity": "year"}]
    assert _normalize_asl_compatibility({"dimensions": items})["dimensions"] == items
