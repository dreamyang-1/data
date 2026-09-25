import unittest
from datetime import datetime
from unittest.mock import patch

from sql_translator_prod import SQLTranslatorProd


class FakeCursor:
    def __init__(self, *, snapshot_supported=True):
        self.snapshot_supported = snapshot_supported
        self.description = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if sql == "SET TRANSACTION READ ONLY" and not self.snapshot_supported:
            raise RuntimeError("consistent snapshots unavailable")
        if sql.startswith("SELECT order_id"):
            self.description = [("order_id",), ("amount",)]

    def fetchone(self):
        return {"data_as_of": datetime(2026, 8, 26, 12, 0, 0, 123456)}

    def fetchall(self):
        return [{"order_id": "O-1", "amount": 10}]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class ExecutionEvidenceTests(unittest.TestCase):
    def config(self):
        return {
            "id": 5, "semantic_model_id": 6, "db_type": "mysql",
            "host": "db", "port": 3306, "username": "u",
            "password": "p", "db_name": "sales",
        }

    def test_consistent_read_emits_verifiable_snapshot_evidence(self):
        connection = FakeConnection(FakeCursor())
        with patch("sql_translator_prod.pymysql.connect", return_value=connection):
            result = SQLTranslatorProd.execute_sql_on_data_source(
                "SELECT order_id, amount FROM orders", self.config()
            )

        self.assertTrue(result["success"])
        self.assertEqual("PASS", result["quality_status"])
        self.assertTrue(result["snapshot_id"].startswith("mysql-consistent:"))
        self.assertEqual("2026-08-26T12:00:00.123456Z", result["data_as_of"])
        self.assertTrue(result["quality_checks"]["read_only_transaction"])

    def test_unsupported_snapshot_never_claims_pass(self):
        connection = FakeConnection(FakeCursor(snapshot_supported=False))
        with patch("sql_translator_prod.pymysql.connect", return_value=connection):
            result = SQLTranslatorProd.execute_sql_on_data_source(
                "SELECT order_id, amount FROM orders", self.config()
            )

        self.assertTrue(result["success"])
        self.assertNotIn("quality_status", result)
        self.assertNotIn("snapshot_id", result)
        self.assertTrue(connection.rolled_back)


if __name__ == "__main__":
    unittest.main()
