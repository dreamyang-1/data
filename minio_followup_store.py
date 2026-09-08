"""MinIO-backed immutable datasets for conversational follow-up analysis.

This module is intentionally standalone.  It does not import or modify the agent
workflow.  A caller may use ``MinioFollowupStore.from_env()`` in an adapter and keep
the returned ``DatasetReference`` in Redis/session state.

Small snapshots use versioned gzip JSON. Medium snapshots use partitioned Parquet
through a lazily imported PyArrow adapter. Very large results remain in the query
engine and are deliberately rejected by the in-process hybrid store.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import uuid
from enum import StrEnum
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, Sequence


FORMAT_VERSION = "youo-followup-dataset/1"
PARQUET_FORMAT_VERSION = "youo-followup-parquet/1"
DEFAULT_PREFIX = "data-analysis/followup-datasets"
DEFAULT_TTL_SECONDS = 2 * 60 * 60
MAX_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_OBJECT_BYTES = 256 * 1024 * 1024
DEFAULT_PREVIEW_ROWS = 100


class DatasetStorageRoute(StrEnum):
    SMALL_JSON_GZIP = "SMALL_JSON_GZIP"
    MEDIUM_PARQUET = "MEDIUM_PARQUET"
    LARGE_QUERY_ENGINE = "LARGE_QUERY_ENGINE"


def choose_dataset_storage_route(
    *,
    row_count: int,
    estimated_uncompressed_bytes: int,
    small_max_rows: int = 10_000,
    small_max_bytes: int = 5 * 1024 * 1024,
    medium_max_rows: int = 1_000_000,
    medium_max_bytes: int = 256 * 1024 * 1024,
) -> DatasetStorageRoute:
    """Classify by both row count and bytes; either limit can promote a dataset."""
    if row_count < 0 or estimated_uncompressed_bytes < 0:
        raise ValueError("dataset size values must not be negative")
    if row_count <= small_max_rows and estimated_uncompressed_bytes <= small_max_bytes:
        return DatasetStorageRoute.SMALL_JSON_GZIP
    if row_count <= medium_max_rows and estimated_uncompressed_bytes <= medium_max_bytes:
        return DatasetStorageRoute.MEDIUM_PARQUET
    return DatasetStorageRoute.LARGE_QUERY_ENGINE


def estimate_dataset_json_bytes(
    columns: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> int:
    """Bounded preflight estimate used before selecting a storage adapter."""
    total = len(_canonical_json({"columns": list(columns)}))
    for row in rows:
        total += len(_canonical_json({str(key): _json_value(value) for key, value in row.items()}))
    return total


class DatasetStoreError(RuntimeError):
    """Base error for dataset storage and follow-up execution."""


class DatasetNotFound(DatasetStoreError):
    pass


class DatasetExpired(DatasetStoreError):
    pass


class DatasetScopeMismatch(DatasetStoreError):
    pass


class InvalidFollowupOperation(DatasetStoreError):
    pass


class MinioClientProtocol(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...
    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: io.BytesIO,
        length: int,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Any: ...
    def get_object(self, bucket_name: str, object_name: str) -> Any: ...
    def stat_object(self, bucket_name: str, object_name: str) -> Any: ...
    def remove_object(self, bucket_name: str, object_name: str) -> Any: ...


@dataclass(frozen=True)
class DatasetScope:
    tenant_id: str
    user_id: str
    application_id: str
    conversation_id: str
    authorized_semantic_scope_fingerprint: str = ''

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if name == 'authorized_semantic_scope_fingerprint' and value == '':
                continue  # Legacy standalone store records cannot match a scoped request.
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a non-empty string of at most 128 characters")


@dataclass(frozen=True)
class DatasetReference:
    dataset_id: str
    bucket: str
    object_name: str
    scope: DatasetScope
    columns: tuple[str, ...]
    row_count: int
    byte_size: int
    snapshot_id: str
    data_as_of: str
    created_at: str
    expires_at: str
    source_type: str
    source_ref: str
    semantic_model_id: int | None = None
    business_domain_ids: tuple[int, ...] = ()
    metric_ids: tuple[str, ...] = ()
    parent_dataset_ids: tuple[str, ...] = ()
    transformation_log: tuple[dict[str, Any], ...] = ()
    storage_type: str = "MINIO_JSON_GZIP"
    format_version: str = FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["columns"] = list(self.columns)
        value["parent_dataset_ids"] = list(self.parent_dataset_ids)
        value["business_domain_ids"] = list(self.business_domain_ids)
        value["metric_ids"] = list(self.metric_ids)
        value["transformation_log"] = list(self.transformation_log)
        return value


@dataclass(frozen=True)
class LoadedDataset:
    reference: DatasetReference
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FollowupResult:
    reference: DatasetReference
    preview_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DatasetStoreError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetStoreError(f"{label} must include a timezone")
    return parsed


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("dataset cannot contain NaN or Infinity")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("dataset cannot contain a non-finite Decimal")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dataset datetime values must include a timezone")
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError(f"unsupported dataset value type: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _scope_digest(scope: DatasetScope) -> str:
    values = asdict(scope)
    if not scope.authorized_semantic_scope_fingerprint:
        values.pop('authorized_semantic_scope_fingerprint')
    raw = "\x1f".join(values.values()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _reference_from_dict(value: Mapping[str, Any]) -> DatasetReference:
    scope_raw = value.get("scope")
    if not isinstance(scope_raw, Mapping):
        raise DatasetStoreError("dataset reference has no valid scope")
    return DatasetReference(
        dataset_id=str(value["dataset_id"]),
        bucket=str(value["bucket"]),
        object_name=str(value["object_name"]),
        scope=DatasetScope(**dict(scope_raw)),
        columns=tuple(value["columns"]),
        row_count=int(value["row_count"]),
        byte_size=int(value["byte_size"]),
        snapshot_id=str(value["snapshot_id"]),
        data_as_of=str(value["data_as_of"]),
        created_at=str(value["created_at"]),
        expires_at=str(value["expires_at"]),
        source_type=str(value["source_type"]),
        source_ref=str(value["source_ref"]),
        semantic_model_id=(
            int(value["semantic_model_id"])
            if value.get("semantic_model_id") is not None
            else None
        ),
        business_domain_ids=tuple(int(item) for item in value.get("business_domain_ids", ())),
        metric_ids=tuple(str(item) for item in value.get("metric_ids", ())),
        parent_dataset_ids=tuple(value.get("parent_dataset_ids", ())),
        transformation_log=tuple(value.get("transformation_log", ())),
        storage_type=str(value.get("storage_type", "MINIO_JSON_GZIP")),
        format_version=str(value.get("format_version", FORMAT_VERSION)),
    )


def dataset_reference_from_dict(value: Mapping[str, Any]) -> DatasetReference:
    """Public deserializer for references restored from Redis/session storage."""
    return _reference_from_dict(value)


class MinioFollowupStore:
    """Store immutable query snapshots and create controlled derived datasets."""

    def __init__(
        self,
        client: MinioClientProtocol,
        *,
        bucket: str,
        prefix: str = DEFAULT_PREFIX,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("invalid MinIO bucket")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.max_object_bytes = max_object_bytes

    @classmethod
    def from_env(cls) -> "MinioFollowupStore":
        """Create a client without hard-coding credentials in source code."""
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("install the 'minio' package before using this adapter") from exc

        endpoint = os.getenv("DATA_AGENT_MINIO_ENDPOINT") or os.getenv("MINIO_HOST")
        access_key = os.getenv("DATA_AGENT_MINIO_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY")
        secret_key = os.getenv("DATA_AGENT_MINIO_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY")
        bucket = os.getenv("DATA_AGENT_MINIO_BUCKET") or os.getenv("AGENT_MINIO_BUCKET")
        secure = (os.getenv("DATA_AGENT_MINIO_SECURE", "false").lower() == "true")
        if not all((endpoint, access_key, secret_key, bucket)):
            raise RuntimeError("MinIO endpoint, access key, secret key and bucket are required")
        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        return cls(client, bucket=bucket)

    def check_ready(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            raise DatasetStoreError(f"MinIO bucket does not exist: {self.bucket}")

    def save_dataset(
        self,
        *,
        scope: DatasetScope,
        columns: Sequence[str],
        rows: Iterable[Mapping[str, Any]],
        snapshot_id: str,
        data_as_of: datetime,
        source_type: str,
        source_ref: str,
        semantic_model_id: int | None = None,
        business_domain_ids: Sequence[int] = (),
        metric_ids: Sequence[str] = (),
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        parent_dataset_ids: Sequence[str] = (),
        transformation_log: Sequence[Mapping[str, Any]] = (),
    ) -> DatasetReference:
        scope.validate()
        normalized_columns = tuple(str(column).strip() for column in columns)
        if not normalized_columns or any(not value for value in normalized_columns):
            raise ValueError("columns must not be empty or blank")
        if len(normalized_columns) != len(set(normalized_columns)):
            raise ValueError("columns must be unique")
        if not 60 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be between 60 and {MAX_TTL_SECONDS}")
        if data_as_of.tzinfo is None or data_as_of.utcoffset() is None:
            raise ValueError("data_as_of must include a timezone")

        normalized_rows: list[dict[str, Any]] = []
        declared = set(normalized_columns)
        for row in rows:
            unknown = set(row) - declared
            if unknown:
                raise ValueError(f"row contains undeclared columns: {sorted(unknown)}")
            normalized_rows.append(
                {column: _json_value(row.get(column)) for column in normalized_columns}
            )

        now = _utc_now()
        dataset_id = f"ds-{uuid.uuid4()}"
        object_name = (
            f"{self.prefix}/{_scope_digest(scope)}/"
            f"{now:%Y/%m/%d}/{dataset_id}.json.gz"
        )
        content_fingerprint = hashlib.sha256(
            _canonical_json({"columns": normalized_columns, "rows": normalized_rows})
        ).hexdigest()
        provisional = DatasetReference(
            dataset_id=dataset_id,
            bucket=self.bucket,
            object_name=object_name,
            scope=scope,
            columns=normalized_columns,
            row_count=len(normalized_rows),
            byte_size=0,
            snapshot_id=snapshot_id or content_fingerprint,
            data_as_of=data_as_of.isoformat(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
            source_type=source_type,
            source_ref=source_ref,
            semantic_model_id=semantic_model_id,
            business_domain_ids=tuple(sorted(set(int(item) for item in business_domain_ids))),
            metric_ids=tuple(sorted(set(str(item) for item in metric_ids if str(item)))),
            parent_dataset_ids=tuple(parent_dataset_ids),
            transformation_log=tuple(dict(item) for item in transformation_log),
        )
        envelope = {
            "format_version": FORMAT_VERSION,
            "reference": provisional.to_dict(),
            "content_sha256": content_fingerprint,
            "rows": normalized_rows,
        }
        payload = gzip.compress(_canonical_json(envelope), compresslevel=6)
        if len(payload) > self.max_object_bytes:
            raise DatasetStoreError(
                f"compressed dataset is {len(payload)} bytes; route it to Parquet/SQL storage"
            )
        reference = replace(provisional, byte_size=len(payload))
        # Embed an exact byte size in the persisted reference.  Usually two rounds
        # converge; the bounded loop avoids relying on that implementation detail.
        for _ in range(4):
            envelope["reference"] = reference.to_dict()
            payload = gzip.compress(_canonical_json(envelope), compresslevel=6)
            if reference.byte_size == len(payload):
                break
            reference = replace(reference, byte_size=len(payload))
        if len(payload) > self.max_object_bytes:
            raise DatasetStoreError("dataset exceeds MinIO object size policy")

        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(payload),
            len(payload),
            content_type="application/gzip",
            metadata={
                "dataset-id": dataset_id,
                "expires-at": reference.expires_at,
                "content-sha256": content_fingerprint,
            },
        )
        return reference

    def load_dataset(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        now: datetime | None = None,
    ) -> LoadedDataset:
        current_scope.validate()
        if current_scope != reference.scope:
            raise DatasetScopeMismatch("dataset does not belong to the current conversation scope")
        if reference.bucket != self.bucket:
            raise DatasetScopeMismatch("dataset bucket does not match this store")
        expected_prefix = f"{self.prefix}/{_scope_digest(current_scope)}/"
        if not reference.object_name.startswith(expected_prefix):
            raise DatasetScopeMismatch("dataset object path does not match its scope")
        if (now or _utc_now()) >= _parse_datetime(reference.expires_at, "expires_at"):
            raise DatasetExpired("dataset has expired; rerun the original query")

        try:
            stat = self.client.stat_object(self.bucket, reference.object_name)
            size = int(getattr(stat, "size", 0) or 0)
            if size > self.max_object_bytes:
                raise DatasetStoreError("dataset object exceeds download size policy")
            response = self.client.get_object(self.bucket, reference.object_name)
            try:
                payload = response.read(self.max_object_bytes + 1)
            finally:
                close = getattr(response, "close", None)
                release = getattr(response, "release_conn", None)
                if callable(close):
                    close()
                if callable(release):
                    release()
        except DatasetStoreError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise DatasetNotFound("dataset object no longer exists") from exc
            raise DatasetStoreError("failed to read dataset from MinIO") from exc
        if len(payload) > self.max_object_bytes:
            raise DatasetStoreError("dataset download exceeds size policy")

        try:
            envelope = json.loads(gzip.decompress(payload))
            stored_reference = _reference_from_dict(envelope["reference"])
            rows = envelope["rows"]
        except Exception as exc:
            raise DatasetStoreError("dataset object is corrupt or has an unsupported format") from exc
        if envelope.get("format_version") != FORMAT_VERSION:
            raise DatasetStoreError("unsupported dataset format version")
        if stored_reference.dataset_id != reference.dataset_id:
            raise DatasetStoreError("dataset reference does not match stored object")
        if (
            stored_reference.scope != current_scope
            or stored_reference.bucket != self.bucket
            or stored_reference.object_name != reference.object_name
        ):
            raise DatasetScopeMismatch("stored dataset scope does not match the current request")
        if (now or _utc_now()) >= _parse_datetime(stored_reference.expires_at, "expires_at"):
            raise DatasetExpired("dataset has expired; rerun the original query")
        fingerprint = hashlib.sha256(
            _canonical_json({"columns": stored_reference.columns, "rows": rows})
        ).hexdigest()
        if fingerprint != envelope.get("content_sha256"):
            raise DatasetStoreError("dataset checksum validation failed")
        if len(rows) != stored_reference.row_count:
            raise DatasetStoreError("dataset row count validation failed")
        return LoadedDataset(stored_reference, tuple(dict(row) for row in rows))

    def execute_followup(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        operation: Mapping[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        preview_rows: int = DEFAULT_PREVIEW_ROWS,
    ) -> FollowupResult:
        """Execute a whitelisted operation and persist a new immutable dataset."""
        loaded = self.load_dataset(reference, current_scope=current_scope)
        columns, rows = apply_followup_operation(
            loaded.reference.columns, loaded.rows, operation
        )
        derived = self.save_dataset(
            scope=current_scope,
            columns=columns,
            rows=rows,
            snapshot_id=reference.snapshot_id,
            data_as_of=_parse_datetime(reference.data_as_of, "data_as_of"),
            source_type="DERIVED_DATASET",
            source_ref=reference.dataset_id,
            ttl_seconds=ttl_seconds,
            parent_dataset_ids=(reference.dataset_id,),
            transformation_log=(*reference.transformation_log, dict(operation)),
            semantic_model_id=reference.semantic_model_id,
            business_domain_ids=reference.business_domain_ids,
            metric_ids=reference.metric_ids,
        )
        return FollowupResult(derived, tuple(rows[: max(0, min(preview_rows, 10000))]))

    def join_datasets(
        self,
        references: Sequence[DatasetReference],
        *,
        current_scope: DatasetScope,
        join_keys: Sequence[str],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        preview_rows: int = DEFAULT_PREVIEW_ROWS,
    ) -> FollowupResult:
        """Inner-join immutable datasets after scope and cardinality validation."""
        return _join_with_store(
            self, references, current_scope=current_scope, join_keys=join_keys,
            ttl_seconds=ttl_seconds, preview_rows=preview_rows,
        )

    def delete_dataset(
        self, reference: DatasetReference, *, current_scope: DatasetScope
    ) -> None:
        if current_scope != reference.scope:
            raise DatasetScopeMismatch("cannot delete a dataset outside the current scope")
        expected_prefix = f"{self.prefix}/{_scope_digest(current_scope)}/"
        if reference.bucket != self.bucket or not reference.object_name.startswith(expected_prefix):
            raise DatasetScopeMismatch("cannot delete an object outside the dataset scope")
        self.client.remove_object(self.bucket, reference.object_name)


class ParquetMinioFollowupStore:
    """Partitioned Parquet adapter for medium immutable datasets.

    ``object_name`` points to a small JSON manifest. Data parts are immutable and
    checksummed independently. PyArrow is imported lazily so small-only
    deployments do not pay the dependency cost.
    """

    def __init__(
        self,
        client: MinioClientProtocol,
        *,
        bucket: str,
        prefix: str = DEFAULT_PREFIX,
        part_rows: int = 50_000,
        max_dataset_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("invalid MinIO bucket")
        if part_rows <= 0:
            raise ValueError("part_rows must be positive")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.part_rows = part_rows
        self.max_dataset_bytes = max_dataset_bytes

    @staticmethod
    def _pyarrow():
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise DatasetStoreError(
                "Parquet dataset support requires the 'pyarrow' package"
            ) from exc
        return pa, pq

    def check_ready(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            raise DatasetStoreError(f"MinIO bucket does not exist: {self.bucket}")
        self._pyarrow()

    def save_dataset(
        self,
        *,
        scope: DatasetScope,
        columns: Sequence[str],
        rows: Iterable[Mapping[str, Any]],
        snapshot_id: str,
        data_as_of: datetime,
        source_type: str,
        source_ref: str,
        semantic_model_id: int | None = None,
        business_domain_ids: Sequence[int] = (),
        metric_ids: Sequence[str] = (),
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        parent_dataset_ids: Sequence[str] = (),
        transformation_log: Sequence[Mapping[str, Any]] = (),
    ) -> DatasetReference:
        pa, pq = self._pyarrow()
        scope.validate()
        normalized_columns = tuple(str(column).strip() for column in columns)
        if not normalized_columns or any(not item for item in normalized_columns):
            raise ValueError("columns must not be empty or blank")
        if len(normalized_columns) != len(set(normalized_columns)):
            raise ValueError("columns must be unique")
        if not 60 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be between 60 and {MAX_TTL_SECONDS}")
        if data_as_of.tzinfo is None or data_as_of.utcoffset() is None:
            raise ValueError("data_as_of must include a timezone")
        declared = set(normalized_columns)
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            unknown = set(row) - declared
            if unknown:
                raise ValueError(f"row contains undeclared columns: {sorted(unknown)}")
            normalized_rows.append(
                {column: _json_value(row.get(column)) for column in normalized_columns}
            )

        now = _utc_now()
        dataset_id = f"ds-{uuid.uuid4()}"
        base_name = (
            f"{self.prefix}/{_scope_digest(scope)}/{now:%Y/%m/%d}/{dataset_id}"
        )
        manifest_name = f"{base_name}/manifest.json"
        content_fingerprint = hashlib.sha256(
            _canonical_json({"columns": normalized_columns, "rows": normalized_rows})
        ).hexdigest()
        uploaded: list[str] = []
        parts: list[dict[str, Any]] = []
        total_bytes = 0
        try:
            chunks = [
                normalized_rows[index:index + self.part_rows]
                for index in range(0, len(normalized_rows), self.part_rows)
            ] or [[]]
            for index, chunk in enumerate(chunks):
                table = (
                    pa.Table.from_pylist(chunk)
                    if chunk
                    else pa.table({column: pa.array([], type=pa.string()) for column in normalized_columns})
                )
                buffer = pa.BufferOutputStream()
                pq.write_table(table, buffer, compression="zstd")
                payload = buffer.getvalue().to_pybytes()
                total_bytes += len(payload)
                if total_bytes > self.max_dataset_bytes:
                    raise DatasetStoreError(
                        "Parquet dataset exceeds medium-data policy; keep it in the query engine"
                    )
                object_name = f"{base_name}/part-{index:05d}.parquet"
                checksum = hashlib.sha256(payload).hexdigest()
                self.client.put_object(
                    self.bucket,
                    object_name,
                    io.BytesIO(payload),
                    len(payload),
                    content_type="application/vnd.apache.parquet",
                    metadata={"dataset-id": dataset_id, "content-sha256": checksum},
                )
                uploaded.append(object_name)
                parts.append({
                    "object_name": object_name,
                    "row_count": len(chunk),
                    "byte_size": len(payload),
                    "content_sha256": checksum,
                })

            reference = DatasetReference(
                dataset_id=dataset_id,
                bucket=self.bucket,
                object_name=manifest_name,
                scope=scope,
                columns=normalized_columns,
                row_count=len(normalized_rows),
                byte_size=total_bytes,
                snapshot_id=snapshot_id or content_fingerprint,
                data_as_of=data_as_of.isoformat(),
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
                source_type=source_type,
                source_ref=source_ref,
                semantic_model_id=semantic_model_id,
                business_domain_ids=tuple(sorted(set(int(item) for item in business_domain_ids))),
                metric_ids=tuple(sorted(set(str(item) for item in metric_ids if str(item)))),
                parent_dataset_ids=tuple(parent_dataset_ids),
                transformation_log=tuple(dict(item) for item in transformation_log),
                storage_type="MINIO_PARQUET",
                format_version=PARQUET_FORMAT_VERSION,
            )
            manifest = {
                "format_version": PARQUET_FORMAT_VERSION,
                "reference": reference.to_dict(),
                "content_sha256": content_fingerprint,
                "parts": parts,
            }
            manifest_payload = _canonical_json(manifest)
            self.client.put_object(
                self.bucket,
                manifest_name,
                io.BytesIO(manifest_payload),
                len(manifest_payload),
                content_type="application/json",
                metadata={
                    "dataset-id": dataset_id,
                    "expires-at": reference.expires_at,
                    "content-sha256": content_fingerprint,
                },
            )
            return reference
        except Exception:
            # All targets are under a freshly generated dataset_id and cannot
            # overlap user data. Best-effort cleanup prevents orphaned parts.
            for object_name in uploaded:
                try:
                    self.client.remove_object(self.bucket, object_name)
                except Exception:
                    pass
            raise

    def _read_object(self, object_name: str, *, max_bytes: int) -> bytes:
        try:
            stat = self.client.stat_object(self.bucket, object_name)
            size = int(getattr(stat, "size", 0) or 0)
            if size > max_bytes:
                raise DatasetStoreError("dataset object exceeds read size policy")
            response = self.client.get_object(self.bucket, object_name)
            try:
                payload = response.read(max_bytes + 1)
            finally:
                close = getattr(response, "close", None)
                release = getattr(response, "release_conn", None)
                if callable(close):
                    close()
                if callable(release):
                    release()
            if len(payload) > max_bytes:
                raise DatasetStoreError("dataset object exceeds read size policy")
            return payload
        except DatasetStoreError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise DatasetNotFound("dataset object no longer exists") from exc
            raise DatasetStoreError("failed to read Parquet dataset object") from exc

    def _load_manifest(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        now: datetime | None,
        check_expiry: bool = True,
    ) -> dict[str, Any]:
        current_scope.validate()
        if current_scope != reference.scope or reference.bucket != self.bucket:
            raise DatasetScopeMismatch("dataset does not belong to the current scope")
        expected_prefix = f"{self.prefix}/{_scope_digest(current_scope)}/"
        if not reference.object_name.startswith(expected_prefix):
            raise DatasetScopeMismatch("dataset object path does not match its scope")
        if check_expiry and (now or _utc_now()) >= _parse_datetime(
            reference.expires_at, "expires_at"
        ):
            raise DatasetExpired("dataset has expired; rerun the original query")
        payload = self._read_object(reference.object_name, max_bytes=4 * 1024 * 1024)
        try:
            manifest = json.loads(payload)
            stored = _reference_from_dict(manifest["reference"])
        except Exception as exc:
            raise DatasetStoreError("invalid Parquet dataset manifest") from exc
        if manifest.get("format_version") != PARQUET_FORMAT_VERSION:
            raise DatasetStoreError("unsupported Parquet dataset format")
        if stored != reference:
            raise DatasetStoreError("dataset reference does not match its manifest")
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not parts:
            raise DatasetStoreError("Parquet manifest has no parts")
        part_prefix = reference.object_name.removesuffix("manifest.json")
        total_rows = 0
        total_bytes = 0
        seen_names: set[str] = set()
        for part in parts:
            if not isinstance(part, dict):
                raise DatasetStoreError("invalid Parquet part descriptor")
            object_name = part.get("object_name")
            if (
                not isinstance(object_name, str)
                or not object_name.startswith(part_prefix)
                or not re.fullmatch(r"part-\d{5}\.parquet", object_name[len(part_prefix):])
                or object_name in seen_names
            ):
                raise DatasetScopeMismatch("Parquet part path is outside the dataset scope")
            seen_names.add(object_name)
            try:
                part_rows = int(part["row_count"])
                part_bytes = int(part["byte_size"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DatasetStoreError("invalid Parquet part size metadata") from exc
            if part_rows < 0 or part_bytes <= 0:
                raise DatasetStoreError("invalid Parquet part size metadata")
            total_rows += part_rows
            total_bytes += part_bytes
        if total_rows != reference.row_count or total_bytes != reference.byte_size:
            raise DatasetStoreError("Parquet manifest totals do not match its reference")
        return manifest

    def load_dataset(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        now: datetime | None = None,
    ) -> LoadedDataset:
        pa, pq = self._pyarrow()
        manifest = self._load_manifest(reference, current_scope=current_scope, now=now)
        rows: list[dict[str, Any]] = []
        total_bytes = 0
        for part in manifest["parts"]:
            if not isinstance(part, dict) or not isinstance(part.get("object_name"), str):
                raise DatasetStoreError("invalid Parquet part descriptor")
            part_size = int(part.get("byte_size", 0))
            total_bytes += part_size
            if total_bytes > self.max_dataset_bytes:
                raise DatasetStoreError("Parquet dataset exceeds read size policy")
            payload = self._read_object(part["object_name"], max_bytes=max(1, part_size))
            if hashlib.sha256(payload).hexdigest() != part.get("content_sha256"):
                raise DatasetStoreError("Parquet part checksum validation failed")
            try:
                table = pq.read_table(pa.BufferReader(payload), columns=list(reference.columns))
                rows.extend(table.to_pylist())
            except Exception as exc:
                raise DatasetStoreError("failed to decode Parquet dataset part") from exc
        if len(rows) != reference.row_count:
            raise DatasetStoreError("Parquet dataset row count validation failed")
        fingerprint = hashlib.sha256(
            _canonical_json({"columns": reference.columns, "rows": rows})
        ).hexdigest()
        if fingerprint != manifest.get("content_sha256"):
            raise DatasetStoreError("Parquet dataset checksum validation failed")
        return LoadedDataset(reference, tuple(rows))

    def read_for_followup(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        operation: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
        """Read the minimum safe Parquet projection for a follow-up operation.

        Projection and early-limit pushdown reduce decoding and Python object
        creation. Every object that is read still passes the immutable part
        checksum. Operations not covered here deliberately fall back to a full
        verified load so their established deterministic semantics do not change.
        """
        op = str(operation.get("type", "")).lower()
        declared = reference.columns
        projection = declared
        row_limit: int | None = None
        if op == "select":
            selected = tuple(
                _require_column(value, declared)
                for value in operation.get("columns", ())
            )
            if not selected or len(selected) != len(set(selected)):
                raise InvalidFollowupOperation(
                    "select columns must be non-empty and unique"
                )
            projection = selected
        elif op == "aggregate":
            group_by = tuple(
                _require_column(value, declared)
                for value in operation.get("group_by", ())
            )
            specs = operation.get("aggregations")
            if not isinstance(specs, list) or not specs:
                raise InvalidFollowupOperation(
                    "aggregations must be a non-empty list"
                )
            aggregate_fields = tuple(
                _require_column(spec.get("field"), declared)
                for spec in specs
                if isinstance(spec, Mapping)
            )
            if len(aggregate_fields) != len(specs):
                raise InvalidFollowupOperation("invalid aggregation specification")
            projection = tuple(dict.fromkeys((*group_by, *aggregate_fields)))
        elif op == "limit":
            count = operation.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 100_000:
                raise InvalidFollowupOperation(
                    "limit count must be an integer between 0 and 100000"
                )
            row_limit = count
        elif op == "filter":
            return declared, self._read_filtered_rows(
                reference,
                current_scope=current_scope,
                operation=operation,
            )
        else:
            loaded = self.load_dataset(reference, current_scope=current_scope)
            return apply_followup_operation(
                loaded.reference.columns, loaded.rows, operation
            )

        rows = self._read_projected_rows(
            reference,
            current_scope=current_scope,
            columns=projection,
            row_limit=row_limit,
        )
        return apply_followup_operation(projection, rows, operation)

    def _read_filtered_rows(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        operation: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply a safe Arrow filter before converting matching rows to Python."""
        pa, pq = self._pyarrow()
        import pyarrow.compute as pc

        field_name = _require_column(operation.get("field"), reference.columns)
        operator = str(operation.get("operator", "eq")).lower()
        target = operation.get("value")
        if operator not in {"eq", "ne", "is_null", "not_null", "in", "gt", "gte", "lt", "lte"}:
            # String coercion for `contains` is intentionally left to the original
            # Python implementation because Arrow casting would change semantics.
            loaded = self.load_dataset(reference, current_scope=current_scope)
            _, rows = apply_followup_operation(
                loaded.reference.columns, loaded.rows, operation
            )
            return rows
        if operator == "in" and not isinstance(target, (list, tuple, set)):
            raise InvalidFollowupOperation("in operator requires a list value")

        manifest = self._load_manifest(
            reference, current_scope=current_scope, now=None
        )
        rows: list[dict[str, Any]] = []
        total_bytes = 0
        for part in manifest["parts"]:
            part_size = int(part["byte_size"])
            total_bytes += part_size
            if total_bytes > self.max_dataset_bytes:
                raise DatasetStoreError("Parquet dataset exceeds read size policy")
            payload = self._read_object(part["object_name"], max_bytes=part_size)
            if hashlib.sha256(payload).hexdigest() != part["content_sha256"]:
                raise DatasetStoreError("Parquet part checksum validation failed")
            try:
                table = pq.read_table(
                    pa.BufferReader(payload), columns=list(reference.columns)
                )
                values = table[field_name]
                if operator == "is_null":
                    mask = pc.is_null(values)
                elif operator == "not_null":
                    mask = pc.is_valid(values)
                elif operator == "eq" and target is None:
                    mask = pc.is_null(values)
                elif operator == "ne" and target is None:
                    mask = pc.is_valid(values)
                elif operator == "in":
                    value_set = pa.array(list(target), type=values.type)
                    mask = pc.is_in(values, value_set=value_set)
                    # Python considers None a member when explicitly requested.
                    null_match = None in target
                    mask = pc.fill_null(mask, null_match)
                else:
                    scalar = pa.scalar(target, type=values.type)
                    function = {
                        "eq": pc.equal,
                        "ne": pc.not_equal,
                        "gt": pc.greater,
                        "gte": pc.greater_equal,
                        "lt": pc.less,
                        "lte": pc.less_equal,
                    }[operator]
                    mask = function(values, scalar)
                    # Preserve `_compare`: None != a non-null target is true;
                    # all other comparisons against None are false.
                    mask = pc.fill_null(mask, operator == "ne" and target is not None)
                rows.extend(table.filter(mask).to_pylist())
            except InvalidFollowupOperation:
                raise
            except (TypeError, ValueError, pa.ArrowException) as exc:
                raise InvalidFollowupOperation(
                    "filter values have incompatible types"
                ) from exc
        return rows

    def _read_projected_rows(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        columns: Sequence[str],
        row_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        pa, pq = self._pyarrow()
        manifest = self._load_manifest(
            reference, current_scope=current_scope, now=None
        )
        if row_limit == 0:
            return []
        rows: list[dict[str, Any]] = []
        total_bytes = 0
        for part in manifest["parts"]:
            part_size = int(part["byte_size"])
            total_bytes += part_size
            if total_bytes > self.max_dataset_bytes:
                raise DatasetStoreError("Parquet dataset exceeds read size policy")
            payload = self._read_object(part["object_name"], max_bytes=part_size)
            if hashlib.sha256(payload).hexdigest() != part["content_sha256"]:
                raise DatasetStoreError("Parquet part checksum validation failed")
            try:
                table = pq.read_table(pa.BufferReader(payload), columns=list(columns))
                part_rows = table.to_pylist()
            except Exception as exc:
                raise DatasetStoreError("failed to decode projected Parquet columns") from exc
            if row_limit is not None:
                remaining = row_limit - len(rows)
                rows.extend(part_rows[:remaining])
                if len(rows) >= row_limit:
                    break
            else:
                rows.extend(part_rows)
        if row_limit is None and len(rows) != reference.row_count:
            raise DatasetStoreError("Parquet projected row count validation failed")
        return rows

    def delete_dataset(
        self, reference: DatasetReference, *, current_scope: DatasetScope
    ) -> None:
        # Expiry is the reason the lifecycle cleaner calls this method, so deletion
        # validates scope/path/manifest but deliberately does not reject expiry.
        manifest = self._load_manifest(
            reference,
            current_scope=current_scope,
            now=None,
            check_expiry=False,
        )
        for part in manifest["parts"]:
            self.client.remove_object(self.bucket, part["object_name"])
        self.client.remove_object(self.bucket, reference.object_name)


class HybridMinioFollowupStore:
    """Route small datasets to JSON, medium datasets to Parquet, reject large ones."""

    def __init__(
        self,
        json_store: MinioFollowupStore,
        parquet_store: ParquetMinioFollowupStore,
        *,
        small_max_rows: int,
        small_max_bytes: int,
        medium_max_rows: int,
        medium_max_bytes: int,
    ) -> None:
        self.json_store = json_store
        self.parquet_store = parquet_store
        self.small_max_rows = small_max_rows
        self.small_max_bytes = small_max_bytes
        self.medium_max_rows = medium_max_rows
        self.medium_max_bytes = medium_max_bytes
        self.bucket = json_store.bucket

    def check_ready(self) -> None:
        self.json_store.check_ready()
        self.parquet_store.check_ready()

    def save_dataset(self, **kwargs) -> DatasetReference:
        source_rows = kwargs["rows"]
        # SQL adapters normally already return a list/tuple. Reuse it instead of
        # doubling peak memory merely to classify the storage route.
        rows = source_rows if isinstance(source_rows, Sequence) else list(source_rows)
        estimated = estimate_dataset_json_bytes(kwargs["columns"], rows)
        route = choose_dataset_storage_route(
            row_count=len(rows),
            estimated_uncompressed_bytes=estimated,
            small_max_rows=self.small_max_rows,
            small_max_bytes=self.small_max_bytes,
            medium_max_rows=self.medium_max_rows,
            medium_max_bytes=self.medium_max_bytes,
        )
        kwargs["rows"] = rows
        if route is DatasetStorageRoute.SMALL_JSON_GZIP:
            return self.json_store.save_dataset(**kwargs)
        if route is DatasetStorageRoute.MEDIUM_PARQUET:
            return self.parquet_store.save_dataset(**kwargs)
        raise DatasetStoreError(
            "dataset exceeds medium-data policy; keep it in the query engine"
        )

    def _store_for(self, reference: DatasetReference):
        if reference.storage_type == "MINIO_PARQUET":
            return self.parquet_store
        if reference.storage_type == "MINIO_JSON_GZIP":
            return self.json_store
        raise DatasetStoreError(f"unsupported dataset storage type: {reference.storage_type}")

    def load_dataset(self, reference: DatasetReference, **kwargs) -> LoadedDataset:
        return self._store_for(reference).load_dataset(reference, **kwargs)

    def execute_followup(
        self,
        reference: DatasetReference,
        *,
        current_scope: DatasetScope,
        operation: Mapping[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        preview_rows: int = DEFAULT_PREVIEW_ROWS,
    ) -> FollowupResult:
        if reference.storage_type == "MINIO_PARQUET":
            columns, rows = self.parquet_store.read_for_followup(
                reference,
                current_scope=current_scope,
                operation=operation,
            )
        else:
            loaded = self.load_dataset(reference, current_scope=current_scope)
            columns, rows = apply_followup_operation(
                loaded.reference.columns, loaded.rows, operation
            )
        derived = self.save_dataset(
            scope=current_scope,
            columns=columns,
            rows=rows,
            snapshot_id=reference.snapshot_id,
            data_as_of=_parse_datetime(reference.data_as_of, "data_as_of"),
            source_type="DERIVED_DATASET",
            source_ref=reference.dataset_id,
            ttl_seconds=ttl_seconds,
            parent_dataset_ids=(reference.dataset_id,),
            transformation_log=(*reference.transformation_log, dict(operation)),
            semantic_model_id=reference.semantic_model_id,
            business_domain_ids=reference.business_domain_ids,
            metric_ids=reference.metric_ids,
        )
        return FollowupResult(
            derived, tuple(rows[: max(0, min(preview_rows, 10_000))])
        )

    def join_datasets(
        self,
        references: Sequence[DatasetReference],
        *,
        current_scope: DatasetScope,
        join_keys: Sequence[str],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        preview_rows: int = DEFAULT_PREVIEW_ROWS,
    ) -> FollowupResult:
        return _join_with_store(
            self, references, current_scope=current_scope, join_keys=join_keys,
            ttl_seconds=ttl_seconds, preview_rows=preview_rows,
        )

    def delete_dataset(self, reference: DatasetReference, **kwargs) -> None:
        self._store_for(reference).delete_dataset(reference, **kwargs)


def _join_with_store(
    store: Any,
    references: Sequence[DatasetReference],
    *,
    current_scope: DatasetScope,
    join_keys: Sequence[str],
    ttl_seconds: int,
    preview_rows: int,
) -> FollowupResult:
    """Perform a bounded, auditable inner join without allowing many-to-many explosion."""
    if not 2 <= len(references) <= 5:
        raise InvalidFollowupOperation("join requires two to five datasets")
    keys = tuple(dict.fromkeys(str(key).strip() for key in join_keys if str(key).strip()))
    if not keys:
        raise InvalidFollowupOperation("join keys must be explicit and non-empty")
    for ref in references:
        if (
            ref.scope.tenant_id != current_scope.tenant_id
            or ref.scope.user_id != current_scope.user_id
            or ref.scope.application_id != current_scope.application_id
            or ref.scope.authorized_semantic_scope_fingerprint != current_scope.authorized_semantic_scope_fingerprint
        ):
            raise DatasetScopeMismatch("joined datasets must belong to the same tenant, user and application")
    # Branch datasets intentionally have distinct internal conversation ids.
    # Their signed references are loaded in their original scopes and the joined
    # result is materialized into the root DAG scope.
    loaded = [store.load_dataset(ref, current_scope=ref.scope) for ref in references]
    for item in loaded:
        missing = set(keys) - set(item.reference.columns)
        if missing:
            raise InvalidFollowupOperation(f"join keys not found: {sorted(missing)}")

    columns = list(loaded[0].reference.columns)
    rows = [dict(row) for row in loaded[0].rows]
    join_steps: list[dict[str, Any]] = []
    for dataset_index, right in enumerate(loaded[1:], start=2):
        right_index: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in right.rows:
            key = tuple(row.get(name) for name in keys)
            if any(value is None for value in key):
                continue
            if key in right_index:
                raise InvalidFollowupOperation(
                    "join rejected because the right dataset is not unique on the join keys"
                )
            right_index[key] = dict(row)
        additions: list[tuple[str, str]] = []
        for column in right.reference.columns:
            if column in keys:
                continue
            output = column if column not in columns else f"{column}__ds{dataset_index}"
            additions.append((column, output))
            columns.append(output)
        merged: list[dict[str, Any]] = []
        left_before = len(rows)
        for left in rows:
            key = tuple(left.get(name) for name in keys)
            match = right_index.get(key)
            if match is None:
                continue
            value = dict(left)
            value.update({output: match.get(source) for source, output in additions})
            merged.append(value)
        rows = merged
        join_steps.append(
            {
                "right_dataset_id": right.reference.dataset_id,
                "left_rows_before": left_before,
                "right_rows": len(right.rows),
                "right_unique_non_null_keys": len(right_index),
                "matched_rows": len(rows),
                "unmatched_left_rows": left_before - len(rows),
                "left_match_rate": (len(rows) / left_before) if left_before else 0.0,
            }
        )

    if not rows:
        raise InvalidFollowupOperation(
            "join produced zero rows; verify the selected key and source scopes"
        )

    source_times = [
        _parse_datetime(item.reference.data_as_of, "data_as_of") for item in loaded
    ]
    # A joined fact is only as fresh as its oldest required input.
    conservative_as_of = min(source_times)
    transformation = {
        "type": "INNER_JOIN",
        "join_keys": list(keys),
        "cardinality_guard": "RIGHT_UNIQUE_EACH_STEP",
        "source_row_counts": {
            item.reference.dataset_id: item.reference.row_count for item in loaded
        },
        "source_data_as_of": {
            item.reference.dataset_id: item.reference.data_as_of for item in loaded
        },
        "snapshot_skew_seconds": (
            max(source_times) - min(source_times)
        ).total_seconds(),
        "steps": join_steps,
        "result_row_count": len(rows),
    }
    reference = store.save_dataset(
        scope=current_scope,
        columns=columns,
        rows=rows,
        snapshot_id=hashlib.sha256(
            "\x1f".join(item.reference.snapshot_id for item in loaded).encode()
        ).hexdigest(),
        data_as_of=conservative_as_of,
        source_type="JOINED_DATASET",
        source_ref=",".join(item.reference.dataset_id for item in loaded),
        semantic_model_id=(
            next(iter({item.reference.semantic_model_id for item in loaded}))
            if len({item.reference.semantic_model_id for item in loaded}) == 1
            else None
        ),
        business_domain_ids=tuple(sorted({
            domain_id
            for item in loaded
            for domain_id in item.reference.business_domain_ids
        })),
        metric_ids=tuple(sorted({
            metric_id
            for item in loaded
            for metric_id in item.reference.metric_ids
        })),
        ttl_seconds=ttl_seconds,
        parent_dataset_ids=tuple(item.reference.dataset_id for item in loaded),
        transformation_log=(transformation,),
    )
    return FollowupResult(reference, tuple(rows[: max(0, min(preview_rows, 10_000))]))


def apply_followup_operation(
    columns: Sequence[str],
    source_rows: Sequence[Mapping[str, Any]],
    operation: Mapping[str, Any],
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """Pure, deterministic and eval-free follow-up transformation engine."""
    op = str(operation.get("type", "")).lower()
    rows = [dict(row) for row in source_rows]
    declared = tuple(columns)

    if op == "pipeline":
        operations = operation.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 5:
            raise InvalidFollowupOperation(
                "pipeline operations must be a list containing between 1 and 5 operations"
            )
        current_columns = declared
        current_rows = rows
        for item in operations:
            if not isinstance(item, Mapping) or str(item.get("type", "")).lower() == "pipeline":
                raise InvalidFollowupOperation("nested or invalid pipeline operation")
            current_columns, current_rows = apply_followup_operation(
                current_columns, current_rows, item
            )
        return current_columns, current_rows

    if op == "filter":
        field_name = _require_column(operation.get("field"), declared)
        operator = str(operation.get("operator", "eq")).lower()
        target = operation.get("value")
        rows = [row for row in rows if _compare(row.get(field_name), operator, target)]
        return declared, rows

    if op == "select":
        selected = tuple(_require_column(value, declared) for value in operation.get("columns", ()))
        if not selected or len(selected) != len(set(selected)):
            raise InvalidFollowupOperation("select columns must be non-empty and unique")
        return selected, [{key: row.get(key) for key in selected} for row in rows]

    if op == "sort":
        field_name = _require_column(operation.get("field"), declared)
        descending = bool(operation.get("descending", False))
        non_null = [row for row in rows if row.get(field_name) is not None]
        null_rows = [row for row in rows if row.get(field_name) is None]
        values = [row[field_name] for row in non_null]
        numeric_sort = bool(values) and all(_numeric_or_none(value) is not None for value in values)
        try:
            non_null.sort(
                key=(
                    (lambda row: _numeric_or_none(row[field_name]))
                    if numeric_sort
                    else (lambda row: row[field_name])
                ),
                reverse=descending,
            )
        except TypeError as exc:
            raise InvalidFollowupOperation("sort field contains incompatible value types") from exc
        return declared, non_null + null_rows

    if op == "sort_limit":
        sorted_columns, sorted_rows = apply_followup_operation(
            declared,
            rows,
            {
                "type": "sort",
                "field": operation.get("field"),
                "descending": operation.get("descending", False),
            },
        )
        return apply_followup_operation(
            sorted_columns,
            sorted_rows,
            {"type": "limit", "count": operation.get("count")},
        )

    if op == "limit":
        limit = operation.get("count")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100_000:
            raise InvalidFollowupOperation("limit count must be an integer between 0 and 100000")
        return declared, rows[:limit]

    if op == "extrema":
        field_name = _require_column(operation.get("field"), declared)
        numeric_rows = [
            (row, _numeric_or_none(row.get(field_name)))
            for row in rows
            if _numeric_or_none(row.get(field_name)) is not None
        ]
        if not numeric_rows:
            return (*declared, "极值类型"), []
        minimum = min(numeric_rows, key=lambda item: item[1])[0]
        maximum = max(numeric_rows, key=lambda item: item[1])[0]
        result_columns = (*declared, "极值类型")
        result_rows = [
            {**maximum, "极值类型": "最高"},
            {**minimum, "极值类型": "最低"},
        ]
        if bool(operation.get("include_difference", False)):
            result_field = str(operation.get("result_field") or f"{field_name}差额").strip()
            if not result_field or result_field in result_columns or len(result_field) > 128:
                raise InvalidFollowupOperation("result_field must be a new non-empty column")
            difference = max(item[1] for item in numeric_rows) - min(item[1] for item in numeric_rows)
            result_columns = (*result_columns, result_field)
            result_rows = [{**row, result_field: difference} for row in result_rows]
        return result_columns, result_rows

    if op == "extrema_difference":
        field_name = _require_column(operation.get("field"), declared)
        result_field = str(operation.get("result_field") or f"{field_name}差额").strip()
        if not result_field or result_field in declared or len(result_field) > 128:
            raise InvalidFollowupOperation("result_field must be a new non-empty column")
        values = [
            value for row in rows
            if (value := _numeric_or_none(row.get(field_name))) is not None
        ]
        if len(values) < 2:
            raise InvalidFollowupOperation(
                "extrema_difference requires at least two numeric rows"
            )
        return (result_field,), [{result_field: max(values) - min(values)}]

    if op == "derive":
        left = _require_column(operation.get("left_field"), declared)
        right = _require_column(operation.get("right_field"), declared)
        result_field = str(operation.get("result_field", "")).strip()
        if not result_field or result_field in declared or len(result_field) > 128:
            raise InvalidFollowupOperation("result_field must be a new non-empty column")
        arithmetic = str(operation.get("operator", "")).lower()
        if arithmetic not in {"add", "subtract", "multiply", "divide"}:
            raise InvalidFollowupOperation("derive operator must be add/subtract/multiply/divide")
        for row in rows:
            row[result_field] = _arithmetic(row.get(left), row.get(right), arithmetic)
        return (*declared, result_field), rows

    if op == "aggregate":
        group_by = tuple(
            _require_column(value, declared) for value in operation.get("group_by", ())
        )
        specs = operation.get("aggregations")
        if not isinstance(specs, list) or not specs:
            raise InvalidFollowupOperation("aggregations must be a non-empty list")
        output_names = list(group_by)
        validated_specs: list[tuple[str, str, str]] = []
        for spec in specs:
            if not isinstance(spec, Mapping):
                raise InvalidFollowupOperation("each aggregation must be an object")
            function = str(spec.get("function", "")).lower()
            source = _require_column(spec.get("field"), declared)
            alias = str(spec.get("alias") or f"{function}_{source}").strip()
            if function not in {"count", "sum", "avg", "min", "max"}:
                raise InvalidFollowupOperation("unsupported aggregation function")
            if not alias or alias in output_names:
                raise InvalidFollowupOperation("aggregation aliases must be non-empty and unique")
            output_names.append(alias)
            validated_specs.append((function, source, alias))
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(tuple(row.get(key) for key in group_by), []).append(row)
        if not group_by and not grouped:
            grouped[()] = []
        output: list[dict[str, Any]] = []
        for key, group_rows in grouped.items():
            result = dict(zip(group_by, key))
            for function, source, alias in validated_specs:
                result[alias] = _aggregate(group_rows, source, function)
            output.append(result)
        return tuple(output_names), output

    raise InvalidFollowupOperation(
        "operation type must be pipeline/filter/select/sort/sort_limit/limit/derive/aggregate/extrema/extrema_difference"
    )


def _require_column(value: Any, columns: Sequence[str]) -> str:
    name = str(value or "").strip()
    if name not in columns:
        raise InvalidFollowupOperation(f"unknown column: {name!r}")
    return name


def _compare(actual: Any, operator: str, target: Any) -> bool:
    if operator == "eq":
        return actual == target
    if operator == "ne":
        return actual != target
    if operator == "is_null":
        return actual is None
    if operator == "not_null":
        return actual is not None
    if operator == "in":
        if not isinstance(target, (list, tuple, set)):
            raise InvalidFollowupOperation("in operator requires a list value")
        return actual in target
    if operator == "contains":
        return actual is not None and str(target) in str(actual)
    if operator in {"gt", "gte", "lt", "lte"}:
        if actual is None:
            return False
        try:
            return {
                "gt": actual > target,
                "gte": actual >= target,
                "lt": actual < target,
                "lte": actual <= target,
            }[operator]
        except TypeError as exc:
            raise InvalidFollowupOperation("filter values have incompatible types") from exc
    raise InvalidFollowupOperation(f"unsupported filter operator: {operator}")


def _as_number(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise InvalidFollowupOperation("arithmetic fields must contain numbers")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise InvalidFollowupOperation("arithmetic fields must contain numbers") from exc
    if not number.is_finite():
        raise InvalidFollowupOperation("arithmetic result must be finite")
    return number


def _numeric_or_none(value: Any) -> Decimal | None:
    try:
        return _as_number(value)
    except InvalidFollowupOperation:
        return None


def _arithmetic(left: Any, right: Any, operator: str) -> float | None:
    if left is None or right is None:
        return None
    first, second = _as_number(left), _as_number(right)
    if operator == "divide" and second == 0:
        return None
    result = {
        "add": lambda: first + second,
        "subtract": lambda: first - second,
        "multiply": lambda: first * second,
        "divide": lambda: first / second,
    }[operator]()
    return float(result)


def _aggregate(rows: Sequence[Mapping[str, Any]], field_name: str, function: str) -> Any:
    values = [row.get(field_name) for row in rows if row.get(field_name) is not None]
    if function == "count":
        return len(values)
    if not values:
        return None
    if function in {"min", "max"}:
        try:
            return (min if function == "min" else max)(values)
        except TypeError as exc:
            raise InvalidFollowupOperation("aggregation field contains incompatible types") from exc
    numbers = [_as_number(value) for value in values]
    result = sum(numbers, Decimal(0))
    if function == "avg":
        result /= len(numbers)
    return float(result)
