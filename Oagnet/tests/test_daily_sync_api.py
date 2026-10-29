from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from daily_job_store import DailyJobStoreError


class FakeDailyJobStore:
    def __init__(self):
        self.jobs = {}
        self.active = None
        self.latest = None
        self.create_count = 0

    def create(self, job):
        self.create_count += 1
        if self.active is not None:
            return False
        self.active = job["job_id"]
        self.jobs[job["job_id"]] = dict(job)
        return True

    def get(self, job_id):
        return self.jobs.get(job_id)

    def active_job_id(self):
        return self.active

    def last_success(self):
        return self.latest


class DailySyncApiTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeDailyJobStore()
        self.store_patch = patch.object(api, "daily_job_store", self.store)
        self.store_patch.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.client.close()
        self.store_patch.stop()

    def test_accepts_dynamic_source_configuration(self):
        payload = {
            "source_table": "entity_attribute_value",
            "id_field": "id",
            "text_fields": ["entity_name", "attribute_value"],
            "metadata_fields": ["semantic_model_id", "business_domain_id"],
        }
        with patch.object(api, "_process_daily_table_sync_job"):
            response = self.client.post("/vector/daily-table/sync", json=payload)

        self.assertEqual(202, response.status_code)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(payload["source_table"], body["source_table"])
        self.assertEqual(
            payload,
            self.store.jobs[body["job_id"]]["source_config"],
        )

        status_response = self.client.get(f"/vector/daily-table/jobs/{body['job_id']}")
        self.assertEqual(200, status_response.status_code)
        self.assertEqual("QUEUED", status_response.json()["status"])

    def test_rejects_unsafe_table_name(self):
        response = self.client.post(
            "/vector/daily-table/sync",
            json={
                "source_table": "users;DROP_TABLE",
                "id_field": "id",
                "text_fields": ["content"],
            },
        )
        self.assertEqual(422, response.status_code)

    def test_requires_at_least_one_text_field(self):
        response = self.client.post(
            "/vector/daily-table/sync",
            json={
                "source_table": "daily_source",
                "id_field": "id",
                "text_fields": [],
            },
        )
        self.assertEqual(422, response.status_code)

    def test_event_id_makes_retries_idempotent(self):
        payload = {
            "source_table": "daily_source",
            "id_field": "id",
            "text_fields": ["content"],
            "event_id": "batch-20260826-001",
        }
        with patch.object(api, "_process_daily_table_sync_job"):
            first = self.client.post("/vector/daily-table/sync", json=payload)
            second = self.client.post("/vector/daily-table/sync", json=payload)

        self.assertEqual(202, first.status_code)
        self.assertEqual(202, second.status_code)
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        self.assertEqual(1, self.store.create_count)

    def test_same_event_id_with_changed_payload_is_rejected(self):
        first_payload = {
            "source_table": "daily_source",
            "id_field": "id",
            "text_fields": ["content"],
            "event_id": "batch-conflict",
        }
        with patch.object(api, "_process_daily_table_sync_job"):
            first = self.client.post("/vector/daily-table/sync", json=first_payload)
            changed = self.client.post(
                "/vector/daily-table/sync",
                json={**first_payload, "text_fields": ["title"]},
            )

        self.assertEqual(202, first.status_code)
        self.assertEqual(409, changed.status_code)
        self.assertEqual(
            "DAILY_VECTOR_SYNC_IDEMPOTENCY_CONFLICT",
            changed.json()["detail"]["code"],
        )

    def test_job_status_maps_active_redis_failure_to_503(self):
        self.store.jobs["job-1"] = {
            "job_id": "job-1",
            "status": "RUNNING",
            "source_table": "daily_source",
            "event_id": None,
            "accepted_at": "2026-08-26T00:00:00+08:00",
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
        }
        with patch.object(
            self.store,
            "active_job_id",
            side_effect=DailyJobStoreError("redis unavailable"),
        ):
            response = self.client.get("/vector/daily-table/jobs/job-1")

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "DAILY_JOB_STORE_UNAVAILABLE",
            response.json()["detail"]["code"],
        )


if __name__ == "__main__":
    unittest.main()
