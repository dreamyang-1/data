"""Make model-81 product-category value semantics auditable and retrievable."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pymysql.cursors import DictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mysql_tool import _get_connection  # noqa: E402


MODEL_ID = 81
DOMAIN_ID = 205
ENTITY_CODE = "product_category"
ATTRIBUTE_CODE = "product_type"
ATTRIBUTE_FIELD = ("product_category", "product_type")
REQUIRED_ENTITY_ALIASES = ("商品分类", "产品分类", "品类", "耗材分类")
DESCRIPTION_MARKER = "[catalog_semantics:v1]"
ATTRIBUTE_DESCRIPTION = (
    f"{DESCRIPTION_MARKER} 目录中的产品类别、商品类型或耗材类型；"
    "用户给出的分类值应作为独立目录条件，不能拼入商品名称。"
)


def _alias_terms(value: str | None) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        decoded = None
    values = (
        [str(item).strip() for item in decoded]
        if isinstance(decoded, list)
        else re.split(r"[,，、;；|\s]+", raw)
    )
    return list(dict.fromkeys(item for item in values if item))


def migrate(*, apply: bool) -> dict:
    connection = _get_connection()
    try:
        connection.begin()
        with connection.cursor(DictCursor) as cursor:
            cursor.execute(
                """
                SELECT e.id, e.alias
                FROM semantic_model_entity_type e
                JOIN semantic_model_business_domain b
                  ON b.id=e.business_domain_id
                 AND b.semantic_model_id=%s
                 AND COALESCE(b.is_deleted,0)=0
                WHERE e.business_domain_id=%s AND e.code=%s
                  AND COALESCE(e.is_deleted,0)=0 AND e.status=1
                FOR UPDATE
                """,
                (MODEL_ID, DOMAIN_ID, ENTITY_CODE),
            )
            entities = cursor.fetchall()
            if len(entities) != 1:
                raise RuntimeError("商品分类实体不存在或不唯一")
            entity = entities[0]
            cursor.execute(
                """
                SELECT id, description, mapping_table, mapping_column
                FROM semantic_model_attribute_config
                WHERE semantic_model_id=%s AND entity_type_id=%s AND code=%s
                  AND COALESCE(is_deleted,'0')='0'
                FOR UPDATE
                """,
                (MODEL_ID, entity["id"], ATTRIBUTE_CODE),
            )
            attributes = cursor.fetchall()
            if len(attributes) != 1:
                raise RuntimeError("商品类型属性不存在或不唯一")
            attribute = attributes[0]
            if (
                attribute.get("mapping_table"),
                attribute.get("mapping_column"),
            ) != ATTRIBUTE_FIELD:
                raise RuntimeError("商品类型属性物理映射与预期不一致")

            aliases = _alias_terms(entity.get("alias"))
            merged_aliases = [
                *aliases,
                *(item for item in REQUIRED_ENTITY_ALIASES if item not in aliases),
            ]
            current_description = str(attribute.get("description") or "").strip()
            description = current_description
            if DESCRIPTION_MARKER not in current_description:
                description = "；".join(filter(None, (
                    current_description,
                    ATTRIBUTE_DESCRIPTION,
                )))
            changes = {
                "entity_alias": merged_aliases != aliases,
                "attribute_description": description != current_description,
            }
            if apply:
                if changes["entity_alias"]:
                    cursor.execute(
                        "UPDATE semantic_model_entity_type "
                        "SET alias=%s,update_time=CURRENT_TIMESTAMP WHERE id=%s",
                        ("、".join(merged_aliases), entity["id"]),
                    )
                if changes["attribute_description"]:
                    cursor.execute(
                        "UPDATE semantic_model_attribute_config "
                        "SET description=%s,update_time=CURRENT_TIMESTAMP WHERE id=%s",
                        (description, attribute["id"]),
                    )
                connection.commit()
            else:
                connection.rollback()
            return {
                "mode": "APPLY" if apply else "DRY_RUN",
                "semantic_model_id": MODEL_ID,
                "business_domain_id": DOMAIN_ID,
                "entity_code": ENTITY_CODE,
                "attribute_code": ATTRIBUTE_CODE,
                "field": ".".join(ATTRIBUTE_FIELD),
                "changes": changes,
                "requires_vector_reindex": any(changes.values()),
            }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
