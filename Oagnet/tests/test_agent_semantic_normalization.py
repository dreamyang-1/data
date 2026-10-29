import json
import unittest
from types import SimpleNamespace

from agent import _normalize_dynamic_subject, _normalize_semantic_references


class SemanticReferenceNormalizationTests(unittest.TestCase):
    def test_replaces_legacy_time_dimension_and_selects_anchor_binding(self):
        ast = {
            "subject": {"entity": "ent_order"},
            "metrics": [{"name": "actual_payment_amount"}],
            "dimensions": [{"name": "dim_date", "attr": None, "granularity": "month"}],
            "time_context": {"anchor": "order_info.pay_time"},
            "sort": {"field": "dim_date", "field_type": "dimension", "direction": "ASC"},
        }
        metadata = {
            "dim_code": "statistical_date",
            "dim_type": "\u65f6\u95f4\u7ef4\u5ea6",
            "granularity_support": json.dumps(["\u65e5", "\u6708"]),
            "bind_metrics": json.dumps(["total_shop_count", "actual_payment_amount"]),
            "bind_entities": json.dumps([
                {"attr": "shop-open", "mappingTable": "shop_info", "mappingColumn": "open_date"},
                {"attr": "2085638300381929475", "mappingTable": "order_info", "mappingColumn": "pay_time"},
            ]),
        }
        metric_metadata = {
            "metric_code": "actual_payment_amount",
            "source_dependency": json.dumps({
                "bind_entity": ["sales_transaction_domain_ent_order"]
            }),
        }
        result = json.loads(_normalize_semantic_references(
            json.dumps(ast),
            {
                "dimensions": [SimpleNamespace(metadata=metadata)],
                "metrics": [SimpleNamespace(metadata=metric_metadata)],
            },
            "\u5206\u67902026\u5e741\u6708\u81f37\u6708\u9500\u552e\u989d\u8d8b\u52bf",
        ))
        self.assertEqual("statistical_date", result["dimensions"][0]["name"])
        self.assertEqual("2085638300381929475", result["dimensions"][0]["attr"])
        self.assertEqual("month", result["dimensions"][0]["granularity"])
        self.assertEqual("month", result["time_context"]["unit"])
        self.assertEqual("statistical_date", result["sort"]["field"])
        self.assertEqual(
            "sales_transaction_domain_ent_order",
            result["subject"]["entity"],
        )

    def test_invalid_model_output_is_left_unchanged(self):
        self.assertEqual("not-json", _normalize_semantic_references("not-json", {}))

    def test_subject_uses_live_entity_for_anchor_table(self):
        content = json.dumps({
            "subject": {"entity": "ent_order"},
            "time_context": {"anchor": "order_info.pay_time"},
        })
        result = json.loads(_normalize_dynamic_subject(
            content,
            6,
            7,
            resolver=lambda sm, bd, table: "sales_transaction_domain_ent_order",
        ))
        self.assertEqual("sales_transaction_domain_ent_order", result["subject"]["entity"])

    def test_subject_uses_selected_metric_formula_without_time_context(self):
        content = json.dumps({
            "subject": {"entity": "wrong_entity"},
            "metrics": [{"name": "actual_payment_amount"}],
            "time_context": None,
        })
        metric = SimpleNamespace(metadata={
            "metric_code": "actual_payment_amount",
            "source_dependency": json.dumps({"bind_entity": []}),
            "calculation_rule": json.dumps({
                "calc_formula": "actual_payment_amount=SUM(order_info.pay_amount)"
            }),
        })
        result = json.loads(_normalize_dynamic_subject(
            content,
            6,
            None,
            {"metrics": [metric]},
            resolver=lambda sm, bd, table: "sales_transaction_domain_ent_order",
        ))
        self.assertEqual("sales_transaction_domain_ent_order", result["subject"]["entity"])

    def test_subject_prefers_metric_global_filter_population_table(self):
        content = json.dumps({
            "subject": {"entity": "hospital"},
            "metrics": [{"name": "cooperating_hospital_count"}],
            "time_context": None,
        })
        metric = SimpleNamespace(metadata={
            "metric_code": "cooperating_hospital_count",
            "source_dependency": json.dumps({"bind_entity": ["hospital"]}),
            "calculation_rule": json.dumps({
                "calc_formula": (
                    "cooperating_hospital_count="
                    "COUNT(DISTINCT hospital.hospital_code)"
                ),
                "global_filters": [{
                    "filterCondition": "sales_order.quantity > 0",
                }],
            }),
        })

        result = json.loads(_normalize_dynamic_subject(
            content,
            81,
            [205],
            {"metrics": [metric]},
            resolver=lambda _sm, _bd, table: {
                "sales_order": "sales_order",
                "hospital": "hospital",
            }.get(table),
        ))

        self.assertEqual("sales_order", result["subject"]["entity"])

    @staticmethod
    def _entity(code: str, table: str, column: str):
        return SimpleNamespace(metadata={
            "entity_code": code,
            "entity_name": code,
            "attributes": json.dumps([{
                "attr_code": column,
                "field_mapping": {"mappingTable": table, "mappingColumn": column},
            }]),
        })

    @staticmethod
    def _relation(source_field: str, target_field: str):
        return SimpleNamespace(metadata={
            "join_key": json.dumps({
                "source_field": source_field,
                "target_field": target_field,
            }),
        })

    def test_detail_projection_recovers_unique_hub_subject(self):
        knowledge = {
            "entities": [
                self._entity("sales_order", "sales_order", "order_id"),
                self._entity("product", "product", "product_name"),
                self._entity("dealer", "dealer", "dealer_name"),
            ],
            "relations": [
                self._relation("sales_order.product_id", "product.product_id"),
                self._relation("sales_order.dealer_id", "dealer.dealer_id"),
            ],
        }
        content = json.dumps({
            "subject": {"entity": None},
            "metrics": [],
            "dimensions": [{"name": "product.product_name"}],
            "filters": [{
                "field": "dealer.dealer_name",
                "operator": "=",
                "value": "南京大乾医疗用品有限公司",
            }],
        })
        result = json.loads(_normalize_dynamic_subject(
            content, 81, [205], knowledge, resolver=lambda *_args: None,
        ))
        self.assertEqual("sales_order", result["subject"]["entity"])
        self.assertIn("sales_order", knowledge["_resolved_subjects"])

    def test_detail_projection_ambiguous_hubs_fail_closed(self):
        knowledge = {
            "entities": [
                self._entity("hub_a", "hub_a", "key_id"),
                self._entity("hub_b", "hub_b", "key_id"),
                self._entity("product", "product", "product_name"),
                self._entity("dealer", "dealer", "dealer_name"),
            ],
            "relations": [
                self._relation("hub_a.product_id", "product.product_id"),
                self._relation("hub_a.dealer_id", "dealer.dealer_id"),
                self._relation("hub_b.product_id", "product.product_id"),
                self._relation("hub_b.dealer_id", "dealer.dealer_id"),
            ],
        }
        content = json.dumps({
            "subject": {"entity": None},
            "metrics": [],
            "dimensions": [{"name": "product.product_name"}],
            "filters": [{"field": "dealer.dealer_name", "operator": "=", "value": "X"}],
        })
        result = json.loads(_normalize_dynamic_subject(
            content, 81, [205], knowledge, resolver=lambda *_args: None,
        ))
        self.assertIsNone(result["subject"]["entity"])

    def test_detail_projection_keeps_existing_subject_and_metric_queries(self):
        knowledge = {
            "entities": [
                self._entity("sales_order", "sales_order", "order_id"),
                self._entity("product", "product", "product_name"),
            ],
            "relations": [
                self._relation("sales_order.product_id", "product.product_id"),
            ],
        }
        kept = json.dumps({
            "subject": {"entity": "product"},
            "metrics": [],
            "dimensions": [{"name": "product.product_name"}],
            "filters": [],
        })
        self.assertEqual(
            "product",
            json.loads(_normalize_dynamic_subject(
                kept, 81, [205], knowledge, resolver=lambda *_args: None,
            ))["subject"]["entity"],
        )
        metric_query = json.dumps({
            "subject": {"entity": None},
            "metrics": [{"name": "actual_payment_amount"}],
            "dimensions": [{"name": "product.product_name"}],
            "filters": [],
        })
        self.assertIsNone(json.loads(_normalize_dynamic_subject(
            metric_query, 81, [205], knowledge, resolver=lambda *_args: None,
        ))["subject"]["entity"])


if __name__ == "__main__":
    unittest.main()
