from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryType(str, Enum):
    """Only durable user choices/context belong here, never query result rows."""

    USER_PREFERENCE = "user_preference"
    METRIC_ALIAS = "metric_alias"
    DEFAULT_FILTER = "default_filter"
    DISPLAY_PREFERENCE = "display_preference"
    BUSINESS_CONTEXT = "business_context"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryScope(BaseModel):
    """The mandatory isolation boundary for every read and write."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=100)


class MemoryCandidate(BaseModel):
    """An unconfirmed durable-memory proposal extracted from one user message."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scope: MemoryScope
    memory_type: MemoryType
    memory_key: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    value: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    source_session_id: str = Field(min_length=1, max_length=128)
    source_message_id: str = Field(min_length=1, max_length=128)
    created_by: str | None = Field(default=None, max_length=128)
    valid_from: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None

    @field_validator("summary", mode="after")
    @classmethod
    def ensure_safe_summary(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("summary must be a single safe display value")
        return value

    @field_validator("valid_from", "expires_at", mode="after")
    @classmethod
    def ensure_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory validity timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_validity_window(self) -> "MemoryCandidate":
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be later than valid_from")
        forbidden_keys = {
            "sql", "rows", "result_rows", "query_result", "raw_question",
            "chat_history", "conversation_history", "prompt", "messages",
            "chain_of_thought", "reasoning", "api_key", "password", "token",
        }
        nested_keys = {
            key.casefold()
            for key in _walk_mapping_keys(self.value)
        }
        if forbidden_keys.intersection(nested_keys):
            raise ValueError("sensitive, raw or transient data cannot be long-term memory")
        try:
            encoded = json.dumps(self.value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("memory value must be a JSON object") from exc
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("memory value must not exceed 4096 UTF-8 bytes")
        if not _allowed_memory_key(self.memory_key):
            raise ValueError("memory_key is not in the governed preference schema")
        if not _memory_type_matches_key(self.memory_type, self.memory_key):
            raise ValueError("memory_type does not match the governed memory_key")
        if not _valid_memory_value(self.memory_key, self.value):
            raise ValueError("memory value does not match its governed memory_key")
        return self


class LongTermMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    memory_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    scope: MemoryScope
    memory_type: MemoryType
    memory_key: str | None = None
    summary: str
    value: dict[str, Any]
    status: MemoryStatus
    confidence: float = Field(ge=0.0, le=1.0)
    source_session_id: str
    source_message_id: str
    created_by: str | None = None
    confirmed_by: str | None = None
    deleted_by: str | None = None
    valid_from: datetime
    expires_at: datetime | None = None
    superseded_by: str | None = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
    deleted_at: datetime | None = None
    version: int = Field(ge=1)


class MemoryNotFoundError(LookupError):
    pass


class InvalidMemoryTransitionError(RuntimeError):
    pass


class MemoryConcurrencyError(RuntimeError):
    pass


class LongTermMemoryStore(Protocol):
    async def healthcheck(self) -> bool: ...

    async def create_candidate(self, candidate: MemoryCandidate) -> LongTermMemory: ...

    async def get(
        self, scope: MemoryScope, memory_id: str, *, include_deleted: bool = False
    ) -> LongTermMemory | None: ...

    async def list_memories(
        self,
        scope: MemoryScope,
        *,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LongTermMemory]: ...

    async def list_active(
        self,
        scope: MemoryScope,
        *,
        memory_types: Sequence[MemoryType] | None = None,
        at: datetime | None = None,
        limit: int = 100,
    ) -> list[LongTermMemory]: ...

    async def confirm(
        self, scope: MemoryScope, memory_id: str, *, confirmed_by: str
    ) -> LongTermMemory: ...

    async def soft_delete(
        self, scope: MemoryScope, memory_id: str, *, deleted_by: str
    ) -> LongTermMemory: ...


class InMemoryLongTermMemoryStore:
    """Concurrency-safe reference implementation for tests and local development."""

    def __init__(self) -> None:
        self._records: dict[str, LongTermMemory] = {}
        self._lock = asyncio.Lock()

    async def healthcheck(self) -> bool:
        return True

    @staticmethod
    def _same_scope(memory: LongTermMemory, scope: MemoryScope) -> bool:
        return memory.scope == scope

    @staticmethod
    def _visible_at(memory: LongTermMemory, at: datetime) -> bool:
        return memory.valid_from <= at and (
            memory.expires_at is None or memory.expires_at > at
        )

    async def create_candidate(self, candidate: MemoryCandidate) -> LongTermMemory:
        now = utc_now()
        record = LongTermMemory(
            memory_id=uuid.uuid4().hex,
            scope=candidate.scope,
            memory_type=candidate.memory_type,
            memory_key=candidate.memory_key,
            summary=candidate.summary,
            value=candidate.value,
            status=MemoryStatus.CANDIDATE,
            confidence=candidate.confidence,
            source_session_id=candidate.source_session_id,
            source_message_id=candidate.source_message_id,
            created_by=candidate.created_by,
            valid_from=candidate.valid_from,
            expires_at=candidate.expires_at,
            created_at=now,
            updated_at=now,
            version=1,
        )
        async with self._lock:
            dedupe_key = _candidate_dedupe_key(candidate)
            for existing in self._records.values():
                if _memory_dedupe_key(existing) == dedupe_key:
                    if not _candidate_matches_memory(candidate, existing):
                        raise InvalidMemoryTransitionError(
                            "source message and memory_key were reused with a different payload"
                        )
                    return existing.model_copy(deep=True)
            self._records[record.memory_id] = record.model_copy(deep=True)
        return record.model_copy(deep=True)

    async def get(
        self, scope: MemoryScope, memory_id: str, *, include_deleted: bool = False
    ) -> LongTermMemory | None:
        async with self._lock:
            record = self._records.get(memory_id)
            if record is None or not self._same_scope(record, scope):
                return None
            if record.status == MemoryStatus.DELETED and not include_deleted:
                return None
            return record.model_copy(deep=True)

    async def list_memories(
        self,
        scope: MemoryScope,
        *,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LongTermMemory]:
        status_set = set(statuses) if statuses is not None else None
        safe_limit = _validated_limit(limit)
        safe_offset = max(0, int(offset))
        async with self._lock:
            records = [
                item
                for item in self._records.values()
                if self._same_scope(item, scope)
                and (status_set is None or item.status in status_set)
            ]
            records.sort(key=lambda item: (item.updated_at, item.memory_id), reverse=True)
            return [
                item.model_copy(deep=True)
                for item in records[safe_offset : safe_offset + safe_limit]
            ]

    async def list_active(
        self,
        scope: MemoryScope,
        *,
        memory_types: Sequence[MemoryType] | None = None,
        at: datetime | None = None,
        limit: int = 100,
    ) -> list[LongTermMemory]:
        requested_types = set(memory_types) if memory_types is not None else None
        effective_at = _as_utc(at or utc_now())
        safe_limit = _validated_limit(limit)
        async with self._lock:
            records = [
                item
                for item in self._records.values()
                if self._same_scope(item, scope)
                and item.status == MemoryStatus.ACTIVE
                and self._visible_at(item, effective_at)
                and (requested_types is None or item.memory_type in requested_types)
            ]
            records.sort(key=lambda item: (item.updated_at, item.memory_id), reverse=True)
            return [item.model_copy(deep=True) for item in records[:safe_limit]]

    async def confirm(
        self, scope: MemoryScope, memory_id: str, *, confirmed_by: str
    ) -> LongTermMemory:
        actor = _validated_actor(confirmed_by)
        now = utc_now()
        async with self._lock:
            record = self._records.get(memory_id)
            if record is None or not self._same_scope(record, scope):
                raise MemoryNotFoundError("memory not found")
            if record.status == MemoryStatus.ACTIVE:
                return record.model_copy(deep=True)
            if record.status != MemoryStatus.CANDIDATE:
                raise InvalidMemoryTransitionError(
                    f"cannot confirm memory in status {record.status.value}"
                )
            if record.expires_at is not None and record.expires_at <= now:
                raise InvalidMemoryTransitionError("cannot confirm an expired memory candidate")

            if record.memory_key:
                for old_id, old in tuple(self._records.items()):
                    if (
                        old_id != memory_id
                        and old.scope == scope
                        and old.status == MemoryStatus.ACTIVE
                        and old.memory_type == record.memory_type
                        and old.memory_key == record.memory_key
                    ):
                        self._records[old_id] = old.model_copy(
                            update={
                                "status": MemoryStatus.SUPERSEDED,
                                "superseded_by": memory_id,
                                "updated_at": now,
                                "version": old.version + 1,
                            },
                            deep=True,
                        )

            confirmed = record.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "confirmed_by": actor,
                    "confirmed_at": now,
                    "updated_at": now,
                    "version": record.version + 1,
                },
                deep=True,
            )
            self._records[memory_id] = confirmed
            return confirmed.model_copy(deep=True)

    async def soft_delete(
        self, scope: MemoryScope, memory_id: str, *, deleted_by: str
    ) -> LongTermMemory:
        actor = _validated_actor(deleted_by)
        now = utc_now()
        async with self._lock:
            record = self._records.get(memory_id)
            if record is None or not self._same_scope(record, scope):
                raise MemoryNotFoundError("memory not found")
            if record.status == MemoryStatus.DELETED:
                return record.model_copy(deep=True)
            deleted = record.model_copy(
                update={
                    "status": MemoryStatus.DELETED,
                    "deleted_by": actor,
                    "deleted_at": now,
                    "updated_at": now,
                    "version": record.version + 1,
                },
                deep=True,
            )
            self._records[memory_id] = deleted
            return deleted.model_copy(deep=True)


class MySQLLongTermMemoryStore:
    """PyMySQL repository. Blocking driver work is isolated with ``to_thread``."""

    TABLE = "agent_long_term_memory"

    def __init__(
        self, connection_factory: Callable[[], Any], *, logical_key_lock_timeout: int = 5
    ) -> None:
        self._connection_factory = connection_factory
        self._logical_key_lock_timeout = max(1, min(int(logical_key_lock_timeout), 30))

    @classmethod
    def from_parameters(
        cls,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        connect_timeout: int = 5,
        read_timeout: int = 5,
        write_timeout: int = 5,
        logical_key_lock_timeout: int = 5,
    ) -> "MySQLLongTermMemoryStore":
        """Create lazily; importing this module does not require PyMySQL."""

        import pymysql

        def connect() -> Any:
            return pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
            )

        return cls(connect, logical_key_lock_timeout=logical_key_lock_timeout)

    async def healthcheck(self) -> bool:
        return await asyncio.to_thread(self._healthcheck_sync)

    async def create_candidate(self, candidate: MemoryCandidate) -> LongTermMemory:
        return await asyncio.to_thread(self._create_candidate_sync, candidate)

    async def get(
        self, scope: MemoryScope, memory_id: str, *, include_deleted: bool = False
    ) -> LongTermMemory | None:
        return await asyncio.to_thread(self._get_sync, scope, memory_id, include_deleted)

    async def list_memories(
        self,
        scope: MemoryScope,
        *,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LongTermMemory]:
        return await asyncio.to_thread(
            self._list_memories_sync, scope, statuses, limit, offset
        )

    async def list_active(
        self,
        scope: MemoryScope,
        *,
        memory_types: Sequence[MemoryType] | None = None,
        at: datetime | None = None,
        limit: int = 100,
    ) -> list[LongTermMemory]:
        return await asyncio.to_thread(
            self._list_active_sync, scope, memory_types, at, limit
        )

    async def confirm(
        self, scope: MemoryScope, memory_id: str, *, confirmed_by: str
    ) -> LongTermMemory:
        return await asyncio.to_thread(
            self._confirm_sync, scope, memory_id, _validated_actor(confirmed_by)
        )

    async def soft_delete(
        self, scope: MemoryScope, memory_id: str, *, deleted_by: str
    ) -> LongTermMemory:
        return await asyncio.to_thread(
            self._soft_delete_sync, scope, memory_id, _validated_actor(deleted_by)
        )

    def _healthcheck_sync(self) -> bool:
        connection = None
        try:
            connection = self._connection_factory()
            with connection.cursor() as cursor:
                # This checks both the database connection and that the migration exists.
                cursor.execute(f"SELECT 1 AS ok FROM {self.TABLE} LIMIT 1")
            return True
        except Exception:
            return False
        finally:
            if connection is not None:
                connection.close()

    def _create_candidate_sync(self, candidate: MemoryCandidate) -> LongTermMemory:
        memory_id = uuid.uuid4().hex
        dedupe_key = _candidate_dedupe_key(candidate)
        now = utc_now()
        sql = f"""
            INSERT INTO {self.TABLE} (
                memory_id, candidate_dedupe_key, tenant_id, user_id, application_id,
                memory_type,
                memory_key, summary_text, memory_value, status, confidence,
                source_session_id, source_message_id, created_by, valid_from,
                expires_at, created_at, updated_at, version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, 1
            )
        """
        params = (
            memory_id,
            dedupe_key,
            candidate.scope.tenant_id,
            candidate.scope.user_id,
            candidate.scope.application_id,
            candidate.memory_type.value,
            candidate.memory_key,
            candidate.summary,
            json.dumps(candidate.value, ensure_ascii=False, separators=(",", ":")),
            MemoryStatus.CANDIDATE.value,
            candidate.confidence,
            candidate.source_session_id,
            candidate.source_message_id,
            candidate.created_by,
            _db_datetime(candidate.valid_from),
            _db_datetime(candidate.expires_at),
            _db_datetime(now),
            _db_datetime(now),
        )
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(sql, params)
                except Exception as exc:
                    if not exc.args or exc.args[0] != 1062:
                        raise
                    connection.rollback()
                    existing = self._select_by_dedupe_key(cursor, dedupe_key)
                    if existing is None:
                        raise RuntimeError(
                            "candidate idempotency conflict could not be resolved"
                        ) from exc
                    existing_memory = _row_to_memory(existing)
                    if not _candidate_matches_memory(candidate, existing_memory):
                        raise InvalidMemoryTransitionError(
                            "source message and memory_key were reused with a different payload"
                        )
                    return existing_memory
                row = self._select_one(cursor, candidate.scope, memory_id)
            connection.commit()
            if row is None:  # defensive: INSERT succeeded but row cannot be read
                raise RuntimeError("created memory could not be read back")
            return _row_to_memory(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _get_sync(
        self, scope: MemoryScope, memory_id: str, include_deleted: bool
    ) -> LongTermMemory | None:
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                row = self._select_one(cursor, scope, memory_id)
            if row is None or (
                not include_deleted and row["status"] == MemoryStatus.DELETED.value
            ):
                return None
            return _row_to_memory(row)
        finally:
            connection.close()

    def _list_memories_sync(
        self,
        scope: MemoryScope,
        statuses: Sequence[MemoryStatus] | None,
        limit: int,
        offset: int,
    ) -> list[LongTermMemory]:
        conditions = ["tenant_id = %s", "user_id = %s", "application_id = %s"]
        params: list[Any] = list(_scope_params(scope))
        if statuses is not None:
            if not statuses:
                return []
            conditions.append("status IN (" + ",".join(["%s"] * len(statuses)) + ")")
            params.extend(status.value for status in statuses)
        params.extend((_validated_limit(limit), max(0, int(offset))))
        sql = f"""
            SELECT * FROM {self.TABLE}
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC, memory_id DESC
            LIMIT %s OFFSET %s
        """
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = _mapping_rows(cursor, cursor.fetchall())
            return [_row_to_memory(row) for row in rows]
        finally:
            connection.close()

    def _list_active_sync(
        self,
        scope: MemoryScope,
        memory_types: Sequence[MemoryType] | None,
        at: datetime | None,
        limit: int,
    ) -> list[LongTermMemory]:
        effective_at = _db_datetime(_as_utc(at or utc_now()))
        conditions = [
            "tenant_id = %s",
            "user_id = %s",
            "application_id = %s",
            "status = %s",
            "valid_from <= %s",
            "(expires_at IS NULL OR expires_at > %s)",
        ]
        params: list[Any] = [
            *_scope_params(scope),
            MemoryStatus.ACTIVE.value,
            effective_at,
            effective_at,
        ]
        if memory_types is not None:
            if not memory_types:
                return []
            conditions.append(
                "memory_type IN (" + ",".join(["%s"] * len(memory_types)) + ")"
            )
            params.extend(memory_type.value for memory_type in memory_types)
        params.append(_validated_limit(limit))
        sql = f"""
            SELECT * FROM {self.TABLE}
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC, memory_id DESC
            LIMIT %s
        """
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = _mapping_rows(cursor, cursor.fetchall())
            return [_row_to_memory(row) for row in rows]
        finally:
            connection.close()

    def _confirm_sync(
        self, scope: MemoryScope, memory_id: str, confirmed_by: str
    ) -> LongTermMemory:
        connection = self._connection_factory()
        advisory_lock: str | None = None
        try:
            with connection.cursor() as cursor:
                # Read the logical key first without holding a row lock while waiting for
                # another confirmation of the same key. State is re-read under FOR UPDATE
                # after the connection-scoped advisory lock has been acquired.
                initial = self._select_one(cursor, scope, memory_id)
                if initial is None:
                    raise MemoryNotFoundError("memory not found")
                if initial.get("memory_key"):
                    advisory_lock = _logical_key_lock_name(
                        scope,
                        str(initial["memory_type"]),
                        str(initial["memory_key"]),
                    )
                    cursor.execute(
                        "SELECT GET_LOCK(%s, %s) AS acquired",
                        (advisory_lock, self._logical_key_lock_timeout),
                    )
                    lock_row = _mapping_rows(cursor, [cursor.fetchone()])[0]
                    if int(lock_row.get("acquired") or 0) != 1:
                        raise MemoryConcurrencyError(
                            "timed out while serializing confirmation of this memory key"
                        )

                row = self._select_one(cursor, scope, memory_id, for_update=True)
                if row is None:
                    raise MemoryNotFoundError("memory not found")
                status = MemoryStatus(row["status"])
                if status == MemoryStatus.ACTIVE:
                    connection.commit()
                    return _row_to_memory(row)
                if status != MemoryStatus.CANDIDATE:
                    raise InvalidMemoryTransitionError(
                        f"cannot confirm memory in status {status.value}"
                    )
                now = utc_now()
                expires_at = _optional_datetime(row.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    raise InvalidMemoryTransitionError(
                        "cannot confirm an expired memory candidate"
                    )

                if row.get("memory_key"):
                    cursor.execute(
                        f"""
                        UPDATE {self.TABLE}
                        SET status = %s, superseded_by = %s, updated_at = %s,
                            version = version + 1
                        WHERE tenant_id = %s AND user_id = %s AND application_id = %s
                          AND memory_type = %s AND memory_key = %s
                          AND status = %s AND memory_id <> %s
                        """,
                        (
                            MemoryStatus.SUPERSEDED.value,
                            memory_id,
                            _db_datetime(now),
                            *_scope_params(scope),
                            row["memory_type"],
                            row["memory_key"],
                            MemoryStatus.ACTIVE.value,
                            memory_id,
                        ),
                    )

                cursor.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET status = %s, confirmed_by = %s, confirmed_at = %s,
                        updated_at = %s, version = version + 1
                    WHERE tenant_id = %s AND user_id = %s AND application_id = %s
                      AND memory_id = %s AND status = %s
                    """,
                    (
                        MemoryStatus.ACTIVE.value,
                        confirmed_by,
                        _db_datetime(now),
                        _db_datetime(now),
                        *_scope_params(scope),
                        memory_id,
                        MemoryStatus.CANDIDATE.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InvalidMemoryTransitionError("memory state changed concurrently")
                updated = self._select_one(cursor, scope, memory_id)
            connection.commit()
            if updated is None:
                raise RuntimeError("confirmed memory could not be read back")
            return _row_to_memory(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            if advisory_lock is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT RELEASE_LOCK(%s)", (advisory_lock,))
                except Exception:
                    # Closing the connection also releases MySQL advisory locks.
                    pass
            connection.close()

    def _soft_delete_sync(
        self, scope: MemoryScope, memory_id: str, deleted_by: str
    ) -> LongTermMemory:
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                row = self._select_one(cursor, scope, memory_id, for_update=True)
                if row is None:
                    raise MemoryNotFoundError("memory not found")
                if row["status"] == MemoryStatus.DELETED.value:
                    connection.commit()
                    return _row_to_memory(row)
                now = utc_now()
                cursor.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET status = %s, deleted_by = %s, deleted_at = %s,
                        updated_at = %s, version = version + 1
                    WHERE tenant_id = %s AND user_id = %s AND application_id = %s
                      AND memory_id = %s AND status <> %s
                    """,
                    (
                        MemoryStatus.DELETED.value,
                        deleted_by,
                        _db_datetime(now),
                        _db_datetime(now),
                        *_scope_params(scope),
                        memory_id,
                        MemoryStatus.DELETED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InvalidMemoryTransitionError("memory state changed concurrently")
                updated = self._select_one(cursor, scope, memory_id)
            connection.commit()
            if updated is None:
                raise RuntimeError("deleted memory could not be read back")
            return _row_to_memory(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _select_one(
        self,
        cursor: Any,
        scope: MemoryScope,
        memory_id: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT * FROM {self.TABLE}
            WHERE tenant_id = %s AND user_id = %s AND application_id = %s
              AND memory_id = %s{suffix}
            """,
            (*_scope_params(scope), memory_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _mapping_rows(cursor, [row])[0]

    def _select_by_dedupe_key(
        self, cursor: Any, dedupe_key: str
    ) -> Mapping[str, Any] | None:
        cursor.execute(
            f"SELECT * FROM {self.TABLE} WHERE candidate_dedupe_key = %s",
            (dedupe_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _mapping_rows(cursor, [row])[0]


def _scope_params(scope: MemoryScope) -> tuple[str, str, str]:
    return scope.tenant_id, scope.user_id, scope.application_id


def _allowed_memory_key(memory_key: str) -> bool:
    exact = {
        "default_metric", "default_time_grain", "default_time_period",
        "default_time_range", "default_dimension", "default_comparison",
        "default_unit", "default_currency", "default_output_format",
        "default_entity", "default_fields",
    }
    prefixes = ("metric_alias:", "default_filter:", "business_term:", "preference:")
    if memory_key in exact:
        return True
    return any(
        memory_key.startswith(prefix) and bool(memory_key.removeprefix(prefix).strip())
        for prefix in prefixes
    )


def _memory_type_matches_key(memory_type: MemoryType, memory_key: str) -> bool:
    if memory_key.startswith("metric_alias:"):
        return memory_type == MemoryType.METRIC_ALIAS
    if memory_key.startswith("default_filter:"):
        return memory_type == MemoryType.DEFAULT_FILTER
    if memory_key.startswith("business_term:"):
        return memory_type == MemoryType.BUSINESS_CONTEXT
    if memory_key.startswith("preference:"):
        return memory_type == MemoryType.USER_PREFERENCE
    if memory_key in {"default_unit", "default_currency", "default_output_format"}:
        return memory_type == MemoryType.DISPLAY_PREFERENCE
    return memory_type == MemoryType.USER_PREFERENCE


def _valid_memory_value(memory_key: str, value: Mapping[str, Any]) -> bool:
    required = {
        "default_metric": {"metric"},
        "default_time_grain": {"time_grain"},
        "default_time_period": {"time_period"},
        "default_time_range": {"time_period"},
        "default_dimension": {"dimension", "dimensions"},
        "default_comparison": {"comparison_type"},
        "default_unit": {"unit"},
        "default_currency": {"currency"},
        "default_output_format": {"output_format"},
        "default_entity": {"entity"},
        "default_fields": {"fields"},
    }
    allowed: set[str]
    if memory_key in required:
        allowed = required[memory_key]
        if not allowed.intersection(value) or not set(value).issubset(allowed):
            return False
        return _safe_memory_scalars(value)
    if memory_key.startswith("metric_alias:"):
        allowed = {"alias", "canonical_metric"}
        return allowed.issubset(value) and set(value).issubset(allowed) and _safe_memory_scalars(value)
    if memory_key.startswith("default_filter:"):
        allowed = {"filter_field", "filter_operator", "filter_value"}
        if not allowed.issubset(value) or not set(value).issubset(allowed):
            return False
        if value.get("filter_operator") not in {"=", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains"}:
            return False
        return _safe_memory_scalars(value)
    if memory_key.startswith("business_term:"):
        allowed = {"term", "meaning"}
        return allowed.issubset(value) and set(value).issubset(allowed) and _safe_memory_scalars(value)
    if memory_key.startswith("preference:"):
        return set(value) == {"preference"} and _safe_memory_scalars(value)
    return False


def _walk_mapping_keys(value: Any):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_mapping_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mapping_keys(nested)


def _safe_memory_scalars(value: Mapping[str, Any]) -> bool:
    """Accept only small JSON primitives/lists; durable memory is not a document store."""
    for item in value.values():
        if isinstance(item, str):
            if not item.strip() or len(item) > 500 or any(ord(char) < 32 for char in item):
                return False
        elif isinstance(item, bool) or item is None or isinstance(item, int):
            continue
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, list):
            if not item or len(item) > 20:
                return False
            if not all(
                isinstance(entry, (str, int, float, bool))
                and (not isinstance(entry, float) or math.isfinite(entry))
                and (not isinstance(entry, str) or (entry.strip() and len(entry) <= 200))
                for entry in item
            ):
                return False
        else:
            return False
    return True


def _candidate_dedupe_key(candidate: MemoryCandidate) -> str:
    raw = "\x1f".join(
        (
            candidate.scope.tenant_id,
            candidate.scope.user_id,
            candidate.scope.application_id,
            candidate.source_session_id,
            candidate.source_message_id,
            candidate.memory_type.value,
            candidate.memory_key or "",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _memory_dedupe_key(memory: LongTermMemory) -> str:
    raw = "\x1f".join(
        (
            memory.scope.tenant_id,
            memory.scope.user_id,
            memory.scope.application_id,
            memory.source_session_id,
            memory.source_message_id,
            memory.memory_type.value,
            memory.memory_key or "",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_matches_memory(
    candidate: MemoryCandidate, memory: LongTermMemory
) -> bool:
    return (
        candidate.scope == memory.scope
        and candidate.memory_type == memory.memory_type
        and candidate.memory_key == memory.memory_key
        and candidate.summary == memory.summary
        and candidate.value == memory.value
    )


def _logical_key_lock_name(
    scope: MemoryScope, memory_type: str, memory_key: str
) -> str:
    raw = "\x1f".join((*_scope_params(scope), memory_type, memory_key))
    # MySQL 5.7 limits advisory lock names to 64 characters.
    return "da_ltm:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:56]


def _validated_actor(actor: str) -> str:
    value = actor.strip()
    if not value or len(value) > 128:
        raise ValueError("actor id must contain 1 to 128 characters")
    return value


def _validated_limit(limit: int) -> int:
    value = int(limit)
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, 500)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value).replace(tzinfo=None)


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("database datetime column returned a non-datetime value")
    return _as_utc(value)


def _mapping_rows(cursor: Any, rows: Sequence[Any]) -> list[Mapping[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return list(rows)
    description = cursor.description
    if not description:
        raise TypeError("cursor returned tuple rows without column metadata")
    names = [item[0] for item in description]
    return [dict(zip(names, row, strict=True)) for row in rows]


def _row_to_memory(row: Mapping[str, Any]) -> LongTermMemory:
    raw_value = row["memory_value"]
    if isinstance(raw_value, (str, bytes, bytearray)):
        raw_value = json.loads(raw_value)
    if not isinstance(raw_value, dict):
        raise TypeError("memory_value must be a JSON object")
    confidence = row["confidence"]
    if isinstance(confidence, Decimal):
        confidence = float(confidence)
    scope = MemoryScope(
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        application_id=row["application_id"],
    )
    return LongTermMemory(
        memory_id=row["memory_id"],
        scope=scope,
        memory_type=MemoryType(row["memory_type"]),
        memory_key=row.get("memory_key"),
        summary=row["summary_text"],
        value=raw_value,
        status=MemoryStatus(row["status"]),
        confidence=confidence,
        source_session_id=row["source_session_id"],
        source_message_id=row["source_message_id"],
        created_by=row.get("created_by"),
        confirmed_by=row.get("confirmed_by"),
        deleted_by=row.get("deleted_by"),
        valid_from=_optional_datetime(row["valid_from"]),
        expires_at=_optional_datetime(row.get("expires_at")),
        superseded_by=row.get("superseded_by"),
        created_at=_optional_datetime(row["created_at"]),
        updated_at=_optional_datetime(row["updated_at"]),
        confirmed_at=_optional_datetime(row.get("confirmed_at")),
        deleted_at=_optional_datetime(row.get("deleted_at")),
        version=int(row["version"]),
    )
