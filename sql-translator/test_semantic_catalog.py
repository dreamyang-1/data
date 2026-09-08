import unittest

from sql_translator_prod import SemanticCatalog


class FakeCatalog(SemanticCatalog):
    def __init__(self):
        super().__init__({})

    def _query(self, sql, params=()):
        if "FROM semantic_model_indicator" in sql and "indicator_code=%s" not in sql:
            return [
                {
                    "indicator_code": "actual_payment_amount",
                    "indicator_name": "实付金额",
                    "synonyms": '"支付金额", "交易额", "GMV", "销售额"',
                    "unit": "元",
                    "business_domain_id": 7,
                },
                {
                    "indicator_code": "refund_amount",
                    "indicator_name": "退款金额",
                    "synonyms": '["退款额"]',
                    "unit": "元",
                    "business_domain_id": 10,
                },
            ]
        if "FROM semantic_model_indicator" in sql:
            return [{
                "id": 1,
                "project_id": 2,
                "semantic_model_id": 6,
                "business_domain_id": 7,
                "indicator_code": "actual_payment_amount",
                "indicator_name": "实付金额",
                "indicator_level": 1,
                "unit": "元",
                "format_rule": "#,##0.00",
                "business_desc": "有效支付订单实付金额",
                "synonyms": '["销售额", "GMV"]',
                "applicable_scenarios": '["经营看板"]',
                "dependence_atomic_indicator": None,
                "calculation_formula": "actual_payment_amount=SUM(order_info.pay_amount)",
                "global_filters": '[]',
                "indicator_logic": None,
                "create_time": None,
                "update_time": None,
            }]
        if "FROM semantic_model_entity_bind_indicator" in sql:
            return [{
                "entity_code": "ent_order",
                "resolved_entity_code": "ent_order",
                "indicator_logic": None,
                "entity_name": "订单",
                "main_table_name": "order_info",
                "data_source_id": 5,
            }]
        if "FROM semantic_model_table" in sql:
            return [{
                "id": 10, "name": "order_info", "data_source_id": 5,
                "description": "订单事实表", "comment": None,
            }]
        if "FROM semantic_model_field" in sql:
            return [{
                "id": 11, "name": "pay_amount", "type": "DECIMAL",
                "null_flag": "NO", "description": "实付金额",
                "comment": None, "table_name": "order_info",
            }]
        if "FROM semantic_model_relation_config" in sql:
            return []
        if "FROM semantic_model_entity_type" in sql:
            return [{
                "entity_code": "ent_order", "indicator_logic": None,
                "entity_name": "订单", "main_table_name": "order_info",
                "data_source_id": 5,
            }]
        raise AssertionError(sql)


class DerivedCatalog(FakeCatalog):
    def definition(self, metric_id, version, model_id=None):
        result = super().definition("6:actual_payment_amount", version, model_id)
        result.update({
            "metric_id": "6:derived_rate", "metric_code": "derived_rate",
            "metric_name": "衍生比率", "calculation_formula": "base_sales / 100",
            "depend_metrics": ["base_sales"],
        })
        return result

    def _metric_row(self, model_id, code):
        if code == "base_sales":
            return {
                "indicator_code": code, "indicator_name": "基础销售额",
                "calculation_formula": "SUM(order_info.pay_amount)",
                "global_filters": "[]", "dependence_atomic_indicator": None,
            }
        return super()._metric_row(model_id, code)


class SemanticCatalogTests(unittest.TestCase):
    def test_resolves_code_name_and_non_json_synonym_list(self):
        catalog = FakeCatalog()
        for text in ("actual_payment_amount", "实付金额", "销售额", "GMV"):
            result = catalog.resolve_metrics(6, [text])
            self.assertEqual("RESOLVED", result["status"])
            self.assertEqual("6:actual_payment_amount", result["metrics"][0]["metric_id"])

    def test_unknown_metric_is_not_fuzzy_guessed(self):
        result = FakeCatalog().resolve_metrics(6, ["金额"])
        self.assertEqual("NOT_FOUND", result["status"])
        self.assertEqual(["金额"], result["unresolved"])

    def test_fractional_or_boolean_model_scope_is_rejected(self):
        for invalid in (6.5, True, "6:*"):
            with self.assertRaisesRegex(ValueError, "正整数"):
                FakeCatalog().resolve_metrics(invalid, ["销售额"])

    def test_definition_contract_contains_formula_and_entity_binding(self):
        result = FakeCatalog().definition("6:actual_payment_amount", "current")
        self.assertEqual("actual_payment_amount", result["metric_code"])
        self.assertEqual("SUM(order_info.pay_amount)", result["calculation_formula"].split("=", 1)[1])
        self.assertEqual("ent_order", result["bound_entities"][0]["entity_code"])

    def test_lineage_is_deterministic_and_grounded_in_formula(self):
        result = FakeCatalog().lineage("6:actual_payment_amount", "current")
        self.assertEqual(["order_info"], result["source_tables"])
        self.assertEqual(["order_info.pay_amount"], result["source_fields"])
        node_types = {node["type"] for node in result["nodes"]}
        self.assertEqual({"METRIC", "ENTITY", "TABLE", "FIELD"}, node_types)
        field = next(node for node in result["nodes"] if node["type"] == "FIELD")
        self.assertTrue(field["registered"])
        self.assertEqual("DECIMAL", field["data_type"])
        self.assertEqual([], result["metadata_warnings"])
        self.assertEqual("2.0", result["lineage_version"])
        self.assertEqual(["业务域:7", "订单", "实付金额"], result["business_lineage"])
        self.assertEqual("order_info", result["physical_sources"][0]["table"])
        self.assertEqual("pay_amount", result["physical_sources"][0]["field"])
        self.assertEqual("SEMANTIC_FORMULA", result["column_lineage"][0]["source"])
        self.assertEqual("6:actual_payment_amount", result["column_lineage"][0]["to_column"])

    def test_plain_metric_id_requires_explicit_model_scope(self):
        with self.assertRaisesRegex(ValueError, "semantic_model_id:metric_code"):
            FakeCatalog().definition("actual_payment_amount", "current")

    def test_derived_metric_lineage_expands_declared_dependencies(self):
        result = DerivedCatalog().lineage("6:derived_rate", "current")
        self.assertEqual(["order_info.pay_amount"], result["source_fields"])
        self.assertEqual("6:base_sales", result["dependency_metrics"][0]["metric_id"])
        self.assertIn(
            ("6:base_sales", "6:derived_rate", "DEPENDS_ON"),
            {(edge["from"], edge["to"], edge["type"]) for edge in result["edges"]},
        )


if __name__ == "__main__":
    unittest.main()
