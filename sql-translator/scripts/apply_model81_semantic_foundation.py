"""Repair model 81's core entity graph and register its normalized lookup tables.

The script is intentionally dry-run by default.  Pass ``--apply`` to perform the
metadata transaction and the scoped Redis refresh.  It never changes the
``drug_sales`` business database.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pymysql
import redis
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = 81
DOMAIN_ID = 205
DATA_SOURCE_ID = 58
EXPECTED_SOURCE_DATABASE = "drug_sales"
ID_NAMESPACE = UUID("81818181-8181-4181-8181-818181818181")

DEALER_ID = "87093284-bf08-4f3b-ba4f-f23f7c705b8f"
PRODUCT_ID = "55282414-738a-4249-a847-6f324402506e"
SALES_ORDER_ID = "0c06869d-6bdb-499e-bb86-7529c61f17b8"
SALES_COMPANY_ID = "31a969c6-2013-43fb-a01b-157855930b95"
MANUFACTURER_ID = "7fdae747-b859-42bd-9e92-340091ace995"
SALESPERSON_ID = "f928e22f-f7dd-40f4-8e20-8ad507e9114a"
HOSPITAL_ID = "4c1e44b3-5aee-4b31-978e-ccfa216de38d"
PROJECT_ID = "1414a01e-eb67-4d8c-b663-e223ebb945c6"
WRONG_RELATION_ID = "fc4692f7-2c85-4639-8e89-b3d9a650cafd"


@dataclass(frozen=True)
class AttributeSpec:
    code: str
    name: str
    description: str
    main: bool = False
    vectorized: bool = False


@dataclass(frozen=True)
class EntitySpec:
    entity_id: str
    code: str
    name: str
    table: str
    description: str
    alias: str
    attributes: tuple[AttributeSpec, ...]


@dataclass(frozen=True)
class RelationSpec:
    code: str
    name: str
    source_code: str
    target_code: str
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    relation_type: str
    description: str


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid5(ID_NAMESPACE, ":".join((kind, *parts))))


CODE_PATCHES = {
    "f928e22f-f7dd-40f4-8e20-8ad507e9114a": ("salesperson", "salesperson", "业务员"),
    "5997bdba-8aed-4b7e-b423-de79cba766f7": ("product_category", "product_category", "产品分类"),
    "db24d124-f737-41af-8e53-edcfda0cb8cd": ("product_line", "product_line", "产品线"),
    "4c1e44b3-5aee-4b31-978e-ccfa216de38d": ("hospital", "hospital", "医院"),
    "1414a01e-eb67-4d8c-b663-e223ebb945c6": ("project", "project", "销售项目"),
}


ENTITY_SPECS = (
    EntitySpec(
        "81810000-0000-4000-8000-000000000001",
        "dealer_product_relation",
        "经销商商品关系",
        "dealer_product_relation",
        "经销商与其实际经营商品的多对多桥接关系。",
        "经销商商品关联、经销商品、商品经销渠道",
        (
            AttributeSpec("id", "关系ID", "经销商商品关系主键"),
            AttributeSpec("product_code", "商品编码", "关联商品编码"),
            AttributeSpec("dealer_code", "经销商编码", "关联经销商编码"),
        ),
    ),
    EntitySpec(
        "81810000-0000-4000-8000-000000000002",
        "product_department_relation",
        "商品适用科室关系",
        "product_dept_relation",
        "商品与适用科室的多对多桥接关系，并区分主要和次要适用。",
        "商品科室关联、适用科室关系、产品科室匹配",
        (
            AttributeSpec("id", "关系ID", "商品适用科室关系主键"),
            AttributeSpec("product_code", "商品编码", "关联商品编码"),
            AttributeSpec("dept_code", "科室编码", "关联科室编码"),
            AttributeSpec("relation_type", "适用类型", "1表示主要适用，2表示次要适用"),
        ),
    ),
    EntitySpec(
        "81810000-0000-4000-8000-000000000003",
        "department",
        "科室",
        "department",
        "医疗机构科室标准主数据。",
        "适用科室、临床科室、医院科室",
        (
            AttributeSpec("dept_code", "科室编码", "科室唯一编码"),
            AttributeSpec("dept_name", "科室名称", "标准科室名称", True, True),
        ),
    ),
    EntitySpec(
        "81810000-0000-4000-8000-000000000004",
        "dealer_profile",
        "经销商画像",
        "dealer_result",
        "按经销商汇总的经营规模、增长、合作稳定性和活跃度画像。",
        "经销商画像、渠道画像、经销商经营表现、经销商业务规模",
        (
            AttributeSpec("id", "画像ID", "经销商画像主键"),
            AttributeSpec("dealer_code", "经销商编码", "经销商唯一编码"),
            AttributeSpec("dealer_name", "经销商名称", "经销商名称", True, True),
            AttributeSpec("total_sales_amount", "累计销售额", "经销商历史累计销售额"),
            AttributeSpec("establish_date", "成立日期", "工商登记成立日期"),
            AttributeSpec("avg_monthly_growth_rate_3m", "近三月平均增长率", "近三个月平均月度销量增长率"),
            AttributeSpec("recent_year_amount", "近一年销售额", "最近一年销售额"),
            AttributeSpec("cooperation_months", "合作时长（月）", "合作持续月数"),
            AttributeSpec("cooperation_count", "合作次数", "历史合作次数"),
            AttributeSpec("business_status", "经营状态", "企业经营状态"),
            AttributeSpec("legal_representative", "法定代表人", "企业法定代表人"),
            AttributeSpec("registered_capital", "注册资本", "企业注册资本"),
            AttributeSpec("credit_code", "统一社会信用代码", "统一社会信用代码"),
            AttributeSpec("address", "地址", "经销商注册地址或经营地址"),
            AttributeSpec("email", "邮箱", "经销商联系邮箱"),
            AttributeSpec("manufacturer_list", "合作厂牌", "历史合作厂牌列表"),
            AttributeSpec("hospital_level_list", "合作医院等级", "历史覆盖医院等级列表"),
            AttributeSpec("activity_level", "活跃状态", "基于最近订单时间形成的活跃状态"),
        ),
    ),
    EntitySpec(
        "81810000-0000-4000-8000-000000000005",
        "all_hospital",
        "全国医院名录",
        "all_hospital",
        "全国二级、三级医院参考名录，用于地区和医院等级筛选。",
        "全国医院、医院名录、医疗机构名录",
        (
            AttributeSpec("hospital_name", "医院名称", "医院名称", True, True),
            AttributeSpec("province", "所在省", "医院所在省份"),
            AttributeSpec("city", "所在市", "医院所在城市"),
            AttributeSpec("hospital_level", "医院等级", "医院等级"),
        ),
    ),
)


SALES_ORDER_TIME_SPEC = EntitySpec(
    SALES_ORDER_ID,
    "sales_order",
    "销售订单",
    "sales_order",
    "销售订单事实表。",
    "销售订单",
    (
        AttributeSpec(
            "created_date", "交易日期", "销售订单真实发生日期"
        ),
    ),
)


RELATION_SPECS = (
    RelationSpec("sales_order_belongs_to_dealer", "归属经销商", "sales_order", "dealer", "sales_order", "dealer_code", "dealer", "dealer_code", "N:1", "销售订单按订单中的经销商编码归属实际成交经销商。"),
    RelationSpec("sales_order_belongs_to_product", "归属商品", "sales_order", "product", "sales_order", "product_code", "product", "product_code", "N:1", "销售订单按订单中的商品编码归属实际成交商品。"),
    RelationSpec("sales_order_belongs_to_hospital", "归属采购医院", "sales_order", "hospital", "sales_order", "hospital_id", "hospital", "hospital_id", "N:1", "销售订单按订单中的医院标识归属实际采购医院。"),
    RelationSpec("sales_order_belongs_to_salesperson", "归属业务员", "sales_order", "salesperson", "sales_order", "salesperson_code", "salesperson", "salesperson_code", "N:1", "销售订单按订单中的业务员编码归属负责业务员。"),
    RelationSpec("sales_order_belongs_to_project", "归属销售项目", "sales_order", "project", "sales_order", "project_code", "project", "project_code", "N:1", "销售订单按订单中的项目编码归属销售项目。"),
    RelationSpec("salesperson_belongs_to_sales_company", "归属国药公司", "salesperson", "sales_company", "salesperson", "guoyao_code", "guoyao_company", "guoyao_code", "N:1", "业务员按国药编码归属国药公司。"),
    RelationSpec("product_belongs_to_manufacturer", "归属生产厂家", "product", "main_data_domain_ent_manufacturer", "product", "manufacturer_code", "manufacturer", "manufacturer_code", "N:1", "商品按生产厂家编码归属生产厂家。"),
    RelationSpec("dealer_has_product_relation", "关联商品经营关系", "dealer", "dealer_product_relation", "dealer", "dealer_code", "dealer_product_relation", "dealer_code", "1:N", "经销商通过桥接表关联其经营商品。"),
    RelationSpec("dealer_product_relation_belongs_to_dealer", "归属经销商", "dealer_product_relation", "dealer", "dealer_product_relation", "dealer_code", "dealer", "dealer_code", "N:1", "商品经营关系归属一个经销商。"),
    RelationSpec("product_has_dealer_relation", "关联经销渠道关系", "product", "dealer_product_relation", "product", "product_code", "dealer_product_relation", "product_code", "1:N", "商品通过桥接表关联经营该商品的经销商。"),
    RelationSpec("dealer_product_relation_belongs_to_product", "归属商品", "dealer_product_relation", "product", "dealer_product_relation", "product_code", "product", "product_code", "N:1", "商品经营关系归属一个商品。"),
    RelationSpec("product_has_department_relation", "关联适用科室关系", "product", "product_department_relation", "product", "product_code", "product_dept_relation", "product_code", "1:N", "商品通过桥接表关联主要或次要适用科室。"),
    RelationSpec("product_department_relation_belongs_to_product", "归属商品", "product_department_relation", "product", "product_dept_relation", "product_code", "product", "product_code", "N:1", "适用科室关系归属一个商品。"),
    RelationSpec("department_has_product_relation", "关联适用商品关系", "department", "product_department_relation", "department", "dept_code", "product_dept_relation", "dept_code", "1:N", "科室通过桥接表关联适用于该科室的商品。"),
    RelationSpec("product_department_relation_belongs_to_department", "归属科室", "product_department_relation", "department", "product_dept_relation", "dept_code", "department", "dept_code", "N:1", "适用科室关系归属一个标准科室。"),
    RelationSpec("dealer_has_profile", "具备经营画像", "dealer", "dealer_profile", "dealer", "dealer_code", "dealer_result", "dealer_code", "1:1", "经销商关联其唯一汇总画像。"),
    RelationSpec("dealer_profile_belongs_to_dealer", "归属经销商", "dealer_profile", "dealer", "dealer_result", "dealer_code", "dealer", "dealer_code", "1:1", "经销商画像归属一个经销商。"),
)


# Every new fact-to-dimension edge is the exact reverse of one active,
# administrator-configured model-81 relation.  Preflight validates these rows
# before the migration is allowed to create the reverse traversal.
PROVEN_FORWARD_RELATIONS = (
    ("dealer_place_sales_order", DEALER_ID, SALES_ORDER_ID,
     "dealer-dealer_code", "sales_order-dealer_code"),
    ("pharmaceutical_products_generation_sales_order", PRODUCT_ID, SALES_ORDER_ID,
     "product-product_code", "sales_order-product_code"),
    ("hospital_purchase_generation_sales_order", HOSPITAL_ID, SALES_ORDER_ID,
     "hospital-hospital_id", "sales_order-hospital_id"),
    ("salesperson_responsible_sales_order", SALESPERSON_ID, SALES_ORDER_ID,
     "salesperson-salesperson_code", "sales_order-salesperson_code"),
    ("sales_item_association_sales_order", PROJECT_ID, SALES_ORDER_ID,
     "project-project_code", "sales_order-project_code"),
    ("china_national_pharmaceuticals_subsidiary_sales_representative",
     SALES_COMPANY_ID, SALESPERSON_ID,
     "guoyao_company-guoyao_code", "salesperson-guoyao_code"),
    ("manufacturer_production_pharmaceutical_products", MANUFACTURER_ID, PRODUCT_ID,
     "manufacturer-manufacturer_code", "product-manufacturer_code"),
)


METADATA_COLUMNS = {
    "semantic_model_business_domain": {"id", "semantic_model_id", "is_deleted"},
    "semantic_model_data_source": {"id", "semantic_model_id", "host", "port", "username", "password", "db_name", "is_deleted"},
    "semantic_model_entity_type": {"id", "semantic_model_id", "business_domain_id", "name", "description", "data_source_id", "main_table_name", "status", "is_deleted", "view_status", "code", "alias", "update_frequency", "data_status"},
    "semantic_model_attribute_config": {"id", "semantic_model_id", "entity_type_id", "code", "vectorization", "attr_name", "description", "mapping_table", "mapping_column", "is_main_attribute", "data_type", "is_primary_key", "is_unique", "is_required", "is_deleted", "view_status", "data_status"},
    "semantic_model_relation_config": {"id", "semantic_model_id", "business_domain_id", "category", "description", "source_entity_type_id", "source_entity_name", "target_entity_type_id", "target_entity_name", "status", "is_deleted", "name", "source_field", "target_field", "source_field_name", "target_field_name", "view_status", "source_table_column_name", "target_table_column_name", "code", "type"},
    "semantic_model_table": {"id", "semantic_model_id", "data_source_id", "name", "change_flag", "description", "comment", "is_deleted", "exist_status"},
    "semantic_model_field": {"id", "semantic_model_id", "data_source_id", "table_id", "name", "type", "null_flag", "comment", "description", "is_deleted", "exist_status"},
}


def _config() -> dict[str, Any]:
    cfg = dict(dotenv_values(ROOT / ".env"))
    required = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "REDIS_HOST", "REDIS_PORT", "REDIS_DB")
    missing = [key for key in required if cfg.get(key) in (None, "")]
    if missing:
        raise RuntimeError(f"根目录.env缺少配置: {', '.join(missing)}")
    return cfg


def _metadata_connection(cfg: dict[str, Any]) -> pymysql.Connection:
    return pymysql.connect(
        host=cfg["MYSQL_HOST"], port=int(cfg["MYSQL_PORT"]),
        user=cfg["MYSQL_USER"], password=cfg["MYSQL_PASSWORD"],
        database=cfg["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        connect_timeout=10, read_timeout=30, write_timeout=30,
    )


def _source_connection(source: dict[str, Any]) -> pymysql.Connection:
    return pymysql.connect(
        host=source["host"], port=int(source["port"]),
        user=source["username"], password=source["password"],
        database=source["db_name"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=10, read_timeout=30, write_timeout=30,
    )


def _redis_client(cfg: dict[str, Any]) -> redis.Redis:
    return redis.Redis(
        host=cfg["REDIS_HOST"], port=int(cfg["REDIS_PORT"]),
        db=int(str(cfg["REDIS_DB"]).split()[0]),
        password=cfg.get("REDIS_PASSWORD") or None,
        decode_responses=True, socket_connect_timeout=5, socket_timeout=10,
    )


def _one(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"{label}应唯一，实际为{len(rows)}条")
    return rows[0]


def _metadata_preflight(cursor: pymysql.cursors.DictCursor, lock: bool) -> tuple[dict[str, Any], dict[str, str]]:
    for table, required in METADATA_COLUMNS.items():
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,),
        )
        present = {row["COLUMN_NAME"] for row in cursor.fetchall()}
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"元数据表{table}缺少字段: {', '.join(missing)}")

    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        "SELECT id FROM semantic_model_business_domain WHERE id=%s "
        "AND semantic_model_id=%s AND is_deleted=0" + suffix,
        (DOMAIN_ID, MODEL_ID),
    )
    _one(cursor.fetchall(), "模型81业务域205")
    cursor.execute(
        "SELECT * FROM semantic_model_data_source WHERE id=%s "
        "AND semantic_model_id=%s AND is_deleted=0" + suffix,
        (DATA_SOURCE_ID, MODEL_ID),
    )
    source = _one(cursor.fetchall(), "模型81数据源58")
    if source.get("db_name") != EXPECTED_SOURCE_DATABASE:
        raise RuntimeError(
            f"数据源58数据库应为{EXPECTED_SOURCE_DATABASE}，实际为{source.get('db_name')}"
        )
    if not all(source.get(key) not in (None, "") for key in ("host", "port", "username", "password")):
        raise RuntimeError("数据源58连接配置不完整")

    resolved = {
        "dealer": DEALER_ID,
        "product": PRODUCT_ID,
        "sales_order": SALES_ORDER_ID,
        "sales_company": SALES_COMPANY_ID,
        "main_data_domain_ent_manufacturer": MANUFACTURER_ID,
    }
    for entity_id, (desired_code, table, name) in CODE_PATCHES.items():
        cursor.execute(
            "SELECT * FROM semantic_model_entity_type WHERE semantic_model_id=%s "
            "AND id=%s AND is_deleted=0" + suffix, (MODEL_ID, entity_id),
        )
        row = _one(cursor.fetchall(), f"待补code实体{name}")
        if row["main_table_name"] != table or row["business_domain_id"] != DOMAIN_ID or row["data_source_id"] != DATA_SOURCE_ID:
            raise RuntimeError(f"实体{name}的表、业务域或数据源与预期不一致")
        if (row.get("code") or "") not in ("", desired_code):
            raise RuntimeError(f"实体{name}已有非预期code: {row.get('code')}")
        cursor.execute(
            "SELECT id FROM semantic_model_entity_type WHERE semantic_model_id=%s "
            "AND code=%s AND id<>%s AND is_deleted=0", (MODEL_ID, desired_code, entity_id),
        )
        if cursor.fetchall():
            raise RuntimeError(f"实体code {desired_code}已被其他实体占用")
        resolved[desired_code] = entity_id

    for code, expected_id, table in (
        ("dealer", DEALER_ID, "dealer"),
        ("product", PRODUCT_ID, "product"),
        ("sales_order", SALES_ORDER_ID, "sales_order"),
        ("sales_company", SALES_COMPANY_ID, "guoyao_company"),
        ("main_data_domain_ent_manufacturer", MANUFACTURER_ID, "manufacturer"),
    ):
        cursor.execute(
            "SELECT id,main_table_name,business_domain_id,data_source_id FROM semantic_model_entity_type "
            "WHERE semantic_model_id=%s AND code=%s AND is_deleted=0" + suffix,
            (MODEL_ID, code),
        )
        row = _one(cursor.fetchall(), f"基础实体{code}")
        if row["id"] != expected_id or row["main_table_name"] != table or row["business_domain_id"] != DOMAIN_ID or row["data_source_id"] != DATA_SOURCE_ID:
            raise RuntimeError(f"基础实体{code}身份或作用域与预期不一致")

    for code, source_id, target_id, source_field, target_field in PROVEN_FORWARD_RELATIONS:
        cursor.execute(
            "SELECT source_entity_type_id,target_entity_type_id,"
            "source_table_column_name,target_table_column_name "
            "FROM semantic_model_relation_config WHERE semantic_model_id=%s "
            "AND code=%s AND status=1 AND is_deleted=0" + suffix,
            (MODEL_ID, code),
        )
        row = _one(cursor.fetchall(), f"反向关系依据{code}")
        actual = (
            row["source_entity_type_id"], row["target_entity_type_id"],
            row["source_table_column_name"], row["target_table_column_name"],
        )
        expected = (source_id, target_id, source_field, target_field)
        if actual != expected:
            raise RuntimeError(f"关系{code}的实体或物理字段已变化，拒绝创建反向边")

    for spec in ENTITY_SPECS:
        cursor.execute(
            "SELECT id,code,business_domain_id,data_source_id FROM semantic_model_entity_type "
            "WHERE semantic_model_id=%s AND main_table_name=%s AND is_deleted=0" + suffix,
            (MODEL_ID, spec.table),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"物理表{spec.table}对应多个活动实体，拒绝自动选择")
        if rows:
            row = rows[0]
            if (row.get("code") or "") not in ("", spec.code):
                raise RuntimeError(f"表{spec.table}已有不兼容实体code: {row.get('code')}")
            if row["business_domain_id"] != DOMAIN_ID or row["data_source_id"] != DATA_SOURCE_ID:
                raise RuntimeError(f"表{spec.table}已有实体不属于模型81主业务域/数据源58")
            resolved[spec.code] = row["id"]
        else:
            cursor.execute("SELECT semantic_model_id,main_table_name FROM semantic_model_entity_type WHERE id=%s", (spec.entity_id,))
            if cursor.fetchall():
                raise RuntimeError(f"预设实体ID已被占用: {spec.entity_id}")
            resolved[spec.code] = spec.entity_id
        cursor.execute(
            "SELECT id FROM semantic_model_entity_type WHERE semantic_model_id=%s "
            "AND code=%s AND id<>%s AND is_deleted=0", (MODEL_ID, spec.code, resolved[spec.code]),
        )
        if cursor.fetchall():
            raise RuntimeError(f"实体code {spec.code}已被其他实体占用")

    cursor.execute(
        "SELECT * FROM semantic_model_relation_config WHERE semantic_model_id=%s "
        "AND is_deleted=0 AND (id=%s OR code='dealer_sales_product')" + suffix,
        (MODEL_ID, WRONG_RELATION_ID),
    )
    wrong_rows = cursor.fetchall()
    if len(wrong_rows) > 1:
        raise RuntimeError("错误的经销商直连商品关系存在多条活动记录，拒绝自动删除")
    if wrong_rows:
        row = wrong_rows[0]
        expected = (
            row["id"] == WRONG_RELATION_ID
            and row["source_entity_type_id"] == DEALER_ID
            and row["target_entity_type_id"] == PRODUCT_ID
            and row["source_table_column_name"] == "dealer-dealer_code"
            and row["target_table_column_name"] == "product-product_code"
        )
        if not expected:
            raise RuntimeError("dealer_sales_product关系已被人工改动，拒绝覆盖")
    return source, resolved


def _source_preflight(source: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    required = {spec.table: {attr.code for attr in spec.attributes} for spec in ENTITY_SPECS}
    required.update({
        "dealer": {"dealer_code"},
        "product": {"product_code", "manufacturer_code"},
        "sales_order": {
            "dealer_code", "product_code", "hospital_id",
            "salesperson_code", "project_code", "created_date",
        },
        "hospital": {"hospital_id"},
        "salesperson": {"salesperson_code", "guoyao_code"},
        "project": {"project_code"},
        "guoyao_company": {"guoyao_code"},
        "manufacturer": {"manufacturer_code"},
    })
    schema: dict[str, dict[str, dict[str, Any]]] = {}
    with _source_connection(source) as connection:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(required))
            cursor.execute(
                f"SELECT TABLE_NAME,TABLE_TYPE FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})",
                tuple(required),
            )
            table_types = {row["TABLE_NAME"]: row["TABLE_TYPE"] for row in cursor.fetchall()}
            for table in required:
                if table_types.get(table) != "BASE TABLE":
                    raise RuntimeError(f"源库缺少基础表{table}")
                cursor.execute(
                    "SELECT COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_KEY,"
                    "COLUMN_COMMENT,CHARACTER_SET_NAME,COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                    (table,),
                )
                schema[table] = {row["COLUMN_NAME"]: row for row in cursor.fetchall()}
                missing = sorted(required[table] - set(schema[table]))
                if missing:
                    raise RuntimeError(f"源表{table}缺少字段: {', '.join(missing)}")

            cursor.execute(
                "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME IN ('dealer','product','dealer_product_relation',"
                "'product_dept_relation','department','dealer_result','hospital',"
                "'salesperson','project','guoyao_company','manufacturer') "
                "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX"
            )
            indexes: dict[str, dict[str, list[str]]] = {}
            for row in cursor.fetchall():
                if int(row["NON_UNIQUE"]) == 0:
                    indexes.setdefault(row["TABLE_NAME"], {}).setdefault(row["INDEX_NAME"], []).append(row["COLUMN_NAME"])
            expected_unique = {
                "dealer": ("dealer_code",),
                "product": ("product_code",),
                "dealer_product_relation": ("product_code", "dealer_code"),
                "product_dept_relation": ("product_code", "dept_code", "relation_type"),
                "department": ("dept_code",),
                "dealer_result": ("dealer_code",),
                "hospital": ("hospital_id",),
                "salesperson": ("salesperson_code",),
                "project": ("project_code",),
                "guoyao_company": ("guoyao_code",),
                "manufacturer": ("manufacturer_code",),
            }
            for table, columns in expected_unique.items():
                if list(columns) not in indexes.get(table, {}).values():
                    raise RuntimeError(f"源表{table}缺少唯一键({', '.join(columns)})")

            join_pairs = (
                (("dealer", "dealer_code"), ("dealer_product_relation", "dealer_code")),
                (("product", "product_code"), ("dealer_product_relation", "product_code")),
                (("product", "product_code"), ("product_dept_relation", "product_code")),
                (("department", "dept_code"), ("product_dept_relation", "dept_code")),
                (("dealer", "dealer_code"), ("dealer_result", "dealer_code")),
                (("sales_order", "dealer_code"), ("dealer", "dealer_code")),
                (("sales_order", "product_code"), ("product", "product_code")),
                (("sales_order", "hospital_id"), ("hospital", "hospital_id")),
                (("sales_order", "salesperson_code"), ("salesperson", "salesperson_code")),
                (("sales_order", "project_code"), ("project", "project_code")),
                (("salesperson", "guoyao_code"), ("guoyao_company", "guoyao_code")),
                (("product", "manufacturer_code"), ("manufacturer", "manufacturer_code")),
            )
            for left, right in join_pairs:
                left_col, right_col = schema[left[0]][left[1]], schema[right[0]][right[1]]
                if left_col["DATA_TYPE"] not in {"char", "varchar"} or right_col["DATA_TYPE"] not in {"char", "varchar"}:
                    raise RuntimeError(f"关联键{left}与{right}不是字符字段")
                if (left_col["CHARACTER_SET_NAME"], left_col["COLLATION_NAME"]) != (right_col["CHARACTER_SET_NAME"], right_col["COLLATION_NAME"]):
                    raise RuntimeError(f"关联键{left}与{right}字符集或排序规则不一致")
            existence_checks = (
                (
                    "SELECT EXISTS(SELECT 1 FROM dealer_product_relation r "
                    "JOIN dealer d ON d.dealer_code=r.dealer_code "
                    "JOIN product p ON p.product_code=r.product_code LIMIT 1) AS present",
                    "经销商—商品桥接链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM product_dept_relation r "
                    "JOIN product p ON p.product_code=r.product_code "
                    "JOIN department d ON d.dept_code=r.dept_code LIMIT 1) AS present",
                    "商品—科室桥接链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM dealer_result r "
                    "JOIN dealer d ON d.dealer_code=r.dealer_code LIMIT 1) AS present",
                    "经销商—画像链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM sales_order s "
                    "JOIN dealer d ON d.dealer_code=s.dealer_code LIMIT 1) AS present",
                    "订单—经销商事实链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM sales_order s "
                    "JOIN product p ON p.product_code=s.product_code LIMIT 1) AS present",
                    "订单—商品事实链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM sales_order s "
                    "JOIN hospital h ON h.hospital_id=s.hospital_id LIMIT 1) AS present",
                    "订单—医院事实链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM sales_order s "
                    "JOIN salesperson p ON p.salesperson_code=s.salesperson_code LIMIT 1) AS present",
                    "订单—业务员事实链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM sales_order s "
                    "JOIN project p ON p.project_code=s.project_code LIMIT 1) AS present",
                    "订单—销售项目事实链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM salesperson s "
                    "JOIN guoyao_company g ON g.guoyao_code=s.guoyao_code LIMIT 1) AS present",
                    "业务员—国药公司链路",
                ),
                (
                    "SELECT EXISTS(SELECT 1 FROM product p "
                    "JOIN manufacturer m ON m.manufacturer_code=p.manufacturer_code LIMIT 1) AS present",
                    "商品—生产厂家链路",
                ),
            )
            for sql, label in existence_checks:
                cursor.execute(sql)
                if not int(cursor.fetchone()["present"]):
                    raise RuntimeError(f"{label}没有可关联数据")

            integrity_checks = (
                (
                    "SELECT COUNT(*) AS orphan_count FROM sales_order s "
                    "LEFT JOIN dealer d ON d.dealer_code=s.dealer_code "
                    "WHERE d.dealer_code IS NULL",
                    "订单—经销商",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM sales_order s "
                    "LEFT JOIN product p ON p.product_code=s.product_code "
                    "WHERE p.product_code IS NULL",
                    "订单—商品",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM sales_order s "
                    "LEFT JOIN hospital h ON h.hospital_id=s.hospital_id "
                    "WHERE h.hospital_id IS NULL",
                    "订单—医院",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM sales_order s "
                    "LEFT JOIN salesperson p ON p.salesperson_code=s.salesperson_code "
                    "WHERE p.salesperson_code IS NULL",
                    "订单—业务员",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM sales_order s "
                    "LEFT JOIN project p ON p.project_code=s.project_code "
                    "WHERE p.project_code IS NULL",
                    "订单—销售项目",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM salesperson s "
                    "LEFT JOIN guoyao_company g ON g.guoyao_code=s.guoyao_code "
                    "WHERE g.guoyao_code IS NULL",
                    "业务员—国药公司",
                ),
                (
                    "SELECT COUNT(*) AS orphan_count FROM product p "
                    "LEFT JOIN manufacturer m ON m.manufacturer_code=p.manufacturer_code "
                    "WHERE m.manufacturer_code IS NULL",
                    "商品—生产厂家",
                ),
            )
            for sql, label in integrity_checks:
                cursor.execute(sql)
                orphan_count = int(cursor.fetchone()["orphan_count"])
                if orphan_count:
                    raise RuntimeError(
                        f"{label}存在{orphan_count}条孤儿记录，拒绝启用可信反向关系"
                    )
    return schema


def _upsert_entity(cursor: pymysql.cursors.DictCursor, spec: EntitySpec, entity_id: str) -> str:
    cursor.execute(
        "SELECT id FROM semantic_model_entity_type WHERE semantic_model_id=%s "
        "AND main_table_name=%s AND is_deleted=0 FOR UPDATE", (MODEL_ID, spec.table),
    )
    rows = cursor.fetchall()
    if rows:
        entity_id = _one(rows, f"实体{spec.code}")["id"]
        cursor.execute(
            "UPDATE semantic_model_entity_type SET business_domain_id=%s,name=%s,description=%s,"
            "data_source_id=%s,status=1,is_deleted=0,view_status=0,code=%s,alias=%s,"
            "update_frequency='每日',data_status=0 WHERE semantic_model_id=%s AND id=%s",
            (DOMAIN_ID, spec.name, spec.description, DATA_SOURCE_ID, spec.code,
             spec.alias, MODEL_ID, entity_id),
        )
    else:
        cursor.execute(
            "INSERT INTO semantic_model_entity_type "
            "(id,semantic_model_id,business_domain_id,name,description,data_source_id,"
            "main_table_name,status,is_deleted,view_status,code,alias,update_frequency,data_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,1,0,0,%s,%s,'每日',0)",
            (entity_id, MODEL_ID, DOMAIN_ID, spec.name, spec.description,
             DATA_SOURCE_ID, spec.table, spec.code, spec.alias),
        )
    return entity_id


def _upsert_attribute(
    cursor: pymysql.cursors.DictCursor,
    entity_id: str,
    spec: EntitySpec,
    attr: AttributeSpec,
    column: dict[str, Any],
) -> None:
    cursor.execute(
        "SELECT * FROM semantic_model_attribute_config WHERE semantic_model_id=%s "
        "AND entity_type_id=%s AND code=%s FOR UPDATE", (MODEL_ID, entity_id, attr.code),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"实体{spec.code}属性{attr.code}存在重复元数据")
    primary = int(column["COLUMN_KEY"] == "PRI")
    unique = int(column["COLUMN_KEY"] == "UNI")
    required = int(column["IS_NULLABLE"] == "NO")
    description = column.get("COLUMN_COMMENT") or attr.description
    if rows:
        row = rows[0]
        if row.get("mapping_table") not in (None, "", spec.table) or row.get("mapping_column") not in (None, "", attr.code):
            raise RuntimeError(f"实体{spec.code}属性{attr.code}已有不兼容字段映射")
        cursor.execute(
            "UPDATE semantic_model_attribute_config SET vectorization=%s,attr_name=%s,"
            "description=%s,mapping_table=%s,mapping_column=%s,is_main_attribute=%s,"
            "data_type=%s,is_primary_key=%s,is_unique=%s,is_required=%s,is_deleted='0',"
            "view_status=0,data_status=0 WHERE id=%s AND view_status=%s",
            (int(attr.vectorized), attr.name, description, spec.table, attr.code,
             int(attr.main), column["DATA_TYPE"], primary, unique, required,
             row["id"], row["view_status"]),
        )
    else:
        attr_id = _stable_id("attribute", spec.code, attr.code)
        cursor.execute("SELECT id FROM semantic_model_attribute_config WHERE id=%s", (attr_id,))
        if cursor.fetchall():
            raise RuntimeError(f"预设属性ID已被占用: {attr_id}")
        cursor.execute(
            "INSERT INTO semantic_model_attribute_config "
            "(id,semantic_model_id,entity_type_id,code,vectorization,attr_name,description,"
            "mapping_table,mapping_column,is_main_attribute,data_type,is_primary_key,is_unique,"
            "is_required,is_deleted,view_status,data_status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'0',0,0)",
            (attr_id, MODEL_ID, entity_id, attr.code, int(attr.vectorized), attr.name,
             description, spec.table, attr.code, int(attr.main), column["DATA_TYPE"],
             primary, unique, required),
        )


def _upsert_physical_metadata(
    cursor: pymysql.cursors.DictCursor,
    spec: EntitySpec,
    columns: dict[str, dict[str, Any]],
) -> None:
    cursor.execute(
        "SELECT * FROM semantic_model_table WHERE semantic_model_id=%s AND data_source_id=%s "
        "AND name=%s AND is_deleted=0 FOR UPDATE", (MODEL_ID, DATA_SOURCE_ID, spec.table),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"物理表元数据{spec.table}存在重复活动记录")
    if rows:
        table_id = rows[0]["id"]
        cursor.execute(
            "UPDATE semantic_model_table SET change_flag=0,description=COALESCE(NULLIF(description,''),%s),"
            "comment=COALESCE(NULLIF(comment,''),%s),is_deleted=0,exist_status=1 WHERE id=%s",
            (spec.name, spec.description, table_id),
        )
    else:
        cursor.execute(
            "INSERT INTO semantic_model_table "
            "(semantic_model_id,data_source_id,name,change_flag,description,comment,is_deleted,exist_status) "
            "VALUES (%s,%s,%s,0,%s,%s,0,1)",
            (MODEL_ID, DATA_SOURCE_ID, spec.table, spec.name, spec.description),
        )
        table_id = cursor.lastrowid
    for attr in spec.attributes:
        column = columns[attr.code]
        cursor.execute(
            "SELECT * FROM semantic_model_field WHERE semantic_model_id=%s AND table_id=%s "
            "AND name=%s AND is_deleted=0 FOR UPDATE", (MODEL_ID, table_id, attr.code),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"物理字段{spec.table}.{attr.code}存在重复活动记录")
        description = column.get("COLUMN_COMMENT") or attr.description
        if rows:
            cursor.execute(
                "UPDATE semantic_model_field SET data_source_id=%s,type=%s,null_flag=%s,"
                "comment=COALESCE(NULLIF(comment,''),%s),description=COALESCE(NULLIF(description,''),%s),"
                "is_deleted=0,exist_status=1 WHERE id=%s",
                (DATA_SOURCE_ID, column["COLUMN_TYPE"], column["IS_NULLABLE"],
                 description, description, rows[0]["id"]),
            )
        else:
            cursor.execute(
                "INSERT INTO semantic_model_field "
                "(semantic_model_id,data_source_id,table_id,name,type,null_flag,comment,description,is_deleted,exist_status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,1)",
                (MODEL_ID, DATA_SOURCE_ID, table_id, attr.code,
                 column["COLUMN_TYPE"], column["IS_NULLABLE"], description, description),
            )


def _upsert_relation(
    cursor: pymysql.cursors.DictCursor,
    spec: RelationSpec,
    entity_ids: dict[str, str],
    entity_names: dict[str, str],
) -> None:
    source_id, target_id = entity_ids[spec.source_code], entity_ids[spec.target_code]
    cursor.execute(
        "SELECT * FROM semantic_model_relation_config WHERE semantic_model_id=%s "
        "AND code=%s FOR UPDATE",
        (MODEL_ID, spec.code),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"关系{spec.code}存在重复元数据")
    values = (
        DOMAIN_ID, "关联关系", spec.description, entity_names[spec.source_code],
        target_id, entity_names[spec.target_code], spec.name, spec.source_column,
        spec.target_column, spec.source_column, spec.target_column,
        f"{spec.source_table}-{spec.source_column}",
        f"{spec.target_table}-{spec.target_column}", spec.relation_type,
    )
    if rows:
        row = rows[0]
        if (
            row["source_entity_type_id"] != source_id
            or row["target_entity_type_id"] != target_id
        ):
            raise RuntimeError(f"关系{spec.code}已有不兼容源或目标实体")
        cursor.execute(
            "UPDATE semantic_model_relation_config SET business_domain_id=%s,category=%s,"
            "description=%s,source_entity_name=%s,target_entity_type_id=%s,target_entity_name=%s,"
            "status=1,is_deleted=0,name=%s,source_field=%s,target_field=%s,source_field_name=%s,"
            "target_field_name=%s,view_status=0,source_table_column_name=%s,"
            "target_table_column_name=%s,type=%s WHERE id=%s",
            (*values, row["id"]),
        )
    else:
        relation_id = _stable_id("relation", spec.source_code, spec.code)
        cursor.execute("SELECT id FROM semantic_model_relation_config WHERE id=%s", (relation_id,))
        if cursor.fetchall():
            raise RuntimeError(f"预设关系ID已被占用: {relation_id}")
        cursor.execute(
            "INSERT INTO semantic_model_relation_config "
            "(id,semantic_model_id,business_domain_id,category,description,source_entity_type_id,"
            "source_entity_name,target_entity_type_id,target_entity_name,status,is_deleted,name,"
            "source_field,target_field,source_field_name,target_field_name,view_status,"
            "source_table_column_name,target_table_column_name,code,type) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,%s,%s,%s,%s,%s,0,%s,%s,%s,%s)",
            (
                relation_id, MODEL_ID, values[0], values[1], values[2],
                source_id, values[3], values[4], values[5], values[6],
                values[7], values[8], values[9], values[10], values[11],
                values[12], spec.code, values[13],
            ),
        )


def _build_redis_payloads(
    cursor: pymysql.cursors.DictCursor,
    codes: set[str],
) -> dict[str, str]:
    placeholders = ",".join(["%s"] * len(codes))
    cursor.execute(
        f"SELECT * FROM semantic_model_entity_type WHERE semantic_model_id=%s "
        f"AND code IN ({placeholders}) AND is_deleted=0", (MODEL_ID, *sorted(codes)),
    )
    entities = cursor.fetchall()
    if len(entities) != len(codes):
        found = {row["code"] for row in entities}
        raise RuntimeError(f"Redis刷新缺少实体: {', '.join(sorted(codes - found))}")
    payloads: dict[str, str] = {}
    for entity in entities:
        cursor.execute(
            "SELECT code,code AS attr_code,attr_name,mapping_table,mapping_column,data_type,"
            "is_required,description AS attr_description,is_primary_key,view_status "
            "FROM semantic_model_attribute_config WHERE semantic_model_id=%s "
            "AND entity_type_id=%s AND is_deleted='0' ORDER BY code",
            (MODEL_ID, entity["id"]),
        )
        attributes = cursor.fetchall()
        cursor.execute(
            "SELECT code,name,target_entity_type_id,type,source_field,target_field,"
            "source_table_column_name,target_table_column_name,description,business_domain_id,"
            "status,is_deleted,source_entity_type_id FROM semantic_model_relation_config "
            "WHERE semantic_model_id=%s AND source_entity_type_id=%s AND is_deleted=0 ORDER BY code",
            (MODEL_ID, entity["id"]),
        )
        relations = cursor.fetchall()
        cursor.execute(
            "SELECT indicator_code,indicator_name,indicator_logic FROM semantic_model_entity_bind_indicator "
            "WHERE semantic_model_id=%s AND entity_code=%s AND is_deleted=0 ORDER BY indicator_code",
            (MODEL_ID, entity["code"]),
        )
        bind_metric = cursor.fetchall()
        cursor.execute(
            "SELECT sub_table_name,main_join_column,sub_join_column,join_condition,view_status "
            "FROM semantic_model_entity_sub_table_mapping WHERE semantic_model_id=%s "
            "AND entity_type_id=%s AND is_deleted=0 ORDER BY sub_table_name",
            (MODEL_ID, entity["id"]),
        )
        sub_tables = cursor.fetchall()
        payload = {
            "id": entity["id"],
            "semantic_model_id": str(MODEL_ID),
            "code": entity["code"], "name": entity["name"],
            "alias": entity.get("alias"),
            "business_domain_id": str(entity["business_domain_id"]),
            "description": entity.get("description") or "",
            "main_table_name": entity["main_table_name"],
            "mapping_table": entity["main_table_name"],
            "data_source_id": str(entity["data_source_id"]),
            "status": entity.get("status"), "is_deleted": 0,
            "view_status": entity.get("view_status"),
            "update_frequency": entity.get("update_frequency"),
            "semantic_model_attribute_config": attributes,
            "semantic_model_relation_config": relations,
            "bind_metric": bind_metric, "bind_dimension": [],
            "semantic_model_entity_sub_table_mapping": sub_tables,
        }
        payloads[entity["code"]] = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str,
        )
    return payloads


def _apply(cfg: dict[str, Any], source_schema: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    client = _redis_client(cfg)
    client.ping()
    connection = _metadata_connection(cfg)
    metadata_committed = False
    payloads: dict[str, str] = {}
    redis_keys: dict[str, str] = {}
    refreshed_codes: set[str] = set()
    wrong_relation_deactivated = 0
    try:
        connection.begin()
        with connection.cursor() as cursor:
            _, entity_ids = _metadata_preflight(cursor, lock=True)
            for entity_id, (code, _, _) in CODE_PATCHES.items():
                cursor.execute(
                    "UPDATE semantic_model_entity_type SET code=%s,status=1,data_status=0 "
                    "WHERE semantic_model_id=%s AND id=%s AND is_deleted=0",
                    (code, MODEL_ID, entity_id),
                )
            cursor.execute(
                "UPDATE semantic_model_relation_config SET status=0,is_deleted=1 "
                "WHERE semantic_model_id=%s AND id=%s AND source_entity_type_id=%s "
                "AND target_entity_type_id=%s AND source_table_column_name='dealer-dealer_code' "
                "AND target_table_column_name='product-product_code' AND is_deleted=0",
                (MODEL_ID, WRONG_RELATION_ID, DEALER_ID, PRODUCT_ID),
            )
            wrong_relation_deactivated = cursor.rowcount

            entity_names = {
                "dealer": "经销商",
                "product": "商品",
                "sales_order": "销售订单",
                "sales_company": "销售公司",
                "main_data_domain_ent_manufacturer": "生产厂家",
            }
            for entity_id, (code, _, name) in CODE_PATCHES.items():
                entity_ids[code] = entity_id
                entity_names[code] = name
            _upsert_physical_metadata(
                cursor,
                SALES_ORDER_TIME_SPEC,
                source_schema[SALES_ORDER_TIME_SPEC.table],
            )
            for attr in SALES_ORDER_TIME_SPEC.attributes:
                _upsert_attribute(
                    cursor,
                    entity_ids["sales_order"],
                    SALES_ORDER_TIME_SPEC,
                    attr,
                    source_schema[SALES_ORDER_TIME_SPEC.table][attr.code],
                )
            for spec in ENTITY_SPECS:
                entity_ids[spec.code] = _upsert_entity(cursor, spec, entity_ids[spec.code])
                entity_names[spec.code] = spec.name
                _upsert_physical_metadata(cursor, spec, source_schema[spec.table])
                for attr in spec.attributes:
                    _upsert_attribute(cursor, entity_ids[spec.code], spec, attr, source_schema[spec.table][attr.code])
            for relation in RELATION_SPECS:
                _upsert_relation(cursor, relation, entity_ids, entity_names)

            cursor.execute(
                "SELECT code FROM semantic_model_entity_type "
                "WHERE semantic_model_id=%s AND business_domain_id=%s "
                "AND status=1 AND is_deleted=0 AND code IS NOT NULL AND code<>''",
                (MODEL_ID, DOMAIN_ID),
            )
            refreshed_codes = {row["code"] for row in cursor.fetchall()}
            payloads = _build_redis_payloads(cursor, refreshed_codes)
            redis_keys = {code: f"semantic_model:{MODEL_ID}:entity:{code}" for code in payloads}
        connection.commit()
        metadata_committed = True

        # MySQL is authoritative.  Refresh Redis only after the metadata commit;
        # a cache failure is recoverable by rerunning this idempotent migration.
        # Writing Redis first creates an unrecoverable split-brain window when a
        # successful MySQL COMMIT returns an ambiguous network error.
        pipe = client.pipeline(transaction=True)
        for code, payload in payloads.items():
            pipe.set(redis_keys[code], payload)
        # All five previously blank codes are now valid; the old blank-code
        # cache entry is invalid and would otherwise keep colliding in scans.
        pipe.delete(f"semantic_model:{MODEL_ID}:entity:")
        pipe.execute()
    except Exception:
        if not metadata_committed:
            connection.rollback()
        else:
            raise RuntimeError(
                "模型81元数据已提交，但Redis实体缓存刷新失败；"
                "请修复Redis后原样重跑本幂等迁移"
            )
        raise
    finally:
        connection.close()
    return {
        "code_patches": len(CODE_PATCHES),
        "registered_entities": len(ENTITY_SPECS),
        "registered_attributes": sum(len(spec.attributes) for spec in ENTITY_SPECS),
        "patched_existing_attributes": len(SALES_ORDER_TIME_SPEC.attributes),
        "registered_relations": len(RELATION_SPECS),
        "wrong_relation_deactivated": wrong_relation_deactivated,
        "redis_entities_refreshed": len(refreshed_codes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="模型81基础实体、桥接关系和经销商画像的幂等迁移",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="显式执行事务；省略时只做只读预检并输出计划",
    )
    args = parser.parse_args()
    cfg = _config()
    with _metadata_connection(cfg) as connection:
        with connection.cursor() as cursor:
            source, _ = _metadata_preflight(cursor, lock=False)
        connection.rollback()
    source_schema = _source_preflight(source)
    summary: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "semantic_model_id": MODEL_ID,
        "business_domain_id": DOMAIN_ID,
        "data_source_id": DATA_SOURCE_ID,
        "source_database": EXPECTED_SOURCE_DATABASE,
        "planned_code_patches": [value[0] for value in CODE_PATCHES.values()],
        "planned_entities": [spec.code for spec in ENTITY_SPECS],
        "planned_existing_attributes": [
            f"{SALES_ORDER_TIME_SPEC.table}.{attr.code}"
            for attr in SALES_ORDER_TIME_SPEC.attributes
        ],
        "planned_relations": len(RELATION_SPECS),
        "source_tables_validated": sorted(source_schema),
        "skipped_entities": {
            "v_dealer_product_department_match": (
                "视图缺少dealer_code和dept_code稳定键，且聚合查询成本高；"
                "使用规范桥接表避免按名称连接和重复聚合"
            ),
        },
        "company_order_lineage": (
            "复用已验证物理键补齐sales_order→salesperson→sales_company反向遍历；"
            "不新增product.guoyao_code直连，避免与订单实际归属路径形成歧义"
        ),
    }
    if args.apply:
        summary.update(_apply(cfg, source_schema))
    else:
        summary["message"] = "只读预检通过；未修改MySQL、Redis或业务库。使用--apply才会执行。"
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
