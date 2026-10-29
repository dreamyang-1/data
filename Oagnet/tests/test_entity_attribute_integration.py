from __future__ import annotations

import unittest
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from api import app
from prompt_build import PromptBuilder
from vector_store import SearchResult


class FakeSearchStore:
    def __init__(self):
        self.calls = []

    def search(self, vector, top_k, where):
        self.calls.append(where)
        serialized = str(where)
        if "entity_attribute_value" in serialized:
            return [SearchResult(
                id="entity-attr-value:5:9:1",
                score=0.99,
                text="实体名称: 门店\n属性名称: 所在区域\n属性编码: region_code\n属性值: 华东",
                metadata={
                    "type": "entity_attribute_value",
                    "semantic_model_id": 5,
                    "business_domain_id": 9,
                    "entity_name": "门店",
                    "attr_name": "所在区域",
                    "attr_code": "region_code",
                    "attr_value": "华东",
                },
            )]
        return [SearchResult(
            id="placeholder",
            score=0.5,
            text="placeholder",
            metadata={"type": "entity", "entity_name": "占位实体", "semantic_model_id": 5, "business_domain_id": 9},
        )] if "entity" in serialized else []


class EntityAttributeIntegrationTests(unittest.TestCase):
    def setUp(self):
        with api._entity_state_lock:
            api._entity_sync_results.clear()
            api._last_entity_sync.clear()
            api._entity_sync_inflight.clear()

    def test_prompt_retrieves_scoped_attribute_values(self):
        store = FakeSearchStore()
        builder = PromptBuilder(
            store, lambda _: [0.1, 0.2], semantic_model_id=5, business_domain_id=9
        )
        prompt = builder.build("查询华东门店")
        self.assertIn("属性编码: region_code", prompt)
        self.assertIn("属性值: 华东", prompt)
        self.assertTrue(any("entity_attribute_value" in str(where) for where in store.calls))
        self.assertTrue(all("semantic_model_id" in str(where) for where in store.calls))

    def test_api_returns_scope_stats(self):
        @contextmanager
        def lock(*_args, **_kwargs):
            yield

        stats = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "received_rows": 8,
            "indexed_rows": 8,
            "overwritten_rows": 2,
            "deleted_rows": 1,
            "by_attribute": [{
                "entity_code": "product",
                "attr_code": "specification",
                "indexed_rows": 8,
            }],
            "included_attributes": [{
                "entity_code": "product",
                "entity_name": "商品",
                "attr_code": "specification",
                "attr_name": "规格型号",
                "mapping_table": "product",
                "mapping_column": "specification",
                "vectorization": True,
                "is_main_attribute": False,
                "reason": "EXPLICIT_VECTORIZATION",
            }],
            "excluded_attributes": [{
                "entity_code": "sales_order",
                "entity_name": "销售订单",
                "attr_code": "order_key",
                "attr_name": "订单号",
                "mapping_table": "sales_order",
                "mapping_column": "order_key",
                "vectorization": False,
                "is_main_attribute": True,
                "reason": "IDENTIFIER_EXACT_ONLY",
            }],
        }
        with patch("api.mysql_advisory_lock", lock), patch(
            "api.replace_entity_attribute_index", return_value=stats
        ), patch("api.logger.info"):
            response = TestClient(app).post(
                "/vector/entity-attributes/rebuild",
                json={"semantic_model_id": 5, "business_domain_id": 9},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(8, response.json()["indexed_rows"])
        self.assertEqual("specification", response.json()["by_attribute"][0]["attr_code"])
        self.assertEqual(
            "IDENTIFIER_EXACT_ONLY",
            response.json()["excluded_attributes"][0]["reason"],
        )

    def test_api_rejects_invalid_scope_ids(self):
        response = TestClient(app).post(
            "/vector/entity-attributes/rebuild",
            json={"semantic_model_id": 0, "business_domain_id": 9},
        )
        self.assertEqual(422, response.status_code)
        self.assertFalse(response.json()["success"])

    def test_api_empty_source_is_safe_422(self):
        @contextmanager
        def lock(*_args, **_kwargs):
            yield

        with patch("api.mysql_advisory_lock", lock), patch(
            "api.replace_entity_attribute_index",
            side_effect=ValueError("没有实体属性值，已保留旧向量数据"),
        ):
            response = TestClient(app).post(
                "/vector/entity-attributes/rebuild",
                json={"semantic_model_id": 5, "business_domain_id": 9},
            )
        self.assertEqual(422, response.status_code)
        self.assertFalse(response.json()["success"])
        self.assertEqual("ENTITY_ATTRIBUTE_SOURCE_INVALID", response.json()["detail"]["code"])

    def test_sync_waits_for_completion_and_is_idempotent(self):
        payload = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "event_id": "batch-20260824-001",
        }
        stats = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "received_rows": 3,
            "indexed_rows": 3,
            "overwritten_rows": 1,
            "deleted_rows": 0,
        }
        @contextmanager
        def lock(*_args, **_kwargs):
            yield
        with patch("api.mysql_advisory_lock", lock), patch(
            "api.replace_entity_attribute_index", return_value=stats
        ) as replace:
            first = TestClient(app).post("/vector/entity-attributes/sync", json=payload)
            second = TestClient(app).post("/vector/entity-attributes/sync", json=payload)
        self.assertEqual(200, first.status_code)
        self.assertTrue(first.json()["success"])
        self.assertEqual(3, first.json()["indexed_rows"])
        self.assertEqual(first.json(), second.json())
        self.assertEqual(1, replace.call_count)

    def test_sync_empty_source_is_successful_skip(self):
        @contextmanager
        def lock(*_args, **_kwargs):
            yield
        empty_stats = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "status": "SKIPPED",
            "code": "NO_ENTITY_ATTRIBUTES",
            "received_rows": 0,
            "indexed_rows": 0,
            "overwritten_rows": 0,
            "deleted_rows": 0,
        }
        with patch("api.mysql_advisory_lock", lock), patch(
            "api.replace_entity_attribute_index", return_value=empty_stats
        ):
            response = TestClient(app).post(
                "/vector/entity-attributes/sync",
                json={"semantic_model_id": 5, "business_domain_id": 9, "event_id": "event-2"},
            )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["success"])
        self.assertEqual("SKIPPED", response.json()["status"])
        self.assertEqual("NO_ENTITY_ATTRIBUTES", response.json()["code"])
        self.assertEqual(0, response.json()["indexed_rows"])

    def test_concurrent_same_event_shares_one_execution(self):
        payload = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "event_id": "concurrent-event",
        }
        stats = {
            "semantic_model_id": 5,
            "business_domain_id": 9,
            "received_rows": 3,
            "indexed_rows": 3,
            "overwritten_rows": 0,
            "deleted_rows": 0,
        }
        started = threading.Event()
        release = threading.Event()
        responses = []

        @contextmanager
        def lock(*_args, **_kwargs):
            yield

        def replace(*_args, **_kwargs):
            started.set()
            release.wait(3)
            return stats

        def invoke():
            with TestClient(app) as client:
                responses.append(client.post("/vector/entity-attributes/sync", json=payload))

        with patch("api.mysql_advisory_lock", lock), patch(
            "api.replace_entity_attribute_index", side_effect=replace
        ) as replace_call:
            first = threading.Thread(target=invoke)
            second = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(started.wait(2))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(3)
            second.join(3)

        self.assertEqual(2, len(responses))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(1, replace_call.call_count)

if __name__ == "__main__":
    unittest.main()
