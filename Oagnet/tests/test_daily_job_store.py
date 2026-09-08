import json

import pytest

from daily_job_store import DailyJobStoreError, RedisDailyJobStore


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, count, key, job_id, *args):
        if self.values.get(key) != job_id:
            return 0
        if "DEL" in script:
            self.values.pop(key, None)
        return 1


def test_job_state_survives_store_instances_and_lock_is_owned():
    redis = FakeRedis()
    first = RedisDailyJobStore(redis)
    second = RedisDailyJobStore(redis)
    job = {"job_id": "job-1", "status": "QUEUED"}

    assert first.create(job) is True
    assert second.get("job-1") == job
    assert second.create({"job_id": "job-2", "status": "QUEUED"}) is False
    second.release("wrong-owner")
    assert first.active_job_id() == "job-1"
    first.release("job-1")
    assert second.active_job_id() is None


def test_last_success_is_shared():
    redis = FakeRedis()
    store = RedisDailyJobStore(redis)
    result = {"success": True, "rows": 12}
    store.set_last_success(result)
    assert json.dumps(store.last_success(), sort_keys=True) == json.dumps(result, sort_keys=True)


def test_corrupted_redis_payload_is_reported_as_store_error():
    redis = FakeRedis()
    store = RedisDailyJobStore(redis)
    redis.values[store._job_key("broken")] = "not-json"
    redis.values[store.last_success_key] = "[]"

    with pytest.raises(DailyJobStoreError, match="corrupted"):
        store.get("broken")
    with pytest.raises(DailyJobStoreError, match="not an object"):
        store.last_success()
