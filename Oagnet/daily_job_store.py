"""Redis-backed state for daily vector synchronization jobs."""
from __future__ import annotations

import json
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from config import (
    DAILY_JOB_LEASE_SECONDS,
    DAILY_JOB_TTL_SECONDS,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_SOCKET_TIMEOUT,
    require_runtime_secret,
)


class DailyJobStoreError(RuntimeError):
    pass


class RedisDailyJobStore:
    prefix = "oagnet:daily-vector"

    def __init__(self, client: Redis | None = None):
        self._client = client

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = Redis(
                host=require_runtime_secret(REDIS_HOST, "REDIS_HOST"),
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD or None,
                decode_responses=True,
                socket_connect_timeout=REDIS_SOCKET_TIMEOUT,
                socket_timeout=REDIS_SOCKET_TIMEOUT,
                health_check_interval=30,
            )
        return self._client

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    @property
    def active_key(self) -> str:
        return f"{self.prefix}:active"

    @property
    def last_success_key(self) -> str:
        return f"{self.prefix}:last-success"

    def _call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (RedisError, OSError, RuntimeError) as exc:
            raise DailyJobStoreError("Redis daily-job state is unavailable") from exc

    def create(self, job: dict[str, Any]) -> bool:
        job_id = str(job["job_id"])
        acquired = self._call(
            self.client.set,
            self.active_key,
            job_id,
            nx=True,
            ex=DAILY_JOB_LEASE_SECONDS,
        )
        if not acquired:
            return False
        try:
            self.save(job)
        except Exception:
            self.release(job_id)
            raise
        return True

    def save(self, job: dict[str, Any]) -> None:
        try:
            payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise DailyJobStoreError("Daily-job state is not JSON serializable") from exc
        self._call(
            self.client.setex,
            self._job_key(str(job["job_id"])),
            DAILY_JOB_TTL_SECONDS,
            payload,
        )

    def get(self, job_id: str) -> dict[str, Any] | None:
        payload = self._call(self.client.get, self._job_key(job_id))
        if not payload:
            return None
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DailyJobStoreError("Redis daily-job state is corrupted") from exc
        if not isinstance(value, dict):
            raise DailyJobStoreError("Redis daily-job state is not an object")
        return value

    def active_job_id(self) -> str | None:
        return self._call(self.client.get, self.active_key)

    def renew(self, job_id: str) -> bool:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        return bool(
            self._call(
                self.client.eval,
                script,
                1,
                self.active_key,
                job_id,
                DAILY_JOB_LEASE_SECONDS,
            )
        )

    def release(self, job_id: str) -> None:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        self._call(self.client.eval, script, 1, self.active_key, job_id)

    def set_last_success(self, result: dict[str, Any]) -> None:
        try:
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise DailyJobStoreError("Last-success state is not JSON serializable") from exc
        self._call(
            self.client.set,
            self.last_success_key,
            payload,
        )

    def last_success(self) -> dict[str, Any] | None:
        payload = self._call(self.client.get, self.last_success_key)
        if not payload:
            return None
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DailyJobStoreError("Redis last-success state is corrupted") from exc
        if not isinstance(value, dict):
            raise DailyJobStoreError("Redis last-success state is not an object")
        return value


daily_job_store = RedisDailyJobStore()
