import json
import pytest
from sql_translator_prod import _normalize_global_filters, _normalize_asl_compatibility
from test_sql_translator_hardening import translator, base_ast


def test_translation_accepts_null_lists_numeric_limit_and_repeated_metric():
    ast = base_ast(metrics=[{"name": "sales"}, {"name": "sales"}],
                   dimensions=None, filters=None, having=None, limit="25")
    sql = translator().translate(json.dumps(ast), "6")
    assert sql.count("SUM(") == 1
    assert "LIMIT 25" in sql


@pytest.mark.parametrize("kind, expected", [(" INCLUDE ", "include"), ("Exclude", "exclude"), (" eq ", "include")])
def test_singleton_filter_configuration_and_case_variants(kind, expected):
    item = {"filterCondition": "orders.active = 1", "filterType": kind}
    for value in (item, json.dumps(item), [item]):
        assert _normalize_global_filters(value) == [
            {"condition": "orders.active = 1", "filter_type": expected}]


def test_empty_catalog_column_means_no_fixed_filters():
    assert _normalize_global_filters("  ") == []


def test_unknown_filter_semantics_and_broken_conditions_still_rejected():
    for item in ({"condition": "x=1", "filter_type": "maybe"}, {"condition": ""}):
        with pytest.raises(ValueError):
            _normalize_global_filters(item)


def test_filter_value_and_conflicting_dimensions_are_preserved():
    ast = {"filters": [{"field": "x.name", "operator": " != ", "value": " A "}],
           "dimensions": [{"name": "x.day", "granularity": "month"},
                          {"name": "x.day", "granularity": "year"}]}
    result = _normalize_asl_compatibility(ast)
    assert result["filters"][0]["value"] == " A "
    assert len(result["dimensions"]) == 2


def test_public_translation_normalizes_before_filter_summary():
    result = translator().translate_only(json.dumps(base_ast(
        dimensions=None, filters=None, having=None, limit="25")), "6")
    assert result["success"], result


def test_scoped_entry_normalizes_before_source_planning():
    from semantic_scope import ScopedTranslator, RequestScope, ScopeError
    instance = object.__new__(ScopedTranslator)
    instance.scope = RequestScope.from_request({"modelId": "81", "business_domain_ids": [205]})
    seen = []
    def plan(ast, model):
        seen.append(ast)
        raise ScopeError("PROBE_STOP", "stop before any catalog access")
    instance._plan_sources = plan
    result = instance.translate_only(json.dumps({"filters": None, "dimensions": None}), "81")
    assert result["code"] == "PROBE_STOP"
    assert seen[0]["filters"] == seen[0]["dimensions"] == []
