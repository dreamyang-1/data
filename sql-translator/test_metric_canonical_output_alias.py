"""Column labels may change; a metric's calculation must not change with them."""
from unittest.mock import Mock

import pytest

from sql_translator_prod import SQLTranslatorProd


@pytest.mark.parametrize("old_alias", ["", " AS `医院总数`", " as hospital_count", ' AS "医院总数"', " AS '医院总数';"])
@pytest.mark.parametrize("formula", ["COUNT(DISTINCT hospital.hospital_code)", "CAST(COUNT(*) AS UNSIGNED)"])
def test_standard_asl_label_overrides_only_old_output_alias(old_alias, formula):
    translator = SQLTranslatorProd.__new__(SQLTranslatorProd)
    translator._get_entity_base_table = Mock(return_value="hospital")
    translator._get_metric = Mock(return_value={"calculation_rule": {"calc_formula": formula + old_alias}})
    metrics = [{"name": "total_hospitals_in_region", "alias": "区域全部医院总数"}]
    selected = translator._build_select_clause(metrics, [], "hospital")
    ordered = translator._build_order_by_clause(
        {"field": "total_hospitals_in_region", "field_type": "metric", "direction": "DESC"}, metrics, [])
    assert selected == formula + " AS `区域全部医院总数`"
    assert ordered == "ORDER BY `区域全部医院总数` DESC"


def test_legacy_null_alias_keeps_formula_owned_label():
    translator = SQLTranslatorProd.__new__(SQLTranslatorProd)
    translator._get_entity_base_table = Mock(return_value="hospital")
    translator._get_metric = Mock(return_value={"calculation_rule": {"calc_formula": "COUNT(*) AS `固定列名`"}})
    assert translator._build_select_clause([{"name": "count", "alias": None}], [], "hospital") == "COUNT(*) AS `固定列名`"


def test_standard_alias_with_backtick_is_escaped_as_identifier():
    translator = SQLTranslatorProd.__new__(SQLTranslatorProd)
    translator._get_entity_base_table = Mock(return_value="hospital")
    translator._get_metric = Mock(return_value={"calculation_rule": {"calc_formula": "COUNT(*)"}})
    assert translator._build_select_clause([{"name": "count", "alias": "登记`总数"}], [], "hospital") == "COUNT(*) AS `登记``总数`"
    for kind in ("metric", None):
        assert translator._build_order_by_clause(
            {"field": "count", "field_type": kind}, [{"name": "count", "alias": "登记`总数"}], [],
        ) == "ORDER BY `登记``总数` ASC"
