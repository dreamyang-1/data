"""Regression: metric global filters must never reach SQL as dict text.

Production chain: the catalog loader stringified published filter dicts
(``_list_value``), so ``_build_filter_clause`` concatenated Python repr text
into WHERE and MySQL returned 1064. These tests cover every hop:

    raw catalog column -> SemanticCatalog.definition -> _adapt_metric
    -> _get_metric_global_filters -> _build_filter_clause

Positive forms: legacy ``condition``/``filter_type``, camelCase
``filterCondition``/``filterType`` (including the extra nested metadata keys
published in production), plain string conditions. Negative forms must fail
closed without producing SQL.
"""
import json
import unittest

from sql_translator_prod import RedisDSLLoader, SemanticCatalog, SQLTranslatorProd


class FilterLoader:
    """Duck-typed loader mirroring the hardening suite fixture."""

    def __init__(self, global_filters):
        self.entities = {
            "ent_order": {
                "entity_code": "ent_order",
                "entity_name": "订单",
                "physical_table_join": {"base_table": "order_info"},
                "attributes": [{"field_mapping": "order_info.order_id"},
                               {"field_mapping": "order_info.pay_amount"},
                               {"field_mapping": "order_info.status"}],
                "relations": [],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
        }
        self.metrics = {
            "sales": {
                "metric_code": "sales",
                "metric_name": "销售额",
                "metric_level": "原子指标",
                "calculation_rule": {
                    "calc_formula": "sales=SUM(order_info.pay_amount)",
                    "global_filters": global_filters,
                    "depend_metrics": [],
                },
                "source_dependency": {"bind_entity": ["ent_order"]},
                "params": {},
            },
        }

    def get_entity(self, code, model_id=None):
        return self.entities.get(code)

    def get_metric(self, code, model_id=None):
        return self.metrics.get(code)

    def get_dimension(self, code, model_id=None):
        return None

    def iter_dimensions(self, model_id):
        return []

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

    def _adapt_metric(self, data, model_id=None):
        return RedisDSLLoader._adapt_metric(self, data, model_id)


def translator(global_filters):
    value = SQLTranslatorProd.__new__(SQLTranslatorProd)
    value.loader = FilterLoader(global_filters)
    value.catalog = None
    value._base_entity_relationship_graphs = {}
    return value


def base_ast():
    return {
        "version": "2.0",
        "intent": "query",
        "subject": {"entity": "ent_order"},
        "metrics": [{"name": "sales", "alias": "销售额"}],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": 100,
        "having": [],
        "ambiguity": [],
    }


class FilterCatalog(SemanticCatalog):
    """Published catalog row whose global_filters column holds JSON dicts."""

    RAW_COLUMN = json.dumps([
        {"filterType": "IN", "filterCondition": "order_info.status = 1",
         "filterExplanation": "只统计有效订单", "globalFiltersJson": ""},
    ], ensure_ascii=False)

    def __init__(self):
        super().__init__({})

    def _query(self, sql, params=()):
        if "FROM semantic_model_indicator" in sql:
            return [{
                "id": 1, "project_id": 2, "semantic_model_id": 6,
                "business_domain_id": 7, "indicator_code": "sales",
                "indicator_name": "销售额", "indicator_level": 1,
                "unit": "元", "format_rule": None, "business_desc": None,
                "synonyms": "[]", "applicable_scenarios": "[]",
                "dependence_atomic_indicator": None,
                "calculation_formula": "sales=SUM(order_info.pay_amount)",
                "global_filters": self.RAW_COLUMN, "indicator_logic": None,
            }]
        if "FROM semantic_model_entity_bind_indicator" in sql:
            return [{
                "entity_code": "ent_order", "resolved_entity_code": "ent_order",
                "indicator_logic": None, "entity_name": "订单",
                "main_table_name": "order_info", "data_source_id": 5,
            }]
        raise AssertionError(sql)


class GlobalFilterNormalizationTests(unittest.TestCase):
    def test_published_dict_column_survives_definition_and_builds_legal_where(self):
        """The full loader/adapter chain must not stringify filter dicts."""
        definition = FilterCatalog().definition("6:sales", "current")
        adapted = RedisDSLLoader._adapt_metric(
            FilterLoader([]), {"global_filters": definition["global_filters"]})
        filters = adapted["calculation_rule"]["global_filters"]
        clause = translator([])._build_filter_clause([], filters, "order_info")
        self.assertEqual("WHERE order_info.status = 1", clause)
        self.assertNotIn("{", clause)
        self.assertNotIn("filterType", clause)

    def test_camel_condition_with_nested_metadata_keys_is_normalized(self):
        filters = [{
            "filterType": "IN",
            "filterCondition": "order_info.status = 1",
            "filterExplanation": "只统计有效订单",
            "globalFiltersJson": "",
        }]
        clause = translator([])._build_filter_clause([], filters, "order_info")
        self.assertEqual("WHERE order_info.status = 1", clause)

    def test_canonical_snake_case_condition_is_normalized(self):
        filters = [{"filter_type": "exclude", "condition": "order_info.status = 9"}]
        clause = translator([])._build_filter_clause([], filters, "order_info")
        self.assertEqual("WHERE NOT (order_info.status = 9)", clause)

    def test_plain_string_condition_is_still_supported(self):
        clause = translator([])._build_filter_clause(
            [], ["order_info.status = 1"], "order_info")
        self.assertEqual("WHERE order_info.status = 1", clause)

    def test_end_to_end_translate_emits_legal_where_not_dict_text(self):
        filters = [{"filterType": "IN", "filterCondition": "order_info.status = 1",
                    "filterExplanation": "只统计有效订单"}]
        sql = translator(filters).translate(json.dumps(base_ast()), "6")
        self.assertIn("WHERE order_info.status = 1", sql)
        self.assertNotIn("{'filterType'", sql)
        self.assertNotIn('"filterType"', sql)

    def test_translate_only_discloses_asl_and_metric_filter_sources(self):
        filters = [
            {"filterType": "IN", "filterCondition": "order_info.status = 1"},
            {"filterType": "IN", "filterCondition": "order_info.kind = 'A'"},
        ]
        ast = base_ast()
        ast["filters"] = [{
            "field": "order_info.channel", "operator": "=", "value": "online",
        }]

        result = translator(filters).translate_only(json.dumps(ast), "6")

        self.assertTrue(result["success"])
        self.assertEqual(ast["filters"], result["effective_filter_summary"]["asl_filters"])
        self.assertEqual(
            ["order_info.status = 1", "order_info.kind = 'A'"],
            [
                item["condition"]
                for item in result["effective_filter_summary"]["metric_global_filters"]
            ],
        )
        global_filter_check = next(
            item
            for item in result["semantic_validation_report"]["layers"]["business"]["checks"]
            if item["code"] == "GLOBAL_FILTER_RULES_APPLIED"
        )
        self.assertEqual(
            result["effective_filter_summary"], global_filter_check["details"]
        )

    def test_missing_condition_is_rejected_without_sql(self):
        with self.assertRaisesRegex(ValueError, "condition"):
            translator([])._build_filter_clause(
                [], [{"filterType": "IN"}], "order_info")

    def test_dict_condition_is_rejected_without_sql(self):
        with self.assertRaisesRegex(ValueError, "condition"):
            translator([])._build_filter_clause(
                [], [{"filterCondition": {"nested": "x"}}], "order_info")

    def test_list_condition_is_rejected_without_sql(self):
        with self.assertRaisesRegex(ValueError, "condition"):
            translator([])._build_filter_clause(
                [], [{"filterCondition": ["a", "b"]}], "order_info")

    def test_non_string_filter_type_is_rejected_without_sql(self):
        with self.assertRaisesRegex(ValueError, "filter_type"):
            translator([])._build_filter_clause(
                [], [{"filterCondition": "order_info.status = 1",
                      "filterType": {"IN": 1}}], "order_info")

    def test_adapt_metric_fails_closed_on_nested_condition(self):
        with self.assertRaisesRegex(ValueError, "condition"):
            RedisDSLLoader._adapt_metric(
                FilterLoader([]),
                {"global_filters": [{"filterCondition": {"nested": 1}}]})

    def test_raw_column_parser_preserves_structured_records(self):
        raw = json.dumps([{"condition": "t.x = 1"}, "t.y = 2"])
        self.assertEqual(
            [{"condition": "t.x = 1"}, "t.y = 2"],
            SemanticCatalog._raw_global_filters(raw))
        self.assertEqual([], SemanticCatalog._raw_global_filters("[]"))
        self.assertEqual([], SemanticCatalog._raw_global_filters(None))

    # --- REVISION 1: explicit filter_type allow-set ---

    def test_allowed_include_types_build_include_predicates(self):
        for filter_type in ("include", "IN", "EQ"):
            clause = translator([])._build_filter_clause(
                [], [{"filterType": filter_type,
                      "filterCondition": "order_info.status = 1"}],
                "order_info")
            self.assertEqual("WHERE order_info.status = 1", clause, filter_type)

    def test_allowed_exclude_type_builds_negated_predicate(self):
        clause = translator([])._build_filter_clause(
            [], [{"filter_type": "exclude", "condition": "order_info.status = 9"}],
            "order_info")
        self.assertEqual("WHERE NOT (order_info.status = 9)", clause)

    def test_misspelled_filter_type_is_rejected_without_sql(self):
        with self.assertRaisesRegex(ValueError, "filter_type"):
            translator([])._build_filter_clause(
                [], [{"filterType": "excldue",
                      "filterCondition": "order_info.status = 1"}],
                "order_info")

    def test_unknown_filter_type_is_rejected_without_sql(self):
        for value in ("between", "LIKE", "all", "INCLUDE", "Exclude"):
            with self.assertRaisesRegex(ValueError, "filter_type", msg=value):
                translator([])._build_filter_clause(
                    [], [{"filterType": value,
                          "filterCondition": "order_info.status = 1"}],
                    "order_info")

    def test_empty_or_blank_filter_type_is_rejected_without_sql(self):
        for value in ("", "   ", None):
            with self.assertRaisesRegex(ValueError, "filter_type", msg=repr(value)):
                translator([])._build_filter_clause(
                    [], [{"filterType": value,
                          "filterCondition": "order_info.status = 1"}],
                    "order_info")


if __name__ == "__main__":
    unittest.main()
