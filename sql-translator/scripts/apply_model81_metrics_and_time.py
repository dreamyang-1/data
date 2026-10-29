"""Repair model-81 time semantics and the metrics required by medical scenarios."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
import pymysql
import redis


ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = 81
DOMAIN_ID = 205
DATA_SOURCE_ID = 58
EXPECTED_SOURCE_DATABASE = "drug_sales"


def _config() -> dict[str, Any]:
    cfg = dict(dotenv_values(ROOT / ".env"))
    required = (
        "MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD",
        "MYSQL_DATABASE", "REDIS_HOST", "REDIS_PORT", "REDIS_DB",
    )
    missing = [key for key in required if cfg.get(key) in (None, "")]
    if missing:
        raise RuntimeError(f"根目录.env缺少配置: {', '.join(missing)}")
    return cfg


def _connection(cfg: dict[str, Any]) -> pymysql.Connection:
    return pymysql.connect(
        host=cfg["MYSQL_HOST"],
        port=int(cfg["MYSQL_PORT"]),
        user=cfg["MYSQL_USER"],
        password=cfg["MYSQL_PASSWORD"],
        database=cfg["MYSQL_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def _redis_client(cfg: dict[str, Any]) -> redis.Redis:
    return redis.Redis(
        host=cfg["REDIS_HOST"],
        port=int(cfg["REDIS_PORT"]),
        db=int(str(cfg["REDIS_DB"]).split()[0]),
        password=cfg.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )


def _source_connection(source: dict[str, Any]) -> pymysql.Connection:
    return pymysql.connect(
        host=source["host"],
        port=int(source["port"]),
        user=source["username"],
        password=source["password"],
        database=source["db_name"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def _require_one(cursor, sql: str, args: tuple, label: str) -> dict:
    cursor.execute(sql, args)
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one {label}, found {len(rows)}")
    return rows[0]


def _upsert_indicator(cursor, project_id: int, metric: dict) -> None:
    cursor.execute(
        "SELECT id FROM semantic_model_indicator "
        "WHERE semantic_model_id=%s AND indicator_code=%s ORDER BY id",
        (MODEL_ID, metric["code"]),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"duplicate indicator_code: {metric['code']}")
    values = (
        metric["name"], metric["unit"], metric["format"], metric["description"],
        metric["synonyms"], metric["scenarios"], metric["formula"],
        metric.get("filters", "[]"), metric.get("level", 1),
    )
    if rows:
        cursor.execute(
            "UPDATE semantic_model_indicator SET indicator_name=%s,unit=%s,"
            "format_rule=%s,business_desc=%s,synonyms=%s,applicable_scenarios=%s,"
            "calculation_formula=%s,global_filters=%s,indicator_level=%s,"
            "is_deleted=0,data_status=0 WHERE id=%s",
            (*values, rows[0]["id"]),
        )
    else:
        cursor.execute(
            "INSERT INTO semantic_model_indicator "
            "(project_id,semantic_model_id,indicator_code,business_domain_id,"
            "indicator_name,indicator_level,unit,format_rule,business_desc,synonyms,"
            "applicable_scenarios,dependence_atomic_indicator,calculation_formula,"
            "global_filters,is_deleted,data_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'',%s,%s,0,0)",
            (
                project_id, MODEL_ID, metric["code"], DOMAIN_ID, metric["name"],
                metric.get("level", 1), metric["unit"], metric["format"],
                metric["description"], metric["synonyms"], metric["scenarios"],
                metric["formula"], metric.get("filters", "[]"),
            ),
        )


def _upsert_binding(
    cursor,
    entity_code: str,
    entity_id: str,
    metric: dict,
    *,
    allowed_previous_entities: set[str] | None = None,
) -> None:
    cursor.execute(
        "SELECT id,entity_code FROM semantic_model_entity_bind_indicator "
        "WHERE semantic_model_id=%s AND indicator_code=%s AND is_deleted=0 "
        "ORDER BY id FOR UPDATE",
        (MODEL_ID, metric["code"]),
    )
    rows = cursor.fetchall()
    allowed_previous_entities = allowed_previous_entities or set()
    unexpected = [
        row for row in rows if row.get("entity_code") not in (entity_code, entity_id)
    ]
    forbidden = [
        row for row in unexpected
        if row.get("entity_code") not in allowed_previous_entities
    ]
    if forbidden:
        raise RuntimeError(
            f"unexpected metric binding for {metric['code']}: "
            f"{', '.join(str(row.get('entity_code')) for row in forbidden)}"
        )
    for row in unexpected:
        cursor.execute(
            "UPDATE semantic_model_entity_bind_indicator SET is_deleted=1 "
            "WHERE id=%s",
            (row["id"],),
        )
    rows = [row for row in rows if row not in unexpected]
    canonical = [row for row in rows if row.get("entity_code") == entity_code]
    legacy = [row for row in rows if row.get("entity_code") == entity_id]
    if len(canonical) > 1 or len(legacy) > 1:
        raise RuntimeError(
            f"duplicate metric binding: {entity_code}/{metric['code']}"
        )
    if canonical:
        cursor.execute(
            "UPDATE semantic_model_entity_bind_indicator SET indicator_name=%s,"
            "is_deleted=0 WHERE id=%s",
            (metric["name"], canonical[0]["id"]),
        )
        if legacy:
            cursor.execute(
                "UPDATE semantic_model_entity_bind_indicator SET is_deleted=1 "
                "WHERE id=%s",
                (legacy[0]["id"],),
            )
    elif legacy:
        cursor.execute(
            "UPDATE semantic_model_entity_bind_indicator SET entity_code=%s,"
            "indicator_name=%s,is_deleted=0 WHERE id=%s",
            (entity_code, metric["name"], legacy[0]["id"]),
        )
    else:
        cursor.execute(
            "SELECT id FROM semantic_model_entity_bind_indicator "
            "WHERE semantic_model_id=%s AND indicator_code=%s "
            "AND entity_code IN (%s,%s) AND is_deleted=1 ORDER BY id LIMIT 1",
            (MODEL_ID, metric["code"], entity_code, entity_id),
        )
        deleted = cursor.fetchone()
        if deleted:
            cursor.execute(
                "UPDATE semantic_model_entity_bind_indicator SET entity_code=%s,"
                "indicator_name=%s,is_deleted=0 WHERE id=%s",
                (entity_code, metric["name"], deleted["id"]),
            )
        else:
            cursor.execute(
                "INSERT INTO semantic_model_entity_bind_indicator "
                "(semantic_model_id,entity_code,indicator_code,indicator_name,is_deleted) "
                "VALUES (%s,%s,%s,%s,0)",
                (MODEL_ID, entity_code, metric["code"], metric["name"]),
            )


def _upsert_transaction_dimension(
    cursor, project_id: int, sales_order_entity_id: str, created_date_attr_id: str
) -> None:
    binding = json.dumps(
        [{
            "attr": created_date_attr_id,
            "entity": sales_order_entity_id,
            "attrName": "交易日期",
            "entityName": "销售订单",
            "mappingTable": "sales_order",
            "mappingColumn": "created_date",
            "businessDomain": str(DOMAIN_ID),
        }],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    metric_codes = [
        "annual_total_sales", "tax_free_sales_amount", "sales_total_cost",
        "sales_total_quantity", "order_count", "cooperating_dealer_count",
        "cooperating_hospital_count",
    ]
    cursor.execute(
        "SELECT id FROM semantic_model_dimension "
        "WHERE semantic_model_id=%s AND dim_code='transaction_date' ORDER BY id",
        (MODEL_ID,),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError("duplicate transaction_date dimensions")
    values = (
        "交易日期", json.dumps(["成交日期", "订单日期", "销售日期"], ensure_ascii=False),
        "销售订单真实发生时间", "时间维度", "[]", "[]", "[]",
        json.dumps(["day", "week", "month", "quarter", "year"]),
        binding, json.dumps(metric_codes, ensure_ascii=False),
    )
    if rows:
        cursor.execute(
            "UPDATE semantic_model_dimension SET dim_name=%s,synonyms=%s,"
            "dim_description=%s,dim_type=%s,special_rules=%s,enum_list=%s,"
            "level_list=%s,date_list=%s,entity_attribute=%s,indicator=%s,"
            "is_deleted=0,data_status=0 WHERE id=%s",
            (*values, rows[0]["id"]),
        )
    else:
        cursor.execute(
            "INSERT INTO semantic_model_dimension "
            "(project_id,semantic_model_id,dim_code,dim_name,synonyms,dim_description,"
            "dim_type,special_rules,enum_list,level_list,date_list,entity_attribute,"
            "indicator,is_deleted,data_status) "
            "VALUES (%s,%s,'transaction_date',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0)",
            (project_id, MODEL_ID, *values),
        )
    # Fiscal month remains available for explicit grouping, but it must not be
    # selected as an automatic date anchor for metrics.
    cursor.execute(
        "UPDATE semantic_model_dimension SET indicator='[]' "
        "WHERE semantic_model_id=%s AND dim_code='general_ledger_year_month'",
        (MODEL_ID,),
    )


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _preflight(cursor, metrics: list[dict]) -> dict[str, Any]:
    domain = _require_one(
        cursor,
        "SELECT id,project_id FROM semantic_model_business_domain "
        "WHERE id=%s AND semantic_model_id=%s AND is_deleted=0",
        (DOMAIN_ID, MODEL_ID),
        "model-81 business domain 205",
    )
    source = _require_one(
        cursor,
        "SELECT id,host,port,username,password,db_name FROM semantic_model_data_source "
        "WHERE id=%s AND semantic_model_id=%s AND is_deleted=0",
        (DATA_SOURCE_ID, MODEL_ID),
        "model-81 data source 58",
    )
    if source.get("db_name") != EXPECTED_SOURCE_DATABASE:
        raise RuntimeError(
            f"expected source database {EXPECTED_SOURCE_DATABASE}, "
            f"found {source.get('db_name')}"
        )

    entity_fields = {
        "sales_order": {
            "amount_with_tax", "amount_without_tax", "cost", "quantity",
            "order_key", "dealer_code", "hospital_id", "created_date",
        },
        "dealer_profile": {
            "total_sales_amount", "recent_year_amount", "cooperation_months",
            "cooperation_count", "avg_monthly_growth_rate_3m",
        },
        "hospital": {"hospital_id"},
    }
    entities: dict[str, dict] = {}
    for entity_code, fields in entity_fields.items():
        entity = _require_one(
            cursor,
            "SELECT id,code,main_table_name FROM semantic_model_entity_type "
            "WHERE semantic_model_id=%s AND business_domain_id=%s AND code=%s "
            "AND is_deleted=0 AND status=1",
            (MODEL_ID, DOMAIN_ID, entity_code),
            f"entity {entity_code} (run semantic foundation migration first)",
        )
        entities[entity_code] = entity
        placeholders = ",".join(["%s"] * len(fields))
        cursor.execute(
            "SELECT mapping_column FROM semantic_model_attribute_config "
            "WHERE semantic_model_id=%s AND entity_type_id=%s AND is_deleted='0' "
            f"AND mapping_column IN ({placeholders})",
            (MODEL_ID, entity["id"], *sorted(fields)),
        )
        present = {row["mapping_column"] for row in cursor.fetchall()}
        missing = sorted(fields - present)
        if missing:
            raise RuntimeError(
                f"entity {entity_code} missing metric fields: {', '.join(missing)}"
            )

    metric_codes = [metric["code"] for metric in metrics]
    if len(metric_codes) != len(set(metric_codes)):
        raise RuntimeError("planned metric codes are not unique")
    for metric in metrics:
        if len(metric["formula"]) > 500 or len(metric.get("filters", "[]")) > 500:
            raise RuntimeError(f"metric metadata exceeds database width: {metric['code']}")

    for dim_code in ("transaction_date", "general_ledger_year_month"):
        cursor.execute(
            "SELECT id FROM semantic_model_dimension "
            "WHERE semantic_model_id=%s AND dim_code=%s ORDER BY id",
            (MODEL_ID, dim_code),
        )
        rows = cursor.fetchall()
        if dim_code == "transaction_date" and len(rows) > 1:
            raise RuntimeError("duplicate transaction_date dimensions")
        if dim_code == "general_ledger_year_month" and len(rows) != 1:
            raise RuntimeError(
                f"expected one general_ledger_year_month dimension, found {len(rows)}"
            )
    return {
        "project_id": domain["project_id"],
        "entities": entities,
        "metric_codes": metric_codes,
        "source": source,
    }


def _source_preflight(source: dict[str, Any]) -> dict[str, Any]:
    if not all(
        source.get(key) not in (None, "")
        for key in ("host", "port", "username", "password", "db_name")
    ):
        raise RuntimeError("model-81 source connection is incomplete")
    with _source_connection(source) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,"
                "COLUMN_COMMENT FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sales_order' "
                "AND COLUMN_NAME='created_date'"
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    f"expected source field sales_order.created_date, found {len(rows)}"
                )
            column = rows[0]
            if column["DATA_TYPE"] not in {"date", "datetime", "timestamp"}:
                raise RuntimeError(
                    "sales_order.created_date is not a real date/time field"
                )
            cursor.execute(
                "SELECT MIN(created_date) AS data_from,MAX(created_date) AS data_as_of "
                "FROM sales_order WHERE created_date IS NOT NULL"
            )
            watermark = cursor.fetchone()
            if not watermark or watermark.get("data_as_of") is None:
                raise RuntimeError("sales_order.created_date contains no usable values")
    return {"column": column, "watermark": watermark}


def _build_cache_payloads(
    cursor, metric_codes: list[str]
) -> tuple[dict[str, str], dict[str, str]]:
    placeholders = ",".join(["%s"] * len(metric_codes))
    cursor.execute(
        "SELECT * FROM semantic_model_indicator WHERE semantic_model_id=%s "
        f"AND indicator_code IN ({placeholders}) AND is_deleted=0 ORDER BY indicator_code",
        (MODEL_ID, *metric_codes),
    )
    metric_rows = cursor.fetchall()
    if len(metric_rows) != len(metric_codes):
        found = {row["indicator_code"] for row in metric_rows}
        raise RuntimeError(
            f"metric cache payload missing: {', '.join(sorted(set(metric_codes) - found))}"
        )

    cursor.execute(
        "SELECT b.indicator_code,b.entity_code FROM semantic_model_entity_bind_indicator b "
        "JOIN semantic_model_entity_type e ON e.semantic_model_id=b.semantic_model_id "
        "AND e.code=b.entity_code AND e.is_deleted=0 AND e.status=1 "
        "WHERE b.semantic_model_id=%s AND b.is_deleted=0 "
        f"AND b.indicator_code IN ({placeholders}) ORDER BY b.indicator_code,b.id",
        (MODEL_ID, *metric_codes),
    )
    bindings: dict[str, list[str]] = {}
    for row in cursor.fetchall():
        bindings.setdefault(row["indicator_code"], []).append(row["entity_code"])

    cursor.execute(
        "SELECT * FROM semantic_model_dimension WHERE semantic_model_id=%s "
        "AND is_deleted=0 ORDER BY dim_code,id",
        (MODEL_ID,),
    )
    dimension_rows = cursor.fetchall()
    dimensions_by_metric: dict[str, list[dict[str, str]]] = {}
    time_caliber_by_metric: dict[str, dict[str, str]] = {}
    for row in dimension_rows:
        indicators = _json_value(row.get("indicator"), [])
        if isinstance(indicators, str):
            indicators = [item.strip() for item in indicators.split(",") if item.strip()]
        if not isinstance(indicators, list):
            continue
        for code in indicators:
            code = str(code)
            if code in metric_codes:
                dimensions_by_metric.setdefault(code, []).append({
                    "dim_code": row["dim_code"],
                    "dim_name": row["dim_name"],
                })
                if row["dim_code"] == "transaction_date":
                    anchors = {
                        f"{binding.get('mappingTable')}.{binding.get('mappingColumn')}"
                        for binding in (_json_value(row.get("entity_attribute"), []) or [])
                        if isinstance(binding, dict)
                        and binding.get("mappingTable")
                        and binding.get("mappingColumn")
                    }
                    if len(anchors) == 1:
                        time_caliber_by_metric[code] = {
                            "time_anchor": next(iter(anchors)),
                            "time_dimension": row["dim_code"],
                        }

    level_names = {1: "原子指标", 2: "衍生指标", 3: "复合口径指标"}
    metric_payloads: dict[str, str] = {}
    for row in metric_rows:
        code = row["indicator_code"]
        bound_entities = list(dict.fromkeys(bindings.get(code, [])))
        if len(bound_entities) != 1:
            raise RuntimeError(
                f"metric {code} must have exactly one canonical entity binding"
            )
        payload = {
            "code": code,
            "name": row["indicator_name"],
            "synonyms": row.get("synonyms") or "",
            "business_domain_id": str(row["business_domain_id"]),
            "metric_level": level_names.get(row.get("indicator_level"), row.get("indicator_level")),
            "unit": row.get("unit"),
            "format_rule": row.get("format_rule"),
            "description": row.get("business_desc") or "",
            "application_scenes": row.get("applicable_scenarios") or "",
            "depend_atom_metric": row.get("dependence_atomic_indicator"),
            "calc_formula": row.get("calculation_formula") or "",
            "global_filters": _json_value(row.get("global_filters"), []),
            "indicator_logic": row.get("indicator_logic"),
            "bind_dimensions": dimensions_by_metric.get(code, []),
            "time_caliber": time_caliber_by_metric.get(code, {}),
            "bind_entity": bound_entities,
            "project_id": str(row["project_id"]),
            "semantic_model_id": str(MODEL_ID),
            "is_deleted": 0,
            "create_by": str(row.get("create_by") or ""),
        }
        metric_payloads[code] = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        )

    dimension_payloads: dict[str, str] = {}
    for row in dimension_rows:
        if row["dim_code"] not in ("transaction_date", "general_ledger_year_month"):
            continue
        payload = {
            "id": str(row["id"]),
            "project_id": str(row["project_id"]),
            "semantic_model_id": str(MODEL_ID),
            "dim_code": row["dim_code"],
            "dim_name": row["dim_name"],
            "synonyms": _json_value(row.get("synonyms"), []),
            "dim_description": row.get("dim_description") or "",
            "dim_type": row.get("dim_type"),
            "special_rules": _json_value(row.get("special_rules"), []),
            "enum_list": _json_value(row.get("enum_list"), []),
            "level_list": _json_value(row.get("level_list"), []),
            "date_list": _json_value(row.get("date_list"), []),
            "entity_attribute": _json_value(row.get("entity_attribute"), []),
            "indicator": _json_value(row.get("indicator"), []),
            "create_by": str(row.get("create_by") or ""),
            "is_deleted": 0,
        }
        dimension_payloads[row["dim_code"]] = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        )
    if set(dimension_payloads) != {"transaction_date"}:
        raise RuntimeError("time-dimension cache payload is incomplete")
    return metric_payloads, dimension_payloads


def _refresh_redis(
    cfg: dict[str, Any], metric_payloads: dict[str, str],
    dimension_payloads: dict[str, str],
) -> None:
    client = _redis_client(cfg)
    client.ping()
    pipe = client.pipeline(transaction=True)
    for code, payload in metric_payloads.items():
        pipe.set(f"semantic_model:{MODEL_ID}:metric:{code}", payload)
    for code, payload in dimension_payloads.items():
        pipe.set(f"semantic_model:{MODEL_ID}:dimension:{code}", payload)
    pipe.delete(
        f"semantic_model:{MODEL_ID}:metric:dealer_rolling_period_performance_growth_rate"
    )
    pipe.delete(
        f"semantic_model:{MODEL_ID}:dimension:general_ledger_year_month"
    )
    pipe.execute()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="模型81核心指标与交易时间口径的幂等迁移",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="显式执行元数据事务和Redis刷新；省略时只做只读预检",
    )
    args = parser.parse_args()
    positive_sales = json.dumps(
        [{
            "filterType": "include",
            "filterCondition": "sales_order.quantity > 0",
            "filterExplanation": "排除退货和零数量记录",
        }],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    net_sales = json.dumps([], ensure_ascii=False, separators=(",", ":"))
    metrics = [
        {
            "code": "annual_total_sales", "name": "含税销售总额", "unit": "元",
            "format": "#,##0.00", "formula": "annual_total_sales=SUM(sales_order.amount_with_tax)",
            "description": "指定时间范围内全部销售记录的含税金额净额合计；退货或冲销按源数据负值抵减",
            "synonyms": "销售总额,销售额,年度总销售额,含税销售金额,订单销售额",
            "scenarios": "销售趋势,经销商排名,商品销售分析,医院销售分析", "filters": net_sales,
            "entity": "sales_order",
        },
        {
            "code": "tax_free_sales_amount", "name": "不含税销售总额", "unit": "元",
            "format": "#,##0.00", "formula": "tax_free_sales_amount=SUM(sales_order.amount_without_tax)",
            "description": "指定时间范围内全部销售记录的不含税金额净额合计；退货或冲销按源数据负值抵减",
            "synonyms": "无税销售总金额,不含税销售额", "scenarios": "财务分析,毛利分析",
            "filters": net_sales, "entity": "sales_order",
        },
        {
            "code": "sales_total_cost", "name": "销售总成本", "unit": "元",
            "format": "#,##0.00", "formula": "sales_total_cost=SUM(sales_order.cost)",
            "description": "指定时间范围内全部销售记录的成本净额合计；退货或冲销按源数据负值抵减",
            "synonyms": "总成本,发货成本", "scenarios": "成本分析,利润分析",
            "filters": net_sales, "entity": "sales_order",
        },
        {
            "code": "sales_total_quantity", "name": "销售总数量", "unit": "件",
            "format": "#,##0.####", "formula": "sales_total_quantity=SUM(sales_order.quantity)",
            "description": "指定时间范围内全部销售记录的商品数量净额合计；退货或冲销按源数据负值抵减",
            "synonyms": "销量,销售数量", "scenarios": "销量趋势,商品动销分析",
            "filters": net_sales, "entity": "sales_order",
        },
        {
            "code": "order_count", "name": "订单笔数", "unit": "笔",
            "format": "#,##0", "formula": "order_count=COUNT(DISTINCT sales_order.order_key)",
            "description": "指定时间范围内全部销售记录按业务订单键去重后的订单数量",
            "synonyms": "订单数,单据数量", "scenarios": "订单分析,订单活跃度",
            "filters": net_sales, "entity": "sales_order",
        },
        {
            "code": "cooperating_dealer_count", "name": "已合作经销商数", "unit": "个",
            "format": "#,##0", "formula": "cooperating_dealer_count=COUNT(DISTINCT dealer.dealer_name)",
            "description": "产生有效销售订单且可在经销商主数据中展示名称的去重经销商数量；与经销商名单使用同一口径",
            "synonyms": "合作经销商数量,经销商覆盖", "scenarios": "渠道覆盖,销售报告",
            "filters": positive_sales, "entity": "sales_order",
        },
        {
            "code": "cooperating_hospital_count", "name": "已合作医院数", "unit": "家",
            "format": "#,##0", "formula": "cooperating_hospital_count=COUNT(DISTINCT hospital.hospital_code)",
            "description": "产生有效销售订单且可在医院主数据中展示名称的去重医院数量；与合作医院名单使用同一口径",
            "synonyms": "成交医院数量,医院覆盖数,医院覆盖", "scenarios": "医院覆盖分析,销售报告",
            "filters": positive_sales, "entity": "sales_order",
        },
        {
            "code": "target_city_hospital_count", "name": "目标城市医院数量", "unit": "家",
            "format": "#,##0", "formula": "target_city_hospital_count=COUNT(DISTINCT hospital.hospital_id)",
            "description": "按医院主数据统计目标城市内去重后的医院数量",
            "synonyms": "目标地区医院数,城市医院数量,地区医院数量", "scenarios": "医院渠道筛选,医院覆盖规划",
            "entity": "hospital",
        },
        {
            "code": "dealer_cumulative_sales_amount", "name": "经销商累计销售额", "unit": "元",
            "format": "#,##0.00", "formula": "dealer_cumulative_sales_amount=MAX(dealer_result.total_sales_amount)",
            "description": "单个经销商画像快照中的历史累计销售金额；按经销商比较使用",
            "synonyms": "累计销售额,经销商累计营收,整体业务规模,业务规模", "scenarios": "经销商排名,经销商画像",
            "entity": "dealer_profile",
        },
        {
            "code": "dealer_recent_year_sales", "name": "经销商近一年销售额", "unit": "元",
            "format": "#,##0.00", "formula": "dealer_recent_year_sales=MAX(dealer_result.recent_year_amount)",
            "description": "单个经销商画像快照中的近一年销售金额；按经销商比较使用",
            "synonyms": "近一年业务规模,近一年销售总额", "scenarios": "经销商推荐,活跃经销商排名",
            "entity": "dealer_profile",
        },
        {
            "code": "dealer_cooperation_months", "name": "合作时长", "unit": "月",
            "format": "#,##0", "formula": "dealer_cooperation_months=MAX(dealer_result.cooperation_months)",
            "description": "经销商画像中的合作月数",
            "synonyms": "合作月数,合作时间", "scenarios": "经销商画像,经销商比较",
            "entity": "dealer_profile",
        },
        {
            "code": "dealer_cooperation_count", "name": "合作次数", "unit": "次",
            "format": "#,##0", "formula": "dealer_cooperation_count=MAX(dealer_result.cooperation_count)",
            "description": "经销商画像中的历史合作次数",
            "synonyms": "历史合作次数", "scenarios": "经销商画像,经销商比较",
            "entity": "dealer_profile",
        },
        {
            "code": "dealer_growth_rate_3m", "name": "近三月业绩增长率", "unit": "%",
            "format": "0.00%", "formula": "dealer_growth_rate_3m=MAX(dealer_result.avg_monthly_growth_rate_3m)",
            "description": "经销商画像预计算的近三个月平均月度增长率",
            "synonyms": "3个月业绩增长率,近3月增长率,业绩增长率", "scenarios": "经销商比较,经销商画像",
            "entity": "dealer_profile",
        },
    ]

    cfg = _config()
    with _connection(cfg) as connection:
        with connection.cursor() as cursor:
            plan = _preflight(cursor, metrics)
        connection.rollback()
    source_state = _source_preflight(plan["source"])

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "semantic_model_id": MODEL_ID,
        "business_domain_id": DOMAIN_ID,
        "data_source_id": DATA_SOURCE_ID,
        "planned_metrics": plan["metric_codes"],
        "time_dimension": "transaction_date",
        "time_anchor": "sales_order.created_date",
        "source_data_from": source_state["watermark"]["data_from"],
        "source_data_as_of": source_state["watermark"]["data_as_of"],
        "legacy_time_binding_to_clear": "general_ledger_year_month",
    }
    if not args.apply:
        summary["message"] = (
            "只读预检通过；未修改MySQL或Redis。使用--apply才会执行。"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return

    # Fail before the MySQL transaction when Redis is unavailable.  If Redis
    # later fails after COMMIT, the database remains authoritative and this
    # idempotent script can be rerun to finish the cache refresh.
    _redis_client(cfg).ping()
    metric_payloads: dict[str, str] = {}
    dimension_payloads: dict[str, str] = {}
    metadata_committed = False
    with _connection(cfg) as connection:
        try:
            connection.begin()
            with connection.cursor() as cursor:
                plan = _preflight(cursor, metrics)
                sales_entity = plan["entities"]["sales_order"]
                created_attr = _require_one(
                    cursor,
                    "SELECT id FROM semantic_model_attribute_config "
                    "WHERE semantic_model_id=%s AND entity_type_id=%s "
                    "AND mapping_table='sales_order' AND mapping_column='created_date' "
                    "AND is_deleted='0'",
                    (MODEL_ID, sales_entity["id"]),
                    "sales_order.created_date attribute",
                )
                for metric in metrics:
                    entity = plan["entities"][metric["entity"]]
                    _upsert_indicator(cursor, plan["project_id"], metric)
                    allowed_previous_entities = (
                        {sales_entity["code"], sales_entity["id"]}
                        if metric["code"] == "dealer_cooperation_count"
                        and entity["code"] == "dealer_profile"
                        else set()
                    )
                    _upsert_binding(
                        cursor,
                        entity["code"],
                        entity["id"],
                        metric,
                        allowed_previous_entities=allowed_previous_entities,
                    )
                _upsert_transaction_dimension(
                    cursor, plan["project_id"], sales_entity["id"], created_attr["id"]
                )
                # Keep the known-invalid rolling formula disabled.
                cursor.execute(
                    "UPDATE semantic_model_indicator SET is_deleted=1 "
                    "WHERE semantic_model_id=%s "
                    "AND indicator_code='dealer_rolling_period_performance_growth_rate'",
                    (MODEL_ID,),
                )
                metric_payloads, dimension_payloads = _build_cache_payloads(
                    cursor, plan["metric_codes"]
                )
            connection.commit()
            metadata_committed = True
        except Exception:
            if not metadata_committed:
                connection.rollback()
            raise

    try:
        _refresh_redis(cfg, metric_payloads, dimension_payloads)
    except Exception as exc:
        raise RuntimeError(
            "模型81指标元数据已提交，但Redis指标/维度缓存刷新失败；"
            "请修复Redis后原样重跑本幂等迁移"
        ) from exc

    summary.update({
        "metrics_upserted": len(metrics),
        "redis_metrics_refreshed": len(metric_payloads),
        "redis_dimensions_refreshed": len(dimension_payloads),
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
