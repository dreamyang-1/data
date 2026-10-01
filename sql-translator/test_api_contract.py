import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import api_server_prod


class FakeCatalog:
    def resolve_metrics(self, model_id, names):
        return {
            "status": "RESOLVED",
            "semantic_model_id": int(model_id),
            "metrics": [{
                "input": names[0], "metric_id": "6:sales", "version": "current",
                "canonical_name": "销售额", "unit": "元",
            }],
            "unresolved": [], "ambiguities": [],
        }

    def definition(self, metric_id, version):
        return {"metric_id": metric_id, "version": version, "metric_code": "sales"}

    def lineage(self, metric_id, version):
        return {"metric_id": metric_id, "version": version, "nodes": [], "edges": []}


class FakeLoader:
    def clear_cache(self, model_id=None):
        return {"entity": 0, "metric": 0, "dimension": 0}


class FakeTranslator:
    catalog = FakeCatalog()
    loader = FakeLoader()

    def execute_query(self, asl, model_id):
        return {
            "success": True, "sql": "SELECT 1 LIMIT 1", "data": [{"value": 1}],
            "columns": ["value"], "row_count": 1, "error": None,
            "error_code": None, "retryable": False,
        }


class APIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = api_server_prod._translator
        api_server_prod._translator = FakeTranslator()
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

    def request(self, path, payload=None, method=None, request_id="test-request"):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self.base + path,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Content-Type": "application/json", "X-Request-Id": request_id},
        )
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_alias(self):
        status, body = self.request("/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_openapi_advertises_all_data_agent_routes(self):
        status, body = self.request("/openapi.json")
        self.assertEqual(200, status)
        self.assertTrue({
            "/api/ast-to-sql",
            "/v1/semantic/metrics/resolve",
            "/v1/semantic/metrics/{metric_id}/versions/{version}",
            "/v1/semantic/metrics/{metric_id}/lineage",
        }.issubset(body["paths"]))

    def test_resolve_definition_and_lineage_contracts(self):
        status, resolved = self.request(
            "/v1/semantic/metrics/resolve",
            {"semantic_model_id": 6, "metric_names": ["销售额"]},
        )
        self.assertEqual(200, status)
        self.assertEqual("RESOLVED", resolved["status"])
        self.assertEqual("test-request", resolved["request_id"])

        status, definition = self.request(
            "/v1/semantic/metrics/6%3Asales/versions/current"
        )
        self.assertEqual(200, status)
        self.assertEqual("6:sales", definition["metric_id"])

        status, lineage = self.request(
            "/v1/semantic/metrics/6%3Asales/lineage", {"version": "current"}
        )
        self.assertEqual(200, status)
        self.assertIn("nodes", lineage)

    def test_ast_endpoint_keeps_legacy_double_encoded_result(self):
        asl = {"metrics": [{"name": "sales"}], "ambiguity": []}
        status, body = self.request(
            "/api/ast-to-sql", {"asl": asl, "modelId": 6}
        )
        self.assertEqual(200, status)
        self.assertTrue(body["success"])
        self.assertEqual("SELECT 1 LIMIT 1", json.loads(body["result"])["sql"])

    def test_fractional_model_id_is_rejected_as_bad_request(self):
        status, body = self.request(
            "/api/ast-to-sql",
            {"asl": {"metrics": [{"name": "sales"}]}, "modelId": 6.5},
        )
        self.assertEqual(400, status)
        self.assertEqual("INVALID_REQUEST", body["code"])

    def test_route_matching_is_exact(self):
        status, body = self.request(
            "/bad/api/ast-to-sql-extra", {"asl": {}, "modelId": 6}
        )
        self.assertEqual(404, status)
        self.assertEqual("NOT_FOUND", body["code"])
        self.assertFalse(body["retryable"])

    def test_unsafe_request_id_is_not_reflected(self):
        req = Request(
            self.base + "/health",
            headers={"X-Request-Id": "forged request id"},
        )
        with urlopen(req, timeout=3) as response:
            self.assertEqual(200, response.status)
            self.assertNotEqual(
                "forged request id", response.headers.get("X-Request-Id")
            )


if __name__ == "__main__":
    unittest.main()
