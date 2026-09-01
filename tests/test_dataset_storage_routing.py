import pytest

from minio_followup_store import (
    DatasetStorageRoute,
    choose_dataset_storage_route,
    estimate_dataset_json_bytes,
)


def test_small_dataset_uses_bounded_json_adapter():
    assert choose_dataset_storage_route(
        row_count=100, estimated_uncompressed_bytes=100_000
    ) is DatasetStorageRoute.SMALL_JSON_GZIP


def test_either_dimension_can_promote_to_parquet():
    assert choose_dataset_storage_route(
        row_count=10_001, estimated_uncompressed_bytes=100_000
    ) is DatasetStorageRoute.MEDIUM_PARQUET
    assert choose_dataset_storage_route(
        row_count=100, estimated_uncompressed_bytes=6 * 1024 * 1024
    ) is DatasetStorageRoute.MEDIUM_PARQUET


def test_large_dataset_stays_in_query_engine():
    assert choose_dataset_storage_route(
        row_count=1_000_001, estimated_uncompressed_bytes=10
    ) is DatasetStorageRoute.LARGE_QUERY_ENGINE
    assert choose_dataset_storage_route(
        row_count=10, estimated_uncompressed_bytes=300 * 1024 * 1024
    ) is DatasetStorageRoute.LARGE_QUERY_ENGINE


def test_invalid_size_is_rejected():
    with pytest.raises(ValueError):
        choose_dataset_storage_route(row_count=-1, estimated_uncompressed_bytes=0)


def test_estimate_counts_unicode_payload_bytes():
    size = estimate_dataset_json_bytes(["地区"], [{"地区": "华东"}])
    assert size >= len("地区华东".encode("utf-8"))
