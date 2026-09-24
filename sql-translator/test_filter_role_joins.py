"""Grouping is not evidence of a filter's business owner."""
import pytest

from test_sql_translator_hardening import translator


def role_translator():
    value = translator()
    value.loader.entities = {
        code: {"entity_code": code, "physical_table_join": {"base_table": table},
               "attributes": [], "relations": [], "sub_table_mappings": []}
        for code, table in [("hospital", "hospital"), ("dealer", "dealer"),
                            ("city", "dim_city"), ("province", "dim_province")]
    }
    value.loader.entities["hospital"]["relations"] = [
        {"target_entity": "dealer", "join_key": "hospital.dealer_id = dealer.id"},
        {"target_entity": "city", "join_key": "hospital.city_id = dim_city.id"},
    ]
    value.loader.entities["dealer"]["relations"] = [
        {"target_entity": "city", "join_key": "dealer.city_id = dim_city.id"},
    ]
    value.loader.entities["city"]["relations"] = [
        {"target_entity": "province", "join_key": "dim_city.province_id = dim_province.id"},
    ]
    value.loader.dimensions = {"dealer": {
        "dim_code": "dealer", "field_mapping": {"dim_table_field": "dealer.name"},
        "bind_entities": [{"entity_code": "dealer"}],
    }}
    return value


def test_grouped_dealer_does_not_override_subject_city_route():
    value = role_translator()
    joins = " ".join(value._detect_additional_joins(
        "hospital", [{"name": "dealer"}],
        [{"field": "dim_city.name", "operator": "=", "value": "上海市"}], model_id="81",
    ))
    assert "hospital.city_id = dim_city.id" in joins
    assert "dealer.city_id = dim_city.id" not in joins


@pytest.mark.parametrize("owner", ["hospital", "dealer"])
def test_role_owned_foreign_key_keeps_its_filter_without_shared_city_join(owner):
    value = role_translator()
    filters = [{"field": owner + ".city_id", "operator": "=", "value": "310100"}]
    joins = " ".join(value._detect_additional_joins(
        "hospital", [{"name": "dealer.name"}], filters, model_id="81",
    ))
    assert "dim_city" not in joins
    assert filters[0]["field"] == owner + ".city_id"


def test_explicit_catalog_hierarchy_still_extends_grouped_city():
    value = role_translator()
    value.loader.dimensions["city"] = {
        "dim_code": "city", "level_list": ["province", "city"],
        "field_mapping": {"dim_table_field": "dim_city.name"},
        "bind_entities": [{"entity_code": "city"}],
    }
    joins = " ".join(value._detect_additional_joins(
        "hospital", [{"name": "city"}],
        [{"field": "dim_province.name", "operator": "=", "value": "上海市"}], model_id="81",
    ))
    assert "dim_city.province_id = dim_province.id" in joins
