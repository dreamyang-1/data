import json
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import api_server_prod
from data_exporter import format_query_result
from sql_translator_prod import SQLTranslatorProd
from test_sql_translator_hardening import base_ast, translator


class SplitTranslator:
    def translate_only(self, asl, model_id):
        return {
            "success": True,
            "sql": "SELECT 1 AS value",
            "model_id": model_id,
            "error": None,
        }

    def execute_sql_only(self, sql, model_id, data_source_id=None):
        SQLTranslatorProd.validate_read_only_sql(sql)
        return {
            "success": True,
            "sql": sql,
            "columns": ["value"],
            "data": [{"value": 1}],
            "row_count": 1,
            "download_url": None,
        }


class SplitAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = api_server_prod._translator
        api_server_prod._translator = SplitTranslator()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), api_server_prod.APIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        api_server_prod._translator = cls.previous

    def request(self, path, payload):
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_translate_and_execute_are_independent(self):
        status, translated = self.request(
            "/api/translate", {"asl": {"ambiguity": []}, "modelId": "6"}
        )
        self.assertEqual(200, status)
        self.assertTrue(translated["success"])
        self.assertEqual("SELECT 1 AS value", translated["sql"])
        self.assertIn("dataSourceId", translated)

        status, executed = self.request(
            "/api/execute", {"sql": translated["sql"], "modelId": "6"}
        )
        self.assertEqual(200, status)
        self.assertTrue(executed["success"])
        self.assertEqual(1, executed["row_count"])

    def test_route_matching_is_exact(self):
        status, _ = self.request(
            "/bad/api/execute-extra", {"sql": "SELECT 1", "modelId": "6"}
        )
        self.assertEqual(404, status)

    def test_execute_rejects_invalid_model_id_before_data_source_lookup(self):
        status, payload = self.request(
            "/api/execute", {"sql": "SELECT 1", "modelId": 0}
        )
        self.assertEqual(400, status)
        self.assertFalse(payload["success"])
        self.assertEqual("INVALID_REQUEST", payload["code"])

    def test_execute_rejects_invalid_optional_data_source_id(self):
        status, payload = self.request(
            "/api/execute",
            {"sql": "SELECT 1", "modelId": 6, "dataSourceId": -1},
        )
        self.assertEqual(400, status)
        self.assertFalse(payload["success"])
        self.assertEqual("INVALID_REQUEST", payload["code"])

    def test_read_only_guard_rejects_dangerous_sql(self):
        for sql in (
            "DELETE FROM orders",
            "SELECT 1; DROP TABLE orders",
            "SELECT * FROM orders INTO OUTFILE '/tmp/orders.csv'",
            "SELECT GET_LOCK('agent', 60)",
        ):
            with self.assertRaises(ValueError, msg=sql):
                SQLTranslatorProd.validate_read_only_sql(sql)

    def test_detail_projection_does_not_require_fake_metric_or_group_rows(self):
        ast = base_ast(
            metrics=[],
            dimensions=[
                {"name": "order_info.order_id"},
                {"name": "order_info.pay_amount"},
            ],
            limit=None,
        )
        sql = translator().translate(json.dumps(ast), "6")
        self.assertIn("order_info.order_id", sql)
        self.assertIn("order_info.pay_amount", sql)
        self.assertNotIn("GROUP BY", sql)
        self.assertTrue(sql.endswith("LIMIT 10000"))

    def test_large_result_keeps_first_twenty_rows_with_download_url(self):
        class FakeExporter:
            @staticmethod
            def export_to_excel(columns, data):
                self.assertEqual(["value"], columns)
                self.assertEqual(205, len(data))
                return "http://files.example/result.xlsx"

        rows = [{"value": index} for index in range(205)]
        with patch("data_exporter.get_exporter", return_value=FakeExporter()):
            result = format_query_result(["value"], rows, len(rows), "SELECT value")

        self.assertTrue(result["success"])
        self.assertEqual(205, result["row_count"])
        self.assertEqual(["value"], result["columns"])
        self.assertEqual(rows[:20], result["data"])
        self.assertEqual(20, result["preview_count"])
        self.assertTrue(result["preview_truncated"])
        self.assertEqual("http://files.example/result.xlsx", result["download_url"])


if __name__ == "__main__":
    unittest.main()
