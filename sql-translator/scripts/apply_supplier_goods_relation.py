"""Create and register the model-6 supplier/product bridge from purchase facts."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from uuid import UUID

import pymysql
import redis
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
TRANSLATOR_ROOT = ROOT / "sql-translator"
sys.path.insert(0, str(TRANSLATOR_ROOT))

from sql_translator_prod import SQLTranslatorProd  # noqa: E402
MODEL_ID = 6
DOMAIN_ID = 7
DATA_SOURCE_ID = 5
BRIDGE_CODE = "sales_transaction_domain_ent_supplier_product_relation"
BRIDGE_ID = "da600000-0000-4000-8000-000000000001"
SUPPLIER_ID = "688422ef-1f8d-4629-af2a-b7f5abb1e618"
PRODUCT_ID = "6c05f583-8a62-479b-908f-8f3ef01ebced"


def _metadata_connection() -> pymysql.Connection:
    cfg = dotenv_values(ROOT / ".env")
    return pymysql.connect(
        host=cfg["MYSQL_HOST"], port=int(cfg["MYSQL_PORT"]),
        user=cfg["MYSQL_USER"], password=cfg["MYSQL_PASSWORD"],
        database=cfg["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def _business_connection() -> pymysql.Connection:
    source = SQLTranslatorProd().fetch_data_source(str(MODEL_ID))
    return pymysql.connect(
        host=source["host"], port=int(source["port"]),
        user=source.get("username") or source.get("user"),
        password=source["password"], database=source["db_name"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _apply_business_bridge() -> int:
    aggregates: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {
            "first": None, "last": None, "orders": set(),
            "quantity": 0, "amount": 0,
        }
    )
    with _business_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS supplier_goods_relation (
                    supplier_id VARCHAR(64) NOT NULL,
                    goods_id VARCHAR(64) NOT NULL,
                    first_purchase_date DATE NOT NULL,
                    last_purchase_date DATE NOT NULL,
                    purchase_order_count INT NOT NULL DEFAULT 0,
                    total_purchase_quantity INT NOT NULL DEFAULT 0,
                    total_purchase_amount DECIMAL(18,2) NOT NULL DEFAULT 0,
                    update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (supplier_id, goods_id),
                    KEY idx_sgr_goods_id (goods_id),
                    KEY idx_sgr_last_purchase_date (last_purchase_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                COMMENT='供应商与商品采购事实派生关系'
                """
            )
            cursor.execute(
                "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='supplier_goods_relation' "
                "AND COLUMN_NAME='supplier_id'"
            )
            if cursor.fetchone()["COLLATION_NAME"] != "utf8mb4_unicode_ci":
                cursor.execute(
                    "ALTER TABLE supplier_goods_relation "
                    "MODIFY supplier_id VARCHAR(64) CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci NOT NULL, "
                    "MODIFY goods_id VARCHAR(64) CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci NOT NULL"
                )
            cursor.execute(
                "SELECT purchase_id,supplier_id,purchase_date,goods_list "
                "FROM purchase_order WHERE supplier_id IS NOT NULL"
            )
            for order in cursor.fetchall():
                items = json.loads(order["goods_list"] or "[]")
                for item in items:
                    goods_id = str(item.get("goods_id") or "").strip()
                    if not goods_id:
                        continue
                    key = (str(order["supplier_id"]), goods_id)
                    value = aggregates[key]
                    purchase_date = order["purchase_date"]
                    value["first"] = min(
                        x for x in (value["first"], purchase_date) if x is not None
                    )
                    value["last"] = max(
                        x for x in (value["last"], purchase_date) if x is not None
                    )
                    value["orders"].add(str(order["purchase_id"]))
                    quantity = int(item.get("quantity") or 0)
                    value["quantity"] += quantity
                    value["amount"] += quantity * float(item.get("price") or 0)
            rows = [
                (supplier_id, goods_id, value["first"], value["last"],
                 len(value["orders"]), value["quantity"], value["amount"])
                for (supplier_id, goods_id), value in aggregates.items()
            ]
            cursor.executemany(
                """
                INSERT INTO supplier_goods_relation (
                    supplier_id,goods_id,first_purchase_date,last_purchase_date,
                    purchase_order_count,total_purchase_quantity,total_purchase_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    first_purchase_date=VALUES(first_purchase_date),
                    last_purchase_date=VALUES(last_purchase_date),
                    purchase_order_count=VALUES(purchase_order_count),
                    total_purchase_quantity=VALUES(total_purchase_quantity),
                    total_purchase_amount=VALUES(total_purchase_amount)
                """,
                rows,
            )
        connection.commit()
    return len(rows)


def _apply_semantic_metadata() -> None:
    attributes = (
        ("supplier_id", "供应商ID", "varchar", 1),
        ("goods_id", "商品ID", "varchar", 1),
        ("first_purchase_date", "首次采购日期", "date", 0),
        ("last_purchase_date", "最近采购日期", "date", 0),
        ("purchase_order_count", "采购次数", "int", 0),
        ("total_purchase_quantity", "累计采购数量", "int", 0),
        ("total_purchase_amount", "累计采购金额", "decimal", 0),
    )
    relations = (
        ("da600000-0000-4000-8000-000000000101", SUPPLIER_ID, "供应商", BRIDGE_ID,
         "供应商商品关系", "supplier_supplier_product", "supplier_info-supplier_id",
         "supplier_goods_relation-supplier_id", "1:N"),
        ("da600000-0000-4000-8000-000000000102", BRIDGE_ID, "供应商商品关系", SUPPLIER_ID,
         "供应商", "supplier_product_supplier", "supplier_goods_relation-supplier_id",
         "supplier_info-supplier_id", "N:1"),
        ("da600000-0000-4000-8000-000000000103", PRODUCT_ID, "商品", BRIDGE_ID,
         "供应商商品关系", "product_supplier_product", "goods_info-goods_id",
         "supplier_goods_relation-goods_id", "1:N"),
        ("da600000-0000-4000-8000-000000000104", BRIDGE_ID, "供应商商品关系", PRODUCT_ID,
         "商品", "supplier_product_product", "supplier_goods_relation-goods_id",
         "goods_info-goods_id", "N:1"),
    )
    with _metadata_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM semantic_model_entity_type "
                "WHERE semantic_model_id=%s AND code=%s AND is_deleted=0",
                (MODEL_ID, BRIDGE_CODE),
            )
            entity_count = int(cursor.fetchone()["n"])
            if entity_count > 1:
                # Only rows owned by this migration use this fixed id/code.
                cursor.execute(
                    "DELETE FROM semantic_model_entity_type WHERE id=%s "
                    "AND semantic_model_id=%s AND code=%s",
                    (BRIDGE_ID, MODEL_ID, BRIDGE_CODE),
                )
                entity_count = 0
            entity_values = (
                BRIDGE_ID, MODEL_ID, DOMAIN_ID, "供应商商品关系",
                "由采购单明细派生的供应商与商品多对多关系", DATA_SOURCE_ID,
                "supplier_goods_relation", BRIDGE_CODE,
                "供应关系、供货关系、供应商品", "每日",
            )
            if entity_count == 0:
                cursor.execute(
                    """
                    INSERT INTO semantic_model_entity_type (
                        id,semantic_model_id,business_domain_id,name,description,
                        data_source_id,main_table_name,status,is_deleted,view_status,
                        code,alias,update_frequency,data_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,0,0,%s,%s,%s,1)
                    """,
                    entity_values,
                )
            else:
                cursor.execute(
                    "UPDATE semantic_model_entity_type SET name=%s,description=%s,"
                    "data_source_id=%s,main_table_name=%s,status=1,is_deleted=0,"
                    "alias=%s,update_frequency=%s,data_status=1 "
                    "WHERE semantic_model_id=%s AND code=%s",
                    (entity_values[3], entity_values[4], DATA_SOURCE_ID,
                     entity_values[6], entity_values[8], entity_values[9],
                     MODEL_ID, BRIDGE_CODE),
                )
            for index, (code, name, data_type, primary) in enumerate(attributes, 1):
                attr_id = f"da600000-0000-4000-8000-{index:012d}"
                cursor.execute(
                    """
                    INSERT INTO semantic_model_attribute_config (
                        id,semantic_model_id,entity_type_id,code,vectorization,
                        attr_name,description,mapping_table,mapping_column,
                        is_main_attribute,data_type,is_primary_key,is_unique,
                        is_required,is_deleted,view_status,data_status
                    ) VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,0,%s,%s,0,1,'0',0,1)
                    ON DUPLICATE KEY UPDATE attr_name=VALUES(attr_name),
                        mapping_table=VALUES(mapping_table),
                        mapping_column=VALUES(mapping_column),is_deleted='0'
                    """,
                    (attr_id, MODEL_ID, BRIDGE_ID, code, name, name,
                     "supplier_goods_relation", code, data_type, primary),
                )
            for relation in relations:
                cursor.execute(
                    "SELECT id FROM semantic_model_relation_config "
                    "WHERE semantic_model_id=%s AND source_entity_type_id=%s "
                    "AND code=%s AND is_deleted=0 LIMIT 1",
                    (MODEL_ID, relation[1], relation[5]),
                )
                existing = cursor.fetchone()
                values = (
                    "基于采购事实的可信关联", relation[2], relation[3],
                    relation[4], relation[4], relation[6], relation[7],
                    relation[8], MODEL_ID, relation[1], relation[5],
                )
                if existing:
                    cursor.execute(
                        "UPDATE semantic_model_relation_config SET description=%s,"
                        "source_entity_name=%s,target_entity_type_id=%s,"
                        "target_entity_name=%s,name=%s,source_table_column_name=%s,"
                        "target_table_column_name=%s,type=%s,status=1,is_deleted=0 "
                        "WHERE semantic_model_id=%s AND source_entity_type_id=%s "
                        "AND code=%s",
                        values,
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO semantic_model_relation_config (
                            id,semantic_model_id,business_domain_id,category,description,
                            source_entity_type_id,source_entity_name,target_entity_type_id,
                            target_entity_name,status,is_deleted,name,view_status,
                            source_table_column_name,target_table_column_name,code,type
                        ) VALUES (%s,%s,%s,'关联关系',%s,%s,%s,%s,%s,1,0,%s,0,%s,%s,%s,%s)
                        """,
                        (relation[0], MODEL_ID, DOMAIN_ID,
                         "基于采购事实的可信关联", relation[1], relation[2],
                         relation[3], relation[4], relation[4], relation[6],
                         relation[7], relation[5], relation[8]),
                    )
            cursor.execute(
                "SELECT id FROM semantic_model_table WHERE semantic_model_id=%s "
                "AND data_source_id=%s AND name='supplier_goods_relation' LIMIT 1",
                (MODEL_ID, DATA_SOURCE_ID),
            )
            table = cursor.fetchone()
            if table is None:
                cursor.execute(
                    "INSERT INTO semantic_model_table "
                    "(semantic_model_id,data_source_id,name,change_flag,description,comment,exist_status) "
                    "VALUES (%s,%s,'supplier_goods_relation',0,%s,%s,1)",
                    (MODEL_ID, DATA_SOURCE_ID, "供应商商品关系表", "采购事实派生"),
                )
                table_id = cursor.lastrowid
            else:
                table_id = table["id"]
            for code, name, data_type, _ in attributes:
                cursor.execute(
                    "SELECT id FROM semantic_model_field WHERE semantic_model_id=%s "
                    "AND table_id=%s AND name=%s LIMIT 1",
                    (MODEL_ID, table_id, code),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "INSERT INTO semantic_model_field "
                        "(semantic_model_id,data_source_id,table_id,name,type,null_flag,comment,description,exist_status) "
                        "VALUES (%s,%s,%s,%s,%s,'NO',%s,%s,1)",
                        (MODEL_ID, DATA_SOURCE_ID, table_id, code, data_type, name, name),
                    )
        connection.commit()


def _refresh_scoped_redis() -> int:
    cfg = dotenv_values(ROOT / ".env")
    client = redis.Redis(
        host=cfg["REDIS_HOST"], port=int(cfg["REDIS_PORT"]),
        db=int(str(cfg["REDIS_DB"]).split()[0]),
        password=cfg.get("REDIS_PASSWORD") or None, decode_responses=True,
    )
    codes = (
        BRIDGE_CODE, "sales_transaction_domain_ent_supplier",
        "sales_transaction_domain_ent_product",
    )
    with _metadata_connection() as connection:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(codes))
            cursor.execute(
                f"SELECT * FROM semantic_model_entity_type WHERE semantic_model_id=%s "
                f"AND code IN ({placeholders}) AND is_deleted=0",
                (MODEL_ID, *codes),
            )
            entities = cursor.fetchall()
            for entity in entities:
                cursor.execute(
                    "SELECT code AS attr_code,attr_name,mapping_table,mapping_column,"
                    "data_type,is_required,description AS attr_description,"
                    "is_primary_key,view_status FROM semantic_model_attribute_config "
                    "WHERE entity_type_id=%s AND is_deleted='0'",
                    (entity["id"],),
                )
                entity["semantic_model_attribute_config"] = cursor.fetchall()
                cursor.execute(
                    "SELECT code,name,target_entity_type_id,type,source_field,"
                    "target_field,source_table_column_name,target_table_column_name,"
                    "description,business_domain_id,status,is_deleted,source_entity_type_id "
                    "FROM semantic_model_relation_config WHERE semantic_model_id=%s "
                    "AND source_entity_type_id=%s AND is_deleted=0",
                    (MODEL_ID, entity["id"]),
                )
                entity["semantic_model_relation_config"] = cursor.fetchall()
    updated = 0
    for entity in entities:
        code = entity["code"]
        payload = {
            "code": code, "name": entity["name"], "alias": entity.get("alias"),
            "business_domain_id": str(entity["business_domain_id"]),
            "description": entity.get("description") or "",
            "main_table_name": entity["main_table_name"],
            "mapping_table": entity["main_table_name"],
            "data_source_id": str(entity["data_source_id"]),
            "status": entity.get("status"), "is_deleted": 0,
            "view_status": entity.get("view_status"),
            "update_frequency": entity.get("update_frequency"),
            "semantic_model_attribute_config": entity["semantic_model_attribute_config"],
            "semantic_model_relation_config": entity["semantic_model_relation_config"],
            "bind_metric": [], "bind_dimension": [],
            "semantic_model_entity_sub_table_mapping": [],
        }
        client.set(
            f"semantic_model:{MODEL_ID}:entity:{code}",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        )
        updated += 1
    return updated


def main() -> None:
    rows = _apply_business_bridge()
    _apply_semantic_metadata()
    redis_entities = _refresh_scoped_redis()
    print(json.dumps({
        "bridge_rows": rows,
        "semantic_model_id": MODEL_ID,
        "redis_entities_refreshed": redis_entities,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
