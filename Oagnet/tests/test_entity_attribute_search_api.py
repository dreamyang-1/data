import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app
from vector_store import SearchResult


class EntityAttributeSearchApiTest(unittest.TestCase):
    def test_search_is_scoped_and_returns_metadata(self):
        result = SearchResult(
            id="entity-attr-value:5:9:1",
            score=0.93,
            text="属性值: 北京区域",
            metadata={
                "type": "entity_attribute_value",
                "semantic_model_id": 5,
                "business_domain_id": 9,
                "entity_name": "销售区域",
                "entity_alias": '["区域"]',
                "attr_name": "地区名称",
                "attr_code": "region_name",
                "attr_value": "北京区域",
            },
        )
        with patch("api.embed_query", return_value=[0.1, 0.2]), patch.object(
            __import__("api")._store, "search", return_value=[result]
        ) as search:
            response = TestClient(app).post(
                "/vector/entity-attributes/search",
                json={
                    "query": "北经区域",
                    "semantic_model_id": 5,
                    "business_domain_id": 9,
                    "top_k": 5,
                    "score_threshold": 0.8,
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["matches"][0]["attribute_value"], "北京区域")
        self.assertEqual(body["matches"][0]["entity_alias"], ["区域"])
        where = search.call_args.kwargs["where"]
        self.assertIn({"semantic_model_id": 5}, where["$and"])
        self.assertIn({"business_domain_id": 9}, where["$and"])

    def test_scope_is_required(self):
        response = TestClient(app).post(
            "/vector/entity-attributes/search", json={"query": "北京"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()["success"])


if __name__ == "__main__":
    unittest.main()
