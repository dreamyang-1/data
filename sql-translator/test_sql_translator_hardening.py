import json
import unittest

import redis

from sql_translator_prod import MAX_QUERY_ROWS, RedisDSLLoader, SQLTranslatorProd


class FakeRedisEntityCatalog:
    def __init__(self, payloads):
        self.payloads = payloads

    def scan_iter(self, match=None, count=None):
        return list(self.payloads)

    def get(self, key):
        value = self.payloads.get(key)
        return json.dumps(value, ensure_ascii=False) if value is not None else None


class FakeLoader:
    def __init__(self):
        self.entities = {
            "ent_order": {
                "entity_code": "ent_order",
                "entity_name": "订单",
                "physical_table_join": {"base_table": "order_info"},
                "attributes": [
                    {"field_mapping": "order_info.order_id"},
                    {"field_mapping": "order_info.pay_amount"},
                    {"field_mapping": "order_info.pay_time"},
                    {"field_mapping": "order_info.channel"},
                    {"field_mapping": "order_info.status"},
                ],
                "relations": [],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "ent_item": {
                "entity_code": "ent_item",
                "entity_name": "订单明细",
                "physical_table_join": {"base_table": "order_item_detail"},
                "attributes": [
                    {"field_mapping": "order_item_detail.order_id"},
                    {"field_mapping": "order_item_detail.goods_name"},
                ],
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
                    "global_filters": [],
                    "depend_metrics": [],
                },
                "source_dependency": {"bind_entity": ["ent_order"]},
                "params": {},
            },
            "order_count": {
                "metric_code": "order_count",
                "metric_name": "订单数",
                "metric_level": "原子指标",
                "calculation_rule": {
                    "calc_formula": "order_count=COUNT(DISTINCT order_info.order_id)",
                    "global_filters": [],
                    "depend_metrics": [],
                },
                "source_dependency": {"bind_entity": ["ent_order"]},
                "params": {},
            },
            "row_count": {
                "metric_code": "row_count",
                "metric_name": "行数",
                "metric_level": "原子指标",
                "calculation_rule": {
                    "calc_formula": "row_count=COUNT(*)",
                    "global_filters": [],
                    "depend_metrics": [],
                },
                "source_dependency": {"bind_entity": ["ent_order"]},
                "params": {},
            },
            "derived_sales": {
                "metric_code": "derived_sales",
                "metric_name": "衍生销售额",
                "metric_level": "衍生指标",
                "calculation_rule": {
                    "calc_formula": "derived_sales=sales / 2",
                    "global_filters": [],
                    "depend_metrics": ["sales"],
                },
                "source_dependency": {"bind_entity": ["ent_order"]},
                "params": {},
            },
        }
        self.dimensions = {
            "statistical_date": {
                "dim_code": "statistical_date",
                "dim_name": "统计日期",
                "dim_type": "时间维度",
                "enum_list": [],
                "field_mapping": {"fact_table_field": "shop_info.open_date", "dim_table_field": ""},
                "bind_entities": [
                    {"entity_code": "ent_shop"},
                    {"entity_code": "ent_order"},
                ],
                "bind_metrics": ["sales"],
                "attribute_mappings": [
                    {"attr_id": "shop-date", "entity_code": "ent_shop", "mapping_table": "shop_info", "field_path": "shop_info.open_date"},
                    {"attr_id": "pay-date", "entity_code": "ent_order", "mapping_table": "order_info", "field_path": "order_info.pay_time"},
                ],
            }
        }

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
    value.loader = FakeLoader()
    value.catalog = None
    return value


def translator_with_hospital():
    value = translator()
    value.loader.entities["ent_order"]["attributes"].append(
        {"field_mapping": "order_info.hospital_id"}
    )
    value.loader.entities["ent_order"]["relations"] = [{
        "relation_code": "order_belongs_to_hospital",
        "target_entity": "hospital",
        "join_key": "order_info.hospital_id = hospital.hospital_id",
        "relation_type": "N:1",
    }]
    value.loader.entities["hospital"] = {
        "entity_code": "hospital",
        "entity_name": "医院",
        "physical_table_join": {"base_table": "hospital"},
        "attributes": [
            {
                "field_mapping": "hospital.hospital_id",
                "is_primary_key": True,
                "is_main_attribute": False,
            },
            {
                "field_mapping": "hospital.hospital_name",
                "is_main_attribute": True,
                "is_nullable": True,
            },
            {
                "field_mapping": "hospital.hospital_level",
                "is_main_attribute": False,
                "is_nullable": True,
            },
        ],
        "relations": [],
        "sub_table_mappings": [],
        "data_source_id": 1,
    }
    return value


class FakeCatalog:
    def resolve_metrics(self, semantic_model_id, metric_names):
        mapping = {"销售额": "ent_order_total_pay_amount"}
        code = mapping.get(metric_names[0])
        if code is None:
            return {
                "status": "NOT_FOUND", "metrics": [],
                "unresolved": metric_names, "ambiguities": [],
            }
        return {
            "status": "RESOLVED",
            "metrics": [{"metric_id": f"{semantic_model_id}:{code}"}],
            "unresolved": [], "ambiguities": [],
        }


class FakeAttributeCatalog:
    def attribute_metadata(self, _model_id, fields):
        return {
            field: [{"is_main_attribute": field == "hospital.hospital_name"}]
            for field in fields
        }


class FakeTemporalFieldCatalog:
    def registered_temporal_fields(self, _model_id):
        return ["order_info.collection_time"]


class FakeBridgeInferenceCatalog:
    def entity_relationship_metadata(self, _model_id):
        return {}

    def infer_unique_bridge_endpoint(self, _model_id, bridge_table, entity_code):
        if bridge_table == "hospital_dept_relation" and entity_code == "department":
            return {
                "sub_table_name": bridge_table,
                "main_join_column": "dept_code",
                "sub_join_column": "dept_code",
            }
        return None


def base_ast(**updates):
    value = {
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
    value.update(updates)
    return value


class TranslatorHardeningTests(unittest.TestCase):
    def test_registered_temporal_field_can_be_projected_in_detail_rows(self):
        value = translator()
        value.catalog = FakeTemporalFieldCatalog()
        ast = base_ast(
            metrics=[],
            dimensions=[{
                "name": "order_info.collection_time",
                "alias": "采集时间",
            }],
            time_context={
                "type": "custom",
                "start": "2026-07-02",
                "end": "2026-09-02",
                "unit": "day",
                "anchor": "order_info.collection_time",
            },
            sort={
                "field": "order_info.collection_time",
                "field_type": "field",
                "direction": "ASC",
            },
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "85")

        self.assertIn("order_info.collection_time AS `采集时间`", sql)
        self.assertIn("order_info.collection_time >= '2026-07-02'", sql)
        self.assertIn("ORDER BY order_info.collection_time ASC", sql)

    def test_entity_uuid_index_is_inferred_from_incident_relation_payloads(self):
        relation = {
            "code": "hospital_include_department",
            "source_entity_type_id": "hospital-uuid",
            "target_entity_type_id": "department-uuid",
            "source_table_column_name": "hospital-hospital_id",
            "target_table_column_name": "hospital_dept_relation-hospital_id",
            "type": "1:N",
        }
        payloads = {
            "semantic_model:81:entity:hospital": {
                "code": "hospital",
                "name": "医院",
                "main_table_name": "hospital",
                "semantic_model_relation_config": [relation],
            },
            "semantic_model:81:entity:department": {
                "code": "department",
                "name": "科室",
                "main_table_name": "department",
                "semantic_model_relation_config": [relation],
                "semantic_model_entity_sub_table_mapping": [{
                    "sub_table_name": "hospital_dept_relation",
                    "main_join_column": "dept_code",
                    "sub_join_column": "dept_code",
                }],
            },
        }
        loader = RedisDSLLoader.__new__(RedisDSLLoader)
        loader._redis = FakeRedisEntityCatalog(payloads)
        loader._table_to_entity = {}
        loader._entity_id_to_code = {}
        loader._index_built_model_id = None
        loader._index_built_global = False

        loader._build_table_index("81")

        self.assertEqual("hospital", loader._find_entity_code_by_id("hospital-uuid", "81"))
        self.assertEqual(
            "department",
            loader._find_entity_code_by_id("department-uuid", "81"),
        )
        adapted = loader._adapt_entity(payloads["semantic_model:81:entity:hospital"], "81")
        self.assertEqual("department", adapted["relations"][0]["target_entity"])

    def test_reverse_bridge_relation_uses_unique_registered_endpoint(self):
        value = translator()
        value.catalog = FakeBridgeInferenceCatalog()
        value.loader.entities = {
            "hospital": {
                "entity_code": "hospital",
                "physical_table_join": {"base_table": "hospital"},
                "attributes": [],
                "relations": [{
                    "relation_code": "hospital_include_department",
                    "target_entity": "department",
                    "join_key": (
                        "hospital.hospital_id = "
                        "hospital_dept_relation.hospital_id"
                    ),
                    "relation_type": "1:N",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "department": {
                "entity_code": "department",
                "physical_table_join": {"base_table": "department"},
                "attributes": [],
                "relations": [],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
        }

        joins = value._find_join_path("department", "hospital", "81")

        self.assertEqual(
            "LEFT JOIN hospital_dept_relation ON "
            "hospital_dept_relation.dept_code = department.dept_code "
            "LEFT JOIN hospital ON hospital.hospital_id = "
            "hospital_dept_relation.hospital_id",
            joins,
        )

    def test_grouped_physical_time_dimension_orders_by_selected_business_alias(self):
        value = translator()
        for attribute in value.loader.entities["ent_order"]["attributes"]:
            if attribute["field_mapping"] == "order_info.pay_time":
                attribute["attr_name"] = "交易日期"
        ast = base_ast(
            dimensions=[{
                "name": "order_info.pay_time",
                "granularity": "month",
            }],
            sort={
                "field": "order_info.pay_time",
                "field_type": "dimension",
                "direction": "ASC",
            },
        )
        sql = value.translate(json.dumps(ast), "6")
        self.assertIn("AS `交易日期`", sql)
        self.assertIn("ORDER BY `交易日期` ASC", sql)
        self.assertNotIn("ORDER BY `order_info.pay_time`", sql)

    def test_translate_returns_authoritative_data_source_id(self):
        result = translator().translate_only(json.dumps(base_ast()), "6")
        self.assertTrue(result["success"])
        self.assertEqual("1", result["data_source_id"])

    def test_translate_rejects_conflicting_explicit_data_source(self):
        ast = base_ast(data_source_id=2)
        result = translator().translate_only(json.dumps(ast), "6")
        self.assertFalse(result["success"])
        self.assertEqual("DATA_SOURCE_SCOPE_MISMATCH", result["error_code"])

    def test_grouped_entity_main_attribute_excludes_unmatched_null_bucket(self):
        value = translator_with_hospital()
        ast = base_ast(
            metrics=[{"name": "sales"}],
            dimensions=[
                {"name": "hospital.hospital_name"},
                {"name": "hospital.hospital_level"},
            ],
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertIn("LEFT JOIN hospital ON order_info.hospital_id = hospital.hospital_id", sql)
        self.assertIn("WHERE hospital.hospital_name IS NOT NULL", sql)
        self.assertIn(
            "GROUP BY hospital.hospital_name, hospital.hospital_level",
            sql,
        )
        self.assertNotIn("hospital.hospital_level IS NOT NULL", sql)

    def test_authoritative_attribute_role_supports_legacy_redis_payload(self):
        value = translator_with_hospital()
        for attribute in value.loader.entities["hospital"]["attributes"]:
            attribute.pop("is_main_attribute", None)
        value.catalog = FakeAttributeCatalog()
        ast = base_ast(
            metrics=[{"name": "sales"}],
            dimensions=[
                {"name": "hospital.hospital_name"},
                {"name": "hospital.hospital_level"},
            ],
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertIn("hospital.hospital_name IS NOT NULL", sql)
        self.assertNotIn("hospital.hospital_level IS NOT NULL", sql)

    def test_non_main_nullable_group_dimension_is_not_filtered(self):
        value = translator_with_hospital()
        ast = base_ast(dimensions=[{"name": "hospital.hospital_level"}])

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertNotIn("IS NOT NULL", sql)
        self.assertIn("GROUP BY hospital.hospital_level", sql)

    def test_explicit_unmatched_group_request_suppresses_implicit_identity_filter(self):
        value = translator_with_hospital()
        ast = base_ast(
            dimensions=[{"name": "hospital.hospital_name"}],
            filters=[{
                "field": "hospital.hospital_id",
                "operator": "IS NULL",
                "value": None,
            }],
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertIn("WHERE hospital.hospital_id IS NULL", sql)
        self.assertNotIn("hospital.hospital_name IS NOT NULL", sql)

    def test_dimension_can_explicitly_include_null_identity_group(self):
        value = translator_with_hospital()
        ast = base_ast(dimensions=[{
            "name": "hospital.hospital_name",
            "include_null_group": True,
        }])

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertNotIn("hospital.hospital_name IS NOT NULL", sql)

    def test_detail_projection_does_not_apply_group_identity_filter(self):
        value = translator_with_hospital()
        ast = base_ast(
            metrics=[],
            dimensions=[{"name": "hospital.hospital_name"}],
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "81")

        self.assertNotIn("hospital.hospital_name IS NOT NULL", sql)

    def test_legacy_metric_code_is_exactly_resolved_by_authoritative_alias(self):
        value = translator()
        value.loader.metrics["ent_order_total_pay_amount"] = dict(
            value.loader.metrics["sales"],
            metric_code="ent_order_total_pay_amount",
        )
        value.catalog = FakeCatalog()
        ast = base_ast(metrics=[{"name": "total_pay_amount", "alias": "销售额"}])

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "6")

        self.assertIn("SUM(order_info.pay_amount) AS `销售额`", sql)

    def test_logical_date_dimension_is_bound_to_explicit_time_anchor(self):
        ast = base_ast(
            dimensions=[{"name": "dim_date", "granularity": "month"}],
            time_context={
                "type": "custom",
                "start": "2026-06-01",
                "end": "2026-07-31",
                "anchor": "order_info.pay_time",
            },
        )

        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")

        expression = "DATE_FORMAT(order_info.pay_time, '%Y-%m')"
        self.assertIn(f"SELECT {expression} AS `统计日期`", sql)
        self.assertIn(f"GROUP BY {expression}", sql)
        self.assertNotIn("order_info.dim_date", sql)

    def test_registered_time_dimension_cannot_override_explicit_anchor(self):
        ast = base_ast(
            dimensions=[{"name": "statistical_date", "granularity": "month"}],
            time_context={
                "type": "custom",
                "start": "2026-01-01",
                "end": "2026-07-31",
                "anchor": "order_info.pay_time",
            },
            sort={
                "field": "statistical_date",
                "field_type": "dimension",
                "direction": "ASC",
            },
        )

        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")

        expression = "DATE_FORMAT(order_info.pay_time, '%Y-%m')"
        self.assertIn(f"SELECT {expression} AS `统计日期`", sql)
        self.assertIn(f"GROUP BY {expression}", sql)
        self.assertIn("ORDER BY 统计日期 ASC", sql)
        self.assertNotIn("shop_info.open_date", sql)

    def test_generic_date_dimension_uses_main_entity_mapping_everywhere(self):
        ast = base_ast(
            dimensions=[{"name": "dim_date", "granularity": "month"}],
            sort={"field": "dim_date", "field_type": "dimension", "direction": "ASC"},
        )
        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")
        expression = "DATE_FORMAT(order_info.pay_time, '%Y-%m')"
        self.assertIn(f"SELECT {expression}", sql)
        self.assertIn(f"GROUP BY {expression}", sql)
        self.assertIn("ORDER BY `统计日期` ASC", sql)
        self.assertNotIn("shop_info.open_date", sql)
        self.assertNotIn("order_info.dim_date", sql)

    def test_iso_week_granularity_is_stable_across_year_boundary(self):
        ast = base_ast(dimensions=[{
            "name": "dim_date", "granularity": "week",
        }])
        sql = translator().translate(json.dumps(ast), "6")
        expression = "DATE_FORMAT(order_info.pay_time, '%x-W%v')"
        self.assertIn(expression, sql)
        self.assertIn(f"GROUP BY {expression}", sql)

    def test_time_filter_uses_index_friendly_half_open_range(self):
        ast = base_ast(time_context={
            "type": "year", "value": 2026,
            "unit": "year", "anchor": "order_info.pay_time",
        })
        sql = translator().translate(json.dumps(ast), "6")
        self.assertIn("order_info.pay_time >= '2026-01-01'", sql)
        self.assertIn("order_info.pay_time < '2027-01-01'", sql)
        self.assertNotIn("YEAR(order_info.pay_time)", sql)

    def test_unknown_time_type_and_granularity_fail_closed(self):
        ast = base_ast(time_context={
            "type": "recent", "unit": "day", "anchor": "order_info.pay_time",
        })
        with self.assertRaisesRegex(ValueError, "不支持的时间范围类型"):
            translator().translate(json.dumps(ast), "6")
        ast = base_ast(dimensions=[{
            "name": "dim_date", "granularity": "fortnight",
        }])
        with self.assertRaisesRegex(ValueError, "不支持的时间粒度"):
            translator().translate(json.dumps(ast), "6")

    def test_in_filter_is_rendered_and_escaped(self):
        ast = base_ast(filters=[{
            "field": "order_info.channel",
            "operator": "IN",
            "value": ["online", "Bob's shop"],
        }])
        sql = translator().translate(json.dumps(ast), "6")
        self.assertIn("order_info.channel IN ('online', 'Bob''s shop')", sql)

    def test_global_filter_or_expression_is_parenthesized_before_user_filter(self):
        clause = translator()._build_filter_clause(
            [{"field": "order_info.channel", "operator": "=", "value": "online"}],
            [{"condition": "order_info.status = 1 OR order_info.status = 2"}],
            "order_info",
        )
        self.assertEqual(
            "WHERE (order_info.status = 1 OR order_info.status = 2) "
            "AND order_info.channel = 'online'",
            clause,
        )

    def test_invalid_filter_operator_is_rejected(self):
        ast = base_ast(filters=[{
            "field": "order_info.status", "operator": "= 1; DROP TABLE x --", "value": 1,
        }])
        with self.assertRaisesRegex(ValueError, "不支持的过滤操作符"):
            translator().translate(json.dumps(ast), "6")

    def test_unknown_metric_fails_closed_instead_of_selecting_null(self):
        ast = base_ast(metrics=[{"name": "invented", "alias": "编造指标"}])
        with self.assertRaisesRegex(ValueError, "不存在指标"):
            translator().translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_duplicate_output_aliases_are_rejected(self):
        ast = base_ast(metrics=[
            {"name": "sales", "alias": "值"},
            {"name": "order_count", "alias": "值"},
        ])
        with self.assertRaisesRegex(ValueError, "列别名重复"):
            translator().translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_unknown_cross_table_dimension_never_guesses_join(self):
        ast = base_ast(dimensions=[{"name": "goods_info.goods_name"}])
        with self.assertRaises(ValueError):
            translator().translate(json.dumps(ast), "6")

    def test_limit_is_bounded(self):
        ast = base_ast(limit=MAX_QUERY_ROWS + 1)
        with self.assertRaisesRegex(ValueError, "LIMIT必须在"):
            translator().translate(json.dumps(ast), "6")

    def test_invalid_sort_does_not_silently_change_semantics(self):
        with self.assertRaisesRegex(ValueError, "排序方向"):
            translator().translate(json.dumps(base_ast(sort={
                "field": "sales", "field_type": "metric", "direction": "sideways",
            })), "6")
        with self.assertRaisesRegex(ValueError, "包含field"):
            translator().translate(json.dumps(base_ast(sort={"direction": "DESC"})), "6")

    def test_model_scope_and_container_types_are_validated(self):
        with self.assertRaisesRegex(ValueError, "正整数"):
            translator().translate(json.dumps(base_ast()), "6:*")
        with self.assertRaisesRegex(ValueError, "subject必须是对象"):
            translator().translate(json.dumps(base_ast(subject=[])), "6")
        with self.assertRaisesRegex(ValueError, "time_context必须是对象"):
            translator().translate(json.dumps(base_ast(time_context=[])), "6")

    def test_dimension_mapping_is_a_registered_physical_filter_field(self):
        value = translator()
        value.loader.dimensions["city"] = {
            "dim_code": "city",
            "dim_name": "城市",
            "dim_type": "文本维度",
            "field_mapping": {
                "fact_table_field": "dim_city.city_name",
                "dim_table_field": "",
            },
            "bind_entities": [],
            "attribute_mappings": [{
                "mapping_table": "dim_city",
                "mapping_column": "city_name",
                "field_path": "dim_city.city_name",
            }],
        }
        ast = base_ast(filters=[{
            "field": "dim_city.city_name",
            "operator": "=",
            "value": "上海市",
        }])

        self.assertEqual("6", value._validate_ast_contract(ast, "6"))

        ast["filters"][0]["field"] = "dim_city.not_registered"
        with self.assertRaisesRegex(ValueError, "不存在过滤字段"):
            value._validate_ast_contract(ast, "6")

    def test_execute_query_has_stable_scope_and_dependency_errors(self):
        value = translator()
        missing_scope = value.execute_query(json.dumps(base_ast()), None)
        self.assertEqual("SEMANTIC_MODEL_REQUIRED", missing_scope["error_code"])
        self.assertFalse(missing_scope["retryable"])

        def unavailable(*_args, **_kwargs):
            raise redis.ConnectionError("secret redis endpoint")

        value.translate = unavailable
        unavailable_result = value.execute_query(json.dumps(base_ast()), "6")
        self.assertEqual("SEMANTIC_DSL_UNAVAILABLE", unavailable_result["error_code"])
        self.assertTrue(unavailable_result["retryable"])
        self.assertNotIn("secret", unavailable_result["error"])

    def test_read_only_guard_rejects_second_statement_and_outfile(self):
        with self.assertRaises(ValueError):
            SQLTranslatorProd.validate_read_only_sql("SELECT 1; DROP TABLE users")
        with self.assertRaises(ValueError):
            SQLTranslatorProd.validate_read_only_sql("SELECT secret INTO OUTFILE '/tmp/x'")
        with self.assertRaisesRegex(ValueError, "子查询"):
            SQLTranslatorProd.validate_read_only_sql(
                "SELECT (SELECT password FROM users LIMIT 1) FROM order_info LIMIT 1"
            )
        with self.assertRaises(ValueError):
            SQLTranslatorProd.validate_read_only_sql("SELECT GET_LOCK('x', 10)")
        with self.assertRaises(ValueError):
            SQLTranslatorProd.validate_read_only_sql("SELECT @@version")

    def test_read_only_guard_allows_governed_shared_hospital_denominator(self):
        sql = (
            "SELECT dealer.dealer_name, "
            "COUNT(DISTINCT hospital.hospital_code) / NULLIF(("
            "SELECT COUNT(DISTINCT denominator_hospital.hospital_id) "
            "FROM hospital AS denominator_hospital "
            "JOIN dim_city AS denominator_city ON "
            "denominator_hospital.city_id = denominator_city.city_id "
            "WHERE denominator_city.city_name = '上海市'"
            "), 0) AS 区域医院覆盖率 "
            "FROM sales_order JOIN dealer ON "
            "sales_order.dealer_code = dealer.dealer_code "
            "JOIN hospital ON sales_order.hospital_id = hospital.hospital_id"
        )

        self.assertEqual(sql, SQLTranslatorProd.validate_read_only_sql(sql))

    def test_read_only_guard_keeps_arbitrary_scalar_subqueries_blocked(self):
        invalid_subqueries = (
            (
                "SELECT (SELECT COUNT(DISTINCT denominator_hospital.hospital_id) "
                "FROM hospital AS denominator_hospital "
                "JOIN dealer AS denominator_city ON "
                "denominator_hospital.city_id = denominator_city.city_id "
                "WHERE denominator_city.city_name = '上海市')"
            ),
            (
                "SELECT (SELECT COUNT(DISTINCT denominator_hospital.hospital_name) "
                "FROM hospital AS denominator_hospital "
                "JOIN dim_city AS denominator_city ON "
                "denominator_hospital.city_id = denominator_city.city_id "
                "WHERE denominator_city.city_name = '上海市')"
            ),
            (
                "SELECT (SELECT COUNT(DISTINCT denominator_hospital.hospital_id) "
                "FROM hospital AS denominator_hospital "
                "JOIN dim_city AS denominator_city ON "
                "denominator_hospital.city_id = denominator_city.city_id "
                "WHERE denominator_city.city_name = '上海市' "
                "UNION SELECT 1)"
            ),
        )
        for sql in invalid_subqueries:
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "子查询"):
                SQLTranslatorProd.validate_read_only_sql(sql)

    def test_known_one_to_many_join_blocks_fact_sum_duplication(self):
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info",
            "target_table": "order_item_detail",
            "relation_type": "1:N",
        }]
        ast = base_ast(dimensions=[{"name": "order_item_detail.goods_name"}])
        with self.assertRaisesRegex(ValueError, "重复累计"):
            value.translate(json.dumps(ast), "6")

    def test_join_fallback_uses_only_a_field_declared_on_both_tables(self):
        value = translator()
        value.loader.entities["ent_item"] = {
            "entity_code": "ent_item",
            "entity_name": "订单商品",
            "physical_table_join": {"base_table": "order_item_detail"},
            "attributes": [
                {"field_mapping": "order_item_detail.goods_id"},
                {"field_mapping": "order_item_detail.order_id"},
            ],
            "relations": [],
            "sub_table_mappings": [],
            "data_source_id": 1,
        }
        value.loader.entities["ent_order"]["attributes"] = [
            {"field_mapping": "order_info.order_id"},
            {"field_mapping": "order_info.pay_amount"},
        ]

        join = value._find_join_to_table(
            "ent_item", "order_info", {"order_item_detail"}, "6"
        )

        self.assertEqual(
            "LEFT JOIN order_info ON "
            "order_item_detail.order_id = order_info.order_id",
            join,
        )

    def test_join_fallback_never_invents_a_column_missing_from_target(self):
        value = translator()
        value.loader.entities["ent_item"] = {
            "entity_code": "ent_item",
            "entity_name": "订单商品",
            "physical_table_join": {"base_table": "order_item_detail"},
            "attributes": [{"field_mapping": "order_item_detail.goods_id"}],
            "relations": [],
            "sub_table_mappings": [],
            "data_source_id": 1,
        }
        value.loader.entities["ent_order"]["attributes"] = [
            {"field_mapping": "order_info.order_id"},
        ]

        join = value._find_join_to_table(
            "ent_item", "order_info", {"order_item_detail"}, "6"
        )

        self.assertIsNone(join)

    def test_reverse_join_still_blocks_fact_sum_duplication(self):
        """The query base can be the detail table while the metric fact is order_info."""
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info",
            "target_table": "order_item_detail",
            "relation_type": "1:N",
        }]
        ast = base_ast(
            subject={"entity": "ent_item"},
            dimensions=[{"name": "order_item_detail.goods_name"}],
        )
        with self.assertRaisesRegex(ValueError, "重复累计"):
            value.translate(json.dumps(ast), "6")

    def test_multihop_one_to_many_join_is_not_missed(self):
        value = translator()
        value.loader.entities.update({
            "ent_shop": {
                "entity_code": "ent_shop", "entity_name": "店铺",
                "physical_table_join": {"base_table": "shop_info"},
                "attributes": [{"field_mapping": "shop_info.shop_id"}],
                "relations": [{
                    "target_entity": "ent_region",
                    "join_key": "shop_info.shop_id = region_info.shop_id",
                    "source_table": "shop_info", "target_table": "region_info",
                    "relation_type": "1:N",
                }],
                "sub_table_mappings": [], "data_source_id": 1,
            },
            "ent_region": {
                "entity_code": "ent_region", "entity_name": "区域明细",
                "physical_table_join": {"base_table": "region_info"},
                "attributes": [
                    {"field_mapping": "region_info.shop_id"},
                    {"field_mapping": "region_info.region_name"},
                ],
                "relations": [], "sub_table_mappings": [], "data_source_id": 1,
            },
        })
        value.loader.entities["ent_order"]["attributes"].append(
            {"field_mapping": "order_info.shop_id"}
        )
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_shop",
            "join_key": "order_info.shop_id = shop_info.shop_id",
            "source_table": "order_info", "target_table": "shop_info",
            "relation_type": "N:1",
        }]
        ast = base_ast(dimensions=[{"name": "region_info.region_name"}])
        with self.assertRaisesRegex(ValueError, "重复累计"):
            value.translate(json.dumps(ast), "6")

    def test_detail_projection_preserves_unicode_filter_across_bridge_join(self):
        value = translator()
        value.loader.entities = {
            "ent_product": {
                "entity_code": "ent_product",
                "entity_name": "product",
                "physical_table_join": {"base_table": "product"},
                "attributes": [
                    {"field_mapping": "product.product_code"},
                    {"field_mapping": "product.product_name"},
                ],
                "relations": [{
                    "target_entity": "ent_product_department",
                    "join_key": (
                        "product.product_code = "
                        "product_department_relation.product_code"
                    ),
                    "source_table": "product",
                    "target_table": "product_department_relation",
                    "relation_type": "1:N",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "ent_product_department": {
                "entity_code": "ent_product_department",
                "entity_name": "product department relation",
                "physical_table_join": {
                    "base_table": "product_department_relation"
                },
                "attributes": [
                    {
                        "field_mapping": (
                            "product_department_relation.product_code"
                        )
                    },
                    {
                        "field_mapping": (
                            "product_department_relation.department_code"
                        )
                    },
                ],
                "relations": [{
                    "target_entity": "ent_department",
                    "join_key": (
                        "product_department_relation.department_code = "
                        "department.department_code"
                    ),
                    "source_table": "product_department_relation",
                    "target_table": "department",
                    "relation_type": "N:1",
                }],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
            "ent_department": {
                "entity_code": "ent_department",
                "entity_name": "department",
                "physical_table_join": {"base_table": "department"},
                "attributes": [
                    {"field_mapping": "department.department_code"},
                    {"field_mapping": "department.department_name"},
                ],
                "relations": [],
                "sub_table_mappings": [],
                "data_source_id": 1,
            },
        }
        ast = base_ast(
            subject={"entity": "ent_product"},
            metrics=[],
            dimensions=[{"name": "department.department_name"}],
            filters=[{
                "field": "product.product_name",
                "operator": "LIKE",
                "value": "%超声血管导引穿刺套件%",
            }],
        )

        sql = value.translate(json.dumps(ast, ensure_ascii=False), "6")

        self.assertIn(
            "LEFT JOIN product_department_relation ON "
            "product.product_code = product_department_relation.product_code",
            sql,
        )
        self.assertIn(
            "LEFT JOIN department ON "
            "product_department_relation.department_code = "
            "department.department_code",
            sql,
        )
        self.assertIn(
            "WHERE product.product_name LIKE '%超声血管导引穿刺套件%'",
            sql,
        )

    def test_join_path_prefers_direct_fact_relation_over_earlier_bridge_route(self):
        value = translator()
        value.loader.entities = {
            "sales_order": {
                "entity_code": "sales_order",
                "physical_table_join": {"base_table": "sales_order"},
                "attributes": [],
                # The longer dealer route is intentionally listed first. A
                # depth-first search used to return it before seeing the direct
                # order-product relation below.
                "relations": [
                    {
                        "target_entity": "dealer",
                        "join_key": "sales_order.dealer_code = dealer.dealer_code",
                    },
                    {
                        "target_entity": "product",
                        "join_key": "sales_order.product_code = product.product_code",
                    },
                ],
                "sub_table_mappings": [],
            },
            "dealer": {
                "entity_code": "dealer",
                "physical_table_join": {"base_table": "dealer"},
                "attributes": [],
                "relations": [{
                    "target_entity": "dealer_product",
                    "join_key": (
                        "dealer.dealer_code = dealer_product.dealer_code"
                    ),
                }],
                "sub_table_mappings": [],
            },
            "dealer_product": {
                "entity_code": "dealer_product",
                "physical_table_join": {"base_table": "dealer_product"},
                "attributes": [],
                "relations": [{
                    "target_entity": "product",
                    "join_key": (
                        "dealer_product.product_code = product.product_code"
                    ),
                }],
                "sub_table_mappings": [],
            },
            "product": {
                "entity_code": "product",
                "physical_table_join": {"base_table": "product"},
                "attributes": [],
                "relations": [],
                "sub_table_mappings": [],
            },
        }

        joins = value._find_join_path("sales_order", "product", "81")

        self.assertEqual(
            "LEFT JOIN product ON sales_order.product_code = product.product_code",
            joins,
        )
        self.assertNotIn("dealer", joins)

    def test_region_filter_extends_the_grouped_city_hierarchy(self):
        value = translator()
        value.loader.entities = {
            "sales_order": {
                "entity_code": "sales_order",
                "physical_table_join": {"base_table": "sales_order"},
                "attributes": [],
                "relations": [
                    {
                        "target_entity": "dealer",
                        "join_key": (
                            "sales_order.dealer_code = dealer.dealer_code"
                        ),
                    },
                    {
                        "target_entity": "hospital",
                        "join_key": (
                            "sales_order.hospital_id = hospital.hospital_id"
                        ),
                    },
                ],
                "sub_table_mappings": [],
            },
            "dealer": {
                "entity_code": "dealer",
                "physical_table_join": {"base_table": "dealer"},
                "attributes": [],
                "relations": [{
                    "target_entity": "city",
                    "join_key": "dealer.city_id = dim_city.city_id",
                }],
                "sub_table_mappings": [],
            },
            "hospital": {
                "entity_code": "hospital",
                "physical_table_join": {"base_table": "hospital"},
                "attributes": [],
                "relations": [{
                    "target_entity": "province",
                    "join_key": (
                        "hospital.province_id = dim_province.province_id"
                    ),
                }],
                "sub_table_mappings": [],
            },
            "city": {
                "entity_code": "city",
                "physical_table_join": {"base_table": "dim_city"},
                "attributes": [],
                "relations": [],
                "sub_table_mappings": [],
            },
            "province": {
                "entity_code": "province",
                "physical_table_join": {"base_table": "dim_province"},
                "attributes": [],
                # The hierarchy is stored parent -> child in the semantic
                # model, while this query needs to navigate city -> province.
                "relations": [{
                    "target_entity": "city",
                    "join_key": (
                        "dim_province.province_id = dim_city.province_id"
                    ),
                }],
                "sub_table_mappings": [],
            },
        }

        joins = value._find_join_to_table(
            "sales_order",
            "dim_province",
            {"sales_order", "dealer", "dim_city"},
            "81",
            preferred_entities=["city"],
        )

        self.assertEqual(
            "LEFT JOIN dim_province ON "
            "dim_province.province_id = dim_city.province_id",
            joins,
        )
        self.assertNotIn("hospital", joins)

    def test_join_path_minimizes_generated_segments_not_entity_hops(self):
        value = translator()
        value.loader.entities = {
            "a": {
                "entity_code": "a",
                "physical_table_join": {"base_table": "a"},
                "attributes": [],
                # The three-segment route is intentionally listed first.
                "relations": [
                    {
                        "target_entity": "x",
                        "join_key": "a.a_id = a_x.a_id",
                    },
                    {
                        "target_entity": "b",
                        "join_key": "a.b_id = b.b_id",
                    },
                ],
                "sub_table_mappings": [],
            },
            "x": {
                "entity_code": "x",
                "physical_table_join": {"base_table": "x"},
                "attributes": [],
                "relations": [{
                    "target_entity": "t",
                    "join_key": "x.t_id = t.t_id",
                }],
                "sub_table_mappings": [{
                    "sub_table_name": "a_x",
                    "main_join_column": "x_id",
                    "sub_join_column": "x_id",
                }],
            },
            "b": {
                "entity_code": "b",
                "physical_table_join": {"base_table": "b"},
                "attributes": [],
                "relations": [{
                    "target_entity": "t",
                    "join_key": "b.t_id = t.t_id",
                }],
                "sub_table_mappings": [],
            },
            "t": {
                "entity_code": "t",
                "physical_table_join": {"base_table": "t"},
                "attributes": [],
                "relations": [],
                "sub_table_mappings": [],
            },
        }

        joins = value._find_join_path("a", "t", "81")

        self.assertEqual(
            "LEFT JOIN b ON a.b_id = b.b_id "
            "LEFT JOIN t ON b.t_id = t.t_id",
            joins,
        )
        self.assertNotIn("a_x", joins)
        self.assertNotIn("LEFT JOIN x", joins)

    def test_distinct_detail_projection_eliminates_join_duplicates_before_limit(self):
        ast = base_ast(
            metrics=[],
            dimensions=[{"name": "order_info.order_id"}],
            projection_mode="DISTINCT",
            limit=None,
        )

        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")

        self.assertTrue(sql.startswith("SELECT DISTINCT order_info.order_id"))
        self.assertTrue(sql.endswith(f"LIMIT {MAX_QUERY_ROWS}"))

    def test_row_detail_projection_keeps_physical_row_semantics(self):
        ast = base_ast(
            metrics=[],
            dimensions=[{"name": "order_info.pay_amount"}],
            projection_mode="ROWS",
        )

        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")

        self.assertTrue(sql.startswith("SELECT order_info.pay_amount"))
        self.assertNotIn("SELECT DISTINCT", sql)

    def test_distinct_projection_is_rejected_for_metric_query(self):
        ast = base_ast(projection_mode="DISTINCT")

        with self.assertRaisesRegex(ValueError, "仅支持无指标明细查询"):
            translator().translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_unknown_projection_mode_is_rejected(self):
        ast = base_ast(
            metrics=[],
            dimensions=[{"name": "order_info.order_id"}],
            projection_mode="UNSAFE",
        )

        with self.assertRaisesRegex(ValueError, "ROWS或DISTINCT"):
            translator().translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_missing_join_cardinality_blocks_non_distinct_aggregate(self):
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info", "target_table": "order_item_detail",
            "relation_type": "",
        }]
        ast = base_ast(dimensions=[{"name": "order_item_detail.goods_name"}])
        with self.assertRaisesRegex(ValueError, "基数未配置"):
            value.translate(json.dumps(ast), "6")

    def test_count_distinct_survives_one_to_many_join(self):
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info",
            "target_table": "order_item_detail",
            "relation_type": "1:N",
        }]
        ast = base_ast(
            metrics=[{"name": "order_count", "alias": "订单数"}],
            dimensions=[{"name": "order_item_detail.goods_name"}],
        )
        sql = value.translate(json.dumps(ast, ensure_ascii=False), "6")
        self.assertIn("COUNT(DISTINCT order_info.order_id)", sql)

    def test_count_star_is_protected_from_one_to_many_duplication(self):
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info", "target_table": "order_item_detail",
            "relation_type": "1:N",
        }]
        ast = base_ast(
            metrics=[{"name": "row_count", "alias": "行数"}],
            dimensions=[{"name": "order_item_detail.goods_name"}],
        )
        with self.assertRaisesRegex(ValueError, "重复累计"):
            value.translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_derived_metric_inherits_dependency_cardinality_guard(self):
        value = translator()
        value.loader.entities["ent_order"]["relations"] = [{
            "target_entity": "ent_item",
            "join_key": "order_info.order_id = order_item_detail.order_id",
            "source_table": "order_info", "target_table": "order_item_detail",
            "relation_type": "1:N",
        }]
        ast = base_ast(
            metrics=[{"name": "derived_sales", "alias": "衍生销售额"}],
            dimensions=[{"name": "order_item_detail.goods_name"}],
        )
        with self.assertRaisesRegex(ValueError, "重复累计"):
            value.translate(json.dumps(ast, ensure_ascii=False), "6")

    def test_declared_dependency_is_replaced_as_a_complete_token(self):
        ast = base_ast(metrics=[{
            "name": "derived_sales", "alias": "衍生销售额",
        }])
        sql = translator().translate(json.dumps(ast, ensure_ascii=False), "6")
        self.assertIn("SUM(order_info.pay_amount) / 2", sql)


if __name__ == "__main__":
    unittest.main()
