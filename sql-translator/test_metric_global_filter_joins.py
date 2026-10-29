"""Regression: metric global filter tables must join into FROM.

Production incident (model 81, 已合作医院数): the metric's global filter
``sales_order.quantity > 0`` was concatenated verbatim into WHERE while the
FROM clause only contained dealer/dim_city/dim_province/hospital, so MySQL
returned 1054 (Unknown column 'sales_order.quantity'). The hospital table
itself took the shortest path dealer -> dim_city -> hospital, which counts
hospitals in the dealer's city instead of hospitals reached through orders.

These tests pin both behaviors:
    1. every table referenced by a merged global filter condition is joined;
    2. metric formula tables prefer anchoring on the global filter table,
       so the aggregation path passes through the metric's fact table.
"""
import json
import unittest

from sql_translator_prod import SQLTranslatorProd


class GlobalFilterJoinLoader:
    """Duck-typed loader mirroring the hardening suite fixture."""

    def __init__(self):
        self.entities = {
            "dealer": {
                "entity_code": "dealer",
                "entity_name": "经销商",
                "physical_table_join": {"base_table": "dealer"},
                "attributes": [
                    {"field_mapping": "dealer.dealer_code", "is_primary_key": True},
                    {"field_mapping": "dealer.dealer_name", "is_main_attribute": True},
                ],
                "relations": [{
                    "target_entity": "city",
                    "join_key": "dealer.city_id = dim_city.city_id",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "sales_order": {
                "entity_code": "sales_order",
                "entity_name": "销售订单",
                "physical_table_join": {"base_table": "sales_order"},
                "attributes": [
                    {"field_mapping": "sales_order.dealer_code"},
                    {"field_mapping": "sales_order.hospital_id"},
                    {"field_mapping": "sales_order.quantity"},
                ],
                "relations": [
                    {"target_entity": "dealer",
                     "join_key": "sales_order.dealer_code = dealer.dealer_code"},
                    {"target_entity": "hospital",
                     "join_key": "sales_order.hospital_id = hospital.hospital_id"},
                ],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "hospital": {
                "entity_code": "hospital",
                "entity_name": "医院",
                "physical_table_join": {"base_table": "hospital"},
                "attributes": [
                    {"field_mapping": "hospital.hospital_id", "is_primary_key": True},
                    {"field_mapping": "hospital.hospital_code"},
                ],
                # 与订单表并行的城市捷径：未经口径表时不应被公式表选中
                "relations": [{
                    "target_entity": "city",
                    "join_key": "hospital.city_id = dim_city.city_id",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "city": {
                "entity_code": "city",
                "entity_name": "城市",
                "physical_table_join": {"base_table": "dim_city"},
                "attributes": [{"field_mapping": "dim_city.city_id"}],
                "relations": [{
                    "target_entity": "province",
                    "join_key": "dim_city.province_id = dim_province.province_id",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "province": {
                "entity_code": "province",
                "entity_name": "省份",
                "physical_table_join": {"base_table": "dim_province"},
                "attributes": [{"field_mapping": "dim_province.province_name"}],
                "relations": [],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
        }
        self.metrics = {
            "cooperating_hospital_count": {
                "metric_code": "cooperating_hospital_count",
                "metric_name": "已合作医院数",
                "metric_level": "原子指标",
                "calculation_rule": {
                    "calc_formula": (
                        "cooperating_hospital_count="
                        "COUNT(DISTINCT hospital.hospital_code)"
                    ),
                    "global_filters": [
                        {"condition": "sales_order.quantity > 0",
                         "filter_type": "include"},
                    ],
                    "depend_metrics": [],
                },
                "source_dependency": {"bind_entity": ["sales_order"]},
                "params": {},
            },
        }
        self.dimensions = {}

    def get_entity(self, code, model_id=None):
        return self.entities.get(code)

    def get_metric(self, code, model_id=None):
        return self.metrics.get(code)

    def get_dimension(self, code, model_id=None):
        return self.dimensions.get(code)

    def iter_dimensions(self, model_id):
        return list(self.dimensions.values())

    def iter_metrics(self, model_id):
        return list(self.metrics.values())

    def iter_entities(self, model_id):
        return list(self.entities.values())

    def find_all_entity_codes_by_table(self, table_name, model_id=None):
        return [code for code, entity in self.entities.items()
                if entity["physical_table_join"]["base_table"] == table_name]

    def _find_entity_code_by_table(self, table_name, model_id=None):
        values = self.find_all_entity_codes_by_table(table_name, model_id)
        return values[0] if values else None


def translator():
    value = SQLTranslatorProd.__new__(SQLTranslatorProd)
    value.loader = GlobalFilterJoinLoader()
    value.catalog = None
    return value


def dealer_subject_ast():
    return {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "dealer"},
        "metrics": [{"name": "cooperating_hospital_count",
                     "alias": "已合作医院数"}],
        "dimensions": [{"name": "dealer", "alias": "经销商名称"}],
        "filters": [{"field": "dim_province.province_name", "operator": "=",
                     "value": "上海市"}],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": [],
    }


class GlobalFilterJoinTests(unittest.TestCase):
    def test_filter_table_joins_and_formula_routes_through_it(self):
        sql = translator().translate(json.dumps(dealer_subject_ast()), "81")

        self.assertIn(
            "LEFT JOIN sales_order ON "
            "sales_order.dealer_code = dealer.dealer_code", sql)
        self.assertIn(
            "LEFT JOIN hospital ON "
            "sales_order.hospital_id = hospital.hospital_id", sql)
        # 城市捷径不允许承担指标口径路径
        self.assertNotIn("hospital.city_id = dim_city.city_id", sql)
        self.assertIn("sales_order.quantity > 0", sql)
        self.assertIn("dim_province.province_name = '上海市'", sql)

    def test_subject_on_filter_table_keeps_single_fact_join(self):
        ast = dealer_subject_ast()
        ast["subject"] = {"entity": "sales_order"}
        ast["dimensions"] = [{"name": "dealer", "alias": "经销商名称"}]

        sql = translator().translate(json.dumps(ast), "81")

        self.assertEqual(sql.count("LEFT JOIN sales_order"), 0)
        self.assertIn(
            "LEFT JOIN hospital ON "
            "sales_order.hospital_id = hospital.hospital_id", sql)
        self.assertNotIn("hospital.city_id = dim_city.city_id", sql)
        self.assertIn("sales_order.quantity > 0", sql)


if __name__ == "__main__":
    unittest.main()
