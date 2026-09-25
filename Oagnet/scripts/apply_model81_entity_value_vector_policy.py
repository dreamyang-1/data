"""Make model-81 entity-value vectorization explicit and auditable.

Runtime indexing no longer treats every main display attribute as searchable.
This migration preserves the intended fuzzy lookup for business names while
keeping the high-cardinality sales-order key on the exact-match path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pymysql.cursors import DictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mysql_tool import _get_connection  # noqa: E402


MODEL_ID = 81
DOMAIN_ID = 205
POLICY = (
    ("main_data_domain_ent_manufacturer", "manufacturer_name", "manufacturer", "manufacturer_name", 1),
    ("salesperson", "salesperson_name", "salesperson", "salesperson_name", 1),
    ("sales_company", "guoyao_name", "guoyao_company", "guoyao_name", 1),
    ("sales_order", "order_key", "sales_order", "order_key", 0),
)


def migrate(*, apply: bool) -> dict:
    connection = _get_connection()
    try:
        connection.begin()
        changes: list[dict] = []
        with connection.cursor(DictCursor) as cursor:
            for entity_code, attr_code, table, column, expected in POLICY:
                cursor.execute(
                    """
                    SELECT e.id AS entity_id, a.id AS attribute_id,
                           a.vectorization, a.mapping_table, a.mapping_column
                    FROM semantic_model_entity_type e
                    JOIN semantic_model_business_domain b
                      ON b.id=e.business_domain_id
                     AND b.semantic_model_id=%s
                     AND COALESCE(b.is_deleted,0)=0
                    JOIN semantic_model_attribute_config a
                      ON a.entity_type_id=e.id
                     AND a.semantic_model_id=%s
                     AND a.code=%s
                     AND COALESCE(a.is_deleted,'0')='0'
                    WHERE e.business_domain_id=%s
                      AND e.code=%s
                      AND COALESCE(e.is_deleted,0)=0
                      AND e.status=1
                    FOR UPDATE
                    """,
                    (MODEL_ID, MODEL_ID, attr_code, DOMAIN_ID, entity_code),
                )
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise RuntimeError(
                        f"active attribute is missing or ambiguous: {entity_code}.{attr_code}"
                    )
                row = rows[0]
                if (row.get("mapping_table"), row.get("mapping_column")) != (table, column):
                    raise RuntimeError(
                        f"physical mapping mismatch: {entity_code}.{attr_code}"
                    )
                current = int(row.get("vectorization") or 0)
                item = {
                    "entity_code": entity_code,
                    "attr_code": attr_code,
                    "mapping": f"{table}.{column}",
                    "from": current,
                    "to": expected,
                    "changed": current != expected,
                }
                changes.append(item)
                if apply and item["changed"]:
                    cursor.execute(
                        """
                        UPDATE semantic_model_attribute_config
                           SET vectorization=%s, update_time=CURRENT_TIMESTAMP
                         WHERE id=%s AND COALESCE(is_deleted,'0')='0'
                        """,
                        (expected, row["attribute_id"]),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"attribute update was not unique: {entity_code}.{attr_code}"
                        )
            if apply:
                connection.commit()
            else:
                connection.rollback()
        return {
            "mode": "APPLY" if apply else "DRY_RUN",
            "semantic_model_id": MODEL_ID,
            "business_domain_id": DOMAIN_ID,
            "changes": changes,
            "changed_count": sum(bool(item["changed"]) for item in changes),
            "requires_entity_value_reindex": any(item["changed"] for item in changes),
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
    print(json.dumps(migrate(apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
