import unittest
from unittest.mock import patch

from sql_translator_prod import RedisDSLLoader, SQLTranslatorProd, SemanticCatalog


class TimeDimensionMappingTests(unittest.TestCase):
    def test_entity_attribute_accepts_legacy_attr_code(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._find_entity_code_by_id = lambda entity_id, model_id: None
        loader._find_entity_code_by_table = lambda table, model_id: None
        entity = loader._adapt_entity({
            "code": "dealer",
            "main_table_name": "dealer",
            "semantic_model_attribute_config": [{
                "attr_code": "dealer_name",
                "mapping_table": "dealer",
                "mapping_column": "dealer_name",
            }],
        }, "81")
        self.assertEqual("dealer_name", entity["attributes"][0]["attr_code"])
        self.assertEqual(
            "dealer.dealer_name", entity["attributes"][0]["field_mapping"]
        )

    def test_entity_attribute_preserves_main_attribute_metadata(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._find_entity_code_by_id = lambda entity_id, model_id: None
        loader._find_entity_code_by_table = lambda table, model_id: None
        entity = loader._adapt_entity({
            "code": "hospital",
            "main_table_name": "hospital",
            "semantic_model_attribute_config": [{
                "code": "hospital_name",
                "mapping_table": "hospital",
                "mapping_column": "hospital_name",
                "is_main_attribute": 1,
                "is_nullable": 1,
            }],
        }, "81")

        self.assertEqual(True, entity["attributes"][0]["is_main_attribute"])
        self.assertEqual(1, entity["attributes"][0]["is_nullable"])

    def test_entity_relation_preserves_cardinality(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._find_entity_code_by_id = (
            lambda entity_id, model_id: "dealer_product_relation"
        )
        loader._find_entity_code_by_table = lambda table, model_id: None
        entity = loader._adapt_entity({
            "code": "dealer",
            "main_table_name": "dealer",
            "semantic_model_relation_config": [{
                "code": "dealer_has_product_relation",
                "target_entity_type_id": "bridge-id",
                "source_table_column_name": "dealer-dealer_code",
                "target_table_column_name": "dealer_product_relation-dealer_code",
                "type": "1:N",
            }],
        }, "81")
        self.assertEqual("1:N", entity["relations"][0]["relation_type"])

    def test_flat_entity_attribute_payload_is_adapted(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._find_entity_code_by_id = lambda entity_id, model_id: "sales_transaction_domain_ent_order"
        loader.get_entity = lambda code, model_id: {"entity_name": "订单", "attributes": []}
        dimension = loader._adapt_dimension({
            "dim_code": "statistical_date",
            "dim_name": "统计日期",
            "dim_type": "时间维度",
            "entity_attribute": [{
                "attr": "2085638300381929475",
                "entity": "order-entity-id",
                "mappingTable": "order_info",
                "mappingColumn": "pay_time",
            }],
        }, "6")
        self.assertEqual("order_info.pay_time", dimension["field_mapping"]["fact_table_field"])
        self.assertEqual("2085638300381929475", dimension["attribute_mappings"][0]["attr_id"])

    def test_month_expression_is_shared_by_select_and_group(self):
        self.assertEqual(
            "DATE_FORMAT(order_info.pay_time, '%Y-%m')",
            SQLTranslatorProd._apply_time_granularity("order_info.pay_time", "month"),
        )

    def test_physical_dimension_uses_declared_business_attribute_alias(self):
        translator = SQLTranslatorProd.__new__(SQLTranslatorProd)

        class Loader:
            @staticmethod
            def find_all_entity_codes_by_table(table, model_id):
                assert (table, model_id) == ('hospital', '81')
                return ['hospital']

        translator.loader = Loader()
        translator._get_entity = lambda code, model_id: {
            'attributes': [{
                'field_mapping': 'hospital.hospital_name',
                'attr_name': '医院名称',
            }]
        }
        self.assertEqual(
            '医院名称',
            translator._dimension_alias(
                'hospital.hospital_name', None, None, '81'
            ),
        )

    def test_scoped_cache_clear(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._entity_cache = {('6', 'order'): {}, ('7', 'order'): {}}
        loader._metric_cache = {('6', 'sales'): {}}
        loader._dimension_cache = {('6', 'date'): {}}
        loader._table_to_entity = {'order_info': ['order']}
        loader._entity_id_to_code = {'id': 'order'}
        loader._index_built_model_id = '6'
        loader._index_built_global = True
        removed = loader.clear_cache('6')
        self.assertEqual({'entity': 1, 'metric': 1, 'dimension': 1}, removed)
        self.assertIn(('7', 'order'), loader._entity_cache)
        self.assertFalse(loader._table_to_entity)

    def test_adapted_dsl_cache_expires_without_restart(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._metric_cache = {('81', 'hospital_count'): {'formula': 'old'}}
        loader._cache_loaded_at = {('metric', '81', 'hospital_count'): 10.0}
        loader._cache_ttl_seconds = 5.0

        with patch('sql_translator_prod.time_module.monotonic', return_value=16.0):
            self.assertIsNone(
                loader._cached('metric', loader._metric_cache, ('81', 'hospital_count'))
            )
        self.assertNotIn(('81', 'hospital_count'), loader._metric_cache)

    def test_relationship_path_uses_declared_join_and_cardinality(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader.iter_entities = lambda model_id: [
            {
                'entity_code': 'sales_order',
                'relations': [{
                    'relation_code': 'sales_order_dealer',
                    'target_entity': 'dealer',
                    'join_key': 'sales_order.dealer_code = dealer.dealer_code',
                    'relation_type': 'N:1',
                }],
            },
            {'entity_code': 'dealer', 'relations': []},
        ]
        result = loader.resolve_relationship_paths('81', 'sales_order', ['dealer'])
        path = result['paths'][0]
        self.assertEqual('sales_order -> dealer', path['canonical_path'])
        self.assertEqual('MANY_TO_ONE', path['relationships'][0]['cardinality'])
        self.assertEqual('LOW', path['risk'])

    def test_relationship_path_rejects_unqualified_or_empty_entity_metadata(self):
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader.iter_entities = lambda model_id: [
            {
                'entity_code': 'hospital',
                'relations': [{
                    'relation_code': 'broken', 'target_entity': '',
                    'join_key': 'hospital_id = hospital_id', 'relation_type': '1:N',
                }],
            },
            {'entity_code': '', 'relations': []},
            {'entity_code': 'product', 'relations': []},
        ]
        result = loader.resolve_relationship_paths('81', 'hospital', ['product'])
        self.assertEqual([], result['paths'])
        self.assertEqual(['product'], result['unresolved_targets'])

    def test_failed_semantic_validation_has_stable_layer_and_code(self):
        report = SQLTranslatorProd._failed_semantic_validation_report(
            '一对多Join会导致指标重复累计: order_item'
        )
        self.assertEqual('FAIL', report['status'])
        self.assertEqual('FAIL', report['layers']['semantic']['status'])
        self.assertEqual('AGGREGATION_FANOUT_RISK', report['errors'][0]['code'])

    def test_successful_semantic_validation_has_three_layers(self):
        translator = SQLTranslatorProd.__new__(SQLTranslatorProd)
        report = translator._semantic_sql_validation_report(
            {
                'metrics': [], 'dimensions': [{'name': 'hospital.hospital_name'}],
                'time_context': None,
            },
            'SELECT hospital.hospital_name AS `医院名称` FROM hospital LIMIT 10000',
            '81',
        )
        self.assertEqual('PASS', report['status'])
        self.assertEqual({'syntax', 'semantic', 'business'}, set(report['layers']))

    def test_attribute_role_cache_is_scoped_and_refreshable(self):
        catalog = SemanticCatalog.__new__(SemanticCatalog)
        catalog._attribute_metadata_cache = {}
        calls = []

        def query(_sql, params):
            calls.append(params)
            return [{
                "mapping_table": "hospital",
                "mapping_column": "hospital_name",
                "is_main_attribute": 1,
            }]

        catalog._query = query
        fields = ["hospital.hospital_name"]

        self.assertEqual(
            1,
            catalog.attribute_metadata(81, fields)[fields[0]][0]["is_main_attribute"],
        )
        catalog.attribute_metadata(81, fields)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, catalog.clear_cache(81))
        catalog.attribute_metadata(81, fields)
        self.assertEqual(2, len(calls))

    def test_live_mysql_dimension_overlays_stale_redis_mapping(self):
        translator = SQLTranslatorProd.__new__(SQLTranslatorProd)

        class Loader:
            @staticmethod
            def get_dimension(code, model_id):
                return {
                    "dim_code": code,
                    "field_mapping": {"fact_table_field": "relation.relation_type"},
                    "attribute_mappings": [{
                        "attr_id": "old", "field_path": "relation.relation_type"
                    }],
                    "bind_entities": [],
                }

            @staticmethod
            def _adapt_dimension(data, model_id):
                return {
                    "dim_code": data["dim_code"],
                    "field_mapping": {
                        "fact_table_field": "department.dept_name"
                    },
                    "attribute_mappings": [{
                        "attr_id": "department-name-id",
                        "field_path": "department.dept_name",
                    }],
                    "bind_entities": [{"entity_code": "department"}],
                }

        class Catalog:
            @staticmethod
            def dimension_metadata(model_id, code):
                self.assertEqual(("81", "applicable_department"), (model_id, code))
                return {"dim_code": code, "entity_attribute": []}

        translator.loader = Loader()
        translator.catalog = Catalog()
        current = translator._get_dimension("applicable_department", "81")
        self.assertEqual(
            "department.dept_name",
            current["field_mapping"]["fact_table_field"],
        )

    def test_dimension_metadata_cache_is_scoped_and_refreshable(self):
        catalog = SemanticCatalog.__new__(SemanticCatalog)
        catalog._attribute_metadata_cache = {}
        catalog._entity_graph_cache = {}
        catalog._dimension_metadata_cache = {}
        calls = []

        def query(_sql, params):
            calls.append(params)
            return [{
                "dim_code": "applicable_department",
                "entity_attribute": '[{"attr": "department-name-id"}]',
            }]

        catalog._query = query
        loaded = catalog.dimension_metadata(81, "applicable_department")
        self.assertEqual("applicable_department", loaded["dim_code"])
        self.assertEqual("department-name-id", loaded["entity_attribute"][0]["attr"])
        catalog.dimension_metadata(81, "applicable_department")
        self.assertEqual(1, len(calls))
        self.assertEqual(1, catalog.clear_cache(81))
        catalog.dimension_metadata(81, "applicable_department")
        self.assertEqual(2, len(calls))


if __name__ == "__main__":
    unittest.main()
