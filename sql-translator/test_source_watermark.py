import unittest
from datetime import datetime
from unittest.mock import patch

from sql_translator_prod import SQLTranslatorProd


def time_dimension(field: str, *, fields: list[str] | None = None) -> dict:
    mappings = fields if fields is not None else [field]
    return {
        "dim_code": "transaction_date",
        "dim_type": "时间维度",
        "field_mapping": {"fact_table_field": field},
        "attribute_mappings": [{"field_path": item} for item in mappings],
    }


class FakeLoader:
    def __init__(self, dimensions):
        self.dimensions = dimensions
        self.requested_model_id = None

    def iter_scoped_dimensions(self, model_id):
        self.requested_model_id = model_id
        return self.dimensions


class WatermarkCursor:
    def __init__(self):
        self.description = None
        self.executed = []
        self.last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.last_sql = sql
        self.executed.append(sql)
        if sql.startswith("SELECT order_id"):
            self.description = [("order_id",), ("amount",)]

    def fetchone(self):
        if self.last_sql == "SELECT UTC_TIMESTAMP(6) AS data_as_of":
            return {"data_as_of": datetime(2026, 8, 28, 1, 2, 3, 4)}
        if self.last_sql.startswith("SELECT MAX("):
            return {"source_data_as_of": datetime(2025, 12, 30, 23, 59, 58)}
        return {}

    def fetchall(self):
        return [{"order_id": "O-1", "amount": 10}]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class SourceWatermarkTests(unittest.TestCase):
    @staticmethod
    def config():
        return {
            "id": 58,
            "semantic_model_id": 81,
            "db_type": "mysql",
            "host": "db",
            "port": 3306,
            "username": "u",
            "password": "p",
            "db_name": "drug_sales",
        }

    def translator_with(self, dimensions):
        translator = SQLTranslatorProd()
        translator.loader = FakeLoader(dimensions)
        return translator

    def test_unique_registered_time_field_is_selected_for_involved_table(self):
        translator = self.translator_with([
            time_dimension("sales_order.created_date"),
            time_dimension("dealer.created_date"),
        ])

        field = translator._resolve_source_watermark_field(
            "SELECT s.order_id FROM sales_order s JOIN dealer d "
            "ON s.dealer_code = d.dealer_code",
            81,
        )

        # Both involved tables expose time fields, so the safe outcome is
        # ambiguity rather than selecting one by order.
        self.assertIsNone(field)
        self.assertEqual("81", translator.loader.requested_model_id)

    def test_one_involved_registered_time_field_is_selected(self):
        translator = self.translator_with([
            time_dimension("sales_order.created_date"),
            time_dimension("dealer.created_date"),
        ])
        self.assertEqual(
            "sales_order.created_date",
            translator._resolve_source_watermark_field(
                "SELECT order_id FROM sales_order", "81"
            ),
        )

    def test_no_time_field_and_composite_time_dimension_return_none(self):
        translator = self.translator_with([])
        self.assertIsNone(translator._resolve_source_watermark_field(
            "SELECT order_id FROM sales_order", 81
        ))

        translator = self.translator_with([
            time_dimension(
                "sales_order.fiscal_year",
                fields=["sales_order.fiscal_year", "sales_order.fiscal_month"],
            )
        ])
        self.assertIsNone(translator._resolve_source_watermark_field(
            "SELECT order_id FROM sales_order", 81
        ))

    def test_unsafe_metadata_field_is_never_selected(self):
        translator = self.translator_with([
            time_dimension("sales_order.created_date;DROP_TABLE")
        ])
        self.assertIsNone(translator._resolve_source_watermark_field(
            "SELECT order_id FROM sales_order", 81
        ))

    def test_watermark_is_queried_inside_snapshot_and_returned(self):
        cursor = WatermarkCursor()
        connection = FakeConnection(cursor)
        with patch("sql_translator_prod.pymysql.connect", return_value=connection):
            result = SQLTranslatorProd.execute_sql_on_data_source(
                "SELECT order_id, amount FROM sales_order",
                self.config(),
                "sales_order.created_date",
            )

        self.assertTrue(result["success"])
        self.assertEqual("2025-12-30T23:59:58.000000", result["source_data_as_of"])
        self.assertEqual("sales_order.created_date", result["source_watermark_field"])
        self.assertEqual(
            "SELECT MAX(`sales_order`.`created_date`) AS source_data_as_of "
            "FROM `sales_order`",
            cursor.executed[-1],
        )
        self.assertLess(
            cursor.executed.index("START TRANSACTION WITH CONSISTENT SNAPSHOT"),
            len(cursor.executed) - 1,
        )

    def test_unsafe_runtime_identifier_never_executes_watermark_sql(self):
        cursor = WatermarkCursor()
        connection = FakeConnection(cursor)
        with patch("sql_translator_prod.pymysql.connect", return_value=connection):
            result = SQLTranslatorProd.execute_sql_on_data_source(
                "SELECT order_id, amount FROM sales_order",
                self.config(),
                "sales_order.created_date` FROM secrets;--",
            )

        self.assertTrue(result["success"])
        self.assertNotIn("source_data_as_of", result)
        self.assertFalse(any(sql.startswith("SELECT MAX(") for sql in cursor.executed))


if __name__ == "__main__":
    unittest.main()
