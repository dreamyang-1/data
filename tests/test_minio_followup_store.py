import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services.dataset_followup import (
    is_dataset_operation_followup,
    plan_dataset_followup,
)
from minio_followup_store import (
    DatasetExpired,
    InvalidFollowupOperation,
    DatasetScope,
    DatasetScopeMismatch,
    HybridMinioFollowupStore,
    MinioFollowupStore,
    ParquetMinioFollowupStore,
    apply_followup_operation,
)


class _Response(io.BytesIO):
    def release_conn(self):
        pass


class _Stat:
    def __init__(self, size):
        self.size = size


class FakeMinio:
    def __init__(self):
        self.objects = {}
        self.get_calls = []

    def bucket_exists(self, _bucket):
        return True

    def put_object(self, bucket, name, data, length, **_kwargs):
        payload = data.read()
        assert len(payload) == length
        self.objects[(bucket, name)] = payload

    def stat_object(self, bucket, name):
        return _Stat(len(self.objects[(bucket, name)]))

    def get_object(self, bucket, name):
        self.get_calls.append((bucket, name))
        return _Response(self.objects[(bucket, name)])

    def remove_object(self, bucket, name):
        self.objects.pop((bucket, name), None)


def _base_dataset():
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket="test-bucket")
    scope = DatasetScope("tenant", "user", "app", "conversation")
    reference = store.save_dataset(
        scope=scope,
        columns=["地区", "销售额"],
        rows=[
            {"地区": "华东", "销售额": 100},
            {"地区": "华南", "销售额": 80},
            {"地区": "华东", "销售额": 120},
        ],
        snapshot_id="snapshot-1",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-1",
    )
    return client, store, scope, reference


def test_save_load_and_derive_are_immutable():
    client, store, scope, source = _base_dataset()
    derived = store.execute_followup(
        source,
        current_scope=scope,
        operation={"type": "filter", "field": "地区", "operator": "eq", "value": "华东"},
    )
    assert source.dataset_id != derived.reference.dataset_id
    assert store.load_dataset(source, current_scope=scope).reference.row_count == 3
    assert derived.reference.row_count == 2
    assert len(client.objects) == 2


def test_cross_branch_join_materializes_lineage_and_rejects_many_to_many():
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket="test-bucket")
    root = DatasetScope("tenant", "user", "app", "root")
    left_scope = DatasetScope("tenant", "user", "app", "dag-left")
    right_scope = DatasetScope("tenant", "user", "app", "dag-right")
    common = dict(snapshot_id="s", data_as_of=datetime.now(timezone.utc), source_type="DATABASE_QUERY")
    left = store.save_dataset(
        scope=left_scope, columns=["supplier_id", "amount"],
        rows=[{"supplier_id": "s1", "amount": 10}, {"supplier_id": "s2", "amount": 20}],
        source_ref="q1", **common,
    )
    right = store.save_dataset(
        scope=right_scope, columns=["supplier_id", "name"],
        rows=[{"supplier_id": "s1", "name": "A"}, {"supplier_id": "s2", "name": "B"}],
        source_ref="q2", **common,
    )
    joined = store.join_datasets([left, right], current_scope=root, join_keys=["supplier_id"])
    assert joined.reference.parent_dataset_ids == (left.dataset_id, right.dataset_id)
    assert joined.reference.row_count == 2
    assert joined.preview_rows[0] == {"supplier_id": "s1", "amount": 10, "name": "A"}
    audit = joined.reference.transformation_log[-1]
    assert audit["steps"][0]["left_match_rate"] == 1.0
    assert audit["result_row_count"] == 2

    duplicate_right = store.save_dataset(
        scope=right_scope, columns=["supplier_id", "name"],
        rows=[{"supplier_id": "s1", "name": "A"}, {"supplier_id": "s1", "name": "A2"}],
        source_ref="q3", **common,
    )
    with pytest.raises(InvalidFollowupOperation, match="not unique"):
        store.join_datasets([left, duplicate_right], current_scope=root, join_keys=["supplier_id"])

    no_match = store.save_dataset(
        scope=right_scope, columns=["supplier_id", "name"],
        rows=[{"supplier_id": "other", "name": "X"}], source_ref="q4", **common,
    )
    with pytest.raises(InvalidFollowupOperation, match="zero rows"):
        store.join_datasets([left, no_match], current_scope=root, join_keys=["supplier_id"])


def test_scope_and_expiry_are_enforced():
    _, store, scope, reference = _base_dataset()
    with pytest.raises(DatasetScopeMismatch):
        store.load_dataset(
            reference,
            current_scope=DatasetScope("tenant", "another-user", "app", "conversation"),
        )
    with pytest.raises(DatasetExpired):
        store.load_dataset(
            reference,
            current_scope=scope,
            now=datetime.now(timezone.utc) + timedelta(days=8),
        )


def test_followup_planner_reuses_only_unambiguous_dataset_operations():
    columns = ["地区", "销售额"]
    rows = [{"地区": "华东", "销售额": 100}, {"地区": "华南", "销售额": 80}]
    assert plan_dataset_followup("刚才只看华东", columns, rows) == {
        "type": "filter", "field": "地区", "operator": "eq", "value": "华东"
    }
    assert plan_dataset_followup("为什么下降", columns, rows) is None


def test_numeric_strings_use_numeric_top_n_order():
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket="test-bucket")
    scope = DatasetScope("tenant", "user", "app", "numeric-sort")
    reference = store.save_dataset(
        scope=scope,
        columns=["月份", "销售额"],
        rows=[
            {"月份": "一月", "销售额": "9"},
            {"月份": "二月", "销售额": "100"},
            {"月份": "三月", "销售额": "20"},
        ],
        snapshot_id="snapshot-sort",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-sort",
    )
    operation = plan_dataset_followup(
        "刚才销售额前2名", ["月份", "销售额"], store.load_dataset(reference, current_scope=scope).rows
    )
    assert operation == {
        "type": "sort_limit", "field": "销售额", "descending": True, "count": 2
    }
    result = store.execute_followup(reference, current_scope=scope, operation=operation)
    assert [row["销售额"] for row in result.preview_rows] == ["100", "20"]


def test_pipeline_applies_sheet_filter_before_aggregation():
    columns, rows = apply_followup_operation(
        ["_sheet_name", "销售额"],
        [
            {"_sheet_name": "销售", "销售额": 100},
            {"_sheet_name": "销售", "销售额": 80},
            {"_sheet_name": "预算", "销售额": 120},
        ],
        {
            "type": "pipeline",
            "operations": [
                {"type": "filter", "field": "_sheet_name", "operator": "eq", "value": "销售"},
                {
                    "type": "aggregate",
                    "group_by": [],
                    "aggregations": [
                        {"field": "销售额", "function": "sum", "alias": "销售额合计"}
                    ],
                },
            ],
        },
    )
    assert columns == ("销售额合计",)
    assert rows == [{"销售额合计": 180.0}]


@pytest.mark.asyncio
async def test_session_keeps_references_not_rows():
    from app.stores import InMemorySessionStore

    _, _, scope, reference = _base_dataset()
    sessions = InMemorySessionStore()
    await sessions.put_dataset_reference(reference.to_dict(), recent_limit=5)
    restored = await sessions.get_recent_dataset_references(
        scope.tenant_id,
        scope.user_id,
        scope.application_id,
        scope.conversation_id,
        limit=5,
    )
    assert restored[0]["dataset_id"] == reference.dataset_id
    assert "rows" not in restored[0]


def test_parquet_store_partitions_loads_and_deletes_expired_dataset():
    client = FakeMinio()
    store = ParquetMinioFollowupStore(
        client, bucket="test-bucket", part_rows=2, max_dataset_bytes=1024 * 1024
    )
    scope = DatasetScope("tenant", "user", "app", "parquet-conversation")
    rows = [{"region": f"r-{index}", "sales": index * 10} for index in range(5)]
    reference = store.save_dataset(
        scope=scope,
        columns=["region", "sales"],
        rows=rows,
        snapshot_id="snapshot-parquet",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-parquet",
    )

    assert reference.storage_type == "MINIO_PARQUET"
    assert reference.object_name.endswith("/manifest.json")
    manifest = json.loads(client.objects[(reference.bucket, reference.object_name)])
    assert [part["row_count"] for part in manifest["parts"]] == [2, 2, 1]
    assert list(store.load_dataset(reference, current_scope=scope).rows) == rows

    expired = replace(
        reference,
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    # The trusted Redis reference can be expired when the cleaner sees it. Its
    # immutable object path is still sufficient for scope-safe deletion.
    manifest["reference"] = expired.to_dict()
    client.objects[(expired.bucket, expired.object_name)] = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    store.delete_dataset(expired, current_scope=scope)
    assert not any(expired.dataset_id in name for _, name in client.objects)


def test_parquet_checksum_corruption_is_rejected():
    client = FakeMinio()
    store = ParquetMinioFollowupStore(client, bucket="test-bucket", part_rows=2)
    scope = DatasetScope("tenant", "user", "app", "checksum-conversation")
    reference = store.save_dataset(
        scope=scope,
        columns=["value"],
        rows=[{"value": 1}, {"value": 2}, {"value": 3}],
        snapshot_id="snapshot-checksum",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-checksum",
    )
    manifest = json.loads(client.objects[(reference.bucket, reference.object_name)])
    first_part = manifest["parts"][0]["object_name"]
    original = client.objects[(reference.bucket, first_part)]
    client.objects[(reference.bucket, first_part)] = bytes([original[0] ^ 1]) + original[1:]
    with pytest.raises(Exception, match="checksum"):
        store.load_dataset(reference, current_scope=scope)


def test_hybrid_store_routes_small_json_and_medium_parquet():
    client = FakeMinio()
    json_store = MinioFollowupStore(client, bucket="test-bucket")
    parquet_store = ParquetMinioFollowupStore(
        client, bucket="test-bucket", part_rows=2, max_dataset_bytes=1024 * 1024
    )
    store = HybridMinioFollowupStore(
        json_store,
        parquet_store,
        small_max_rows=2,
        small_max_bytes=1024,
        medium_max_rows=10,
        medium_max_bytes=1024 * 1024,
    )
    scope = DatasetScope("tenant", "user", "app", "hybrid-conversation")
    common = {
        "scope": scope,
        "columns": ["value"],
        "snapshot_id": "snapshot-hybrid",
        "data_as_of": datetime.now(timezone.utc),
        "source_type": "DATABASE_QUERY",
        "source_ref": "query-hybrid",
    }
    small = store.save_dataset(rows=[{"value": 1}], **common)
    medium = store.save_dataset(
        rows=[{"value": 1}, {"value": 2}, {"value": 3}], **common
    )
    assert small.storage_type == "MINIO_JSON_GZIP"
    assert medium.storage_type == "MINIO_PARQUET"
    assert len(store.load_dataset(medium, current_scope=scope).rows) == 3


def test_parquet_limit_pushdown_stops_after_enough_parts_and_routes_derived_small():
    client = FakeMinio()
    store = HybridMinioFollowupStore(
        MinioFollowupStore(client, bucket="test-bucket"),
        ParquetMinioFollowupStore(
            client, bucket="test-bucket", part_rows=2, max_dataset_bytes=1024 * 1024
        ),
        small_max_rows=2,
        small_max_bytes=1024,
        medium_max_rows=100,
        medium_max_bytes=1024 * 1024,
    )
    scope = DatasetScope("tenant", "user", "app", "pushdown-conversation")
    source = store.save_dataset(
        scope=scope,
        columns=["region", "sales", "unused"],
        rows=[
            {"region": f"r-{index}", "sales": index, "unused": "x" * 20}
            for index in range(6)
        ],
        snapshot_id="snapshot-pushdown",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-pushdown",
    )
    assert source.storage_type == "MINIO_PARQUET"
    client.get_calls.clear()

    result = store.execute_followup(
        source,
        current_scope=scope,
        operation={"type": "limit", "count": 1},
    )

    # One Manifest and only the first of three Parquet parts are read.
    assert len(client.get_calls) == 2
    assert result.reference.storage_type == "MINIO_JSON_GZIP"
    assert result.preview_rows == ({"region": "r-0", "sales": 0, "unused": "x" * 20},)


def test_parquet_aggregate_projection_preserves_deterministic_result():
    client = FakeMinio()
    parquet = ParquetMinioFollowupStore(
        client, bucket="test-bucket", part_rows=2, max_dataset_bytes=1024 * 1024
    )
    scope = DatasetScope("tenant", "user", "app", "aggregate-conversation")
    reference = parquet.save_dataset(
        scope=scope,
        columns=["region", "sales", "unused"],
        rows=[
            {"region": "east", "sales": 10, "unused": "a"},
            {"region": "east", "sales": 20, "unused": "b"},
            {"region": "west", "sales": 7, "unused": "c"},
        ],
        snapshot_id="snapshot-aggregate",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-aggregate",
    )
    columns, rows = parquet.read_for_followup(
        reference,
        current_scope=scope,
        operation={
            "type": "aggregate",
            "group_by": ["region"],
            "aggregations": [{"field": "sales", "function": "sum", "alias": "total"}],
        },
    )
    assert columns == ("region", "total")
    assert rows == [{"region": "east", "total": 30.0}, {"region": "west", "total": 7.0}]


@pytest.mark.parametrize(
    "operation",
    [
        {"type": "filter", "field": "sales", "operator": "eq", "value": 20},
        {"type": "filter", "field": "sales", "operator": "ne", "value": 20},
        {"type": "filter", "field": "sales", "operator": "eq", "value": None},
        {"type": "filter", "field": "sales", "operator": "ne", "value": None},
        {"type": "filter", "field": "sales", "operator": "is_null"},
        {"type": "filter", "field": "sales", "operator": "not_null"},
        {"type": "filter", "field": "sales", "operator": "in", "value": [10, None]},
        {"type": "filter", "field": "sales", "operator": "gt", "value": 10},
        {"type": "filter", "field": "region", "operator": "contains", "value": "ast"},
    ],
)
def test_parquet_filter_pushdown_matches_reference_semantics(operation):
    client = FakeMinio()
    parquet = ParquetMinioFollowupStore(
        client, bucket="test-bucket", part_rows=2, max_dataset_bytes=1024 * 1024
    )
    scope = DatasetScope("tenant", "user", "app", "filter-conversation")
    source_rows = [
        {"region": "east", "sales": 10},
        {"region": "east", "sales": 20},
        {"region": "west", "sales": None},
        {"region": "west", "sales": 30},
    ]
    reference = parquet.save_dataset(
        scope=scope,
        columns=["region", "sales"],
        rows=source_rows,
        snapshot_id="snapshot-filter",
        data_as_of=datetime.now(timezone.utc),
        source_type="DATABASE_QUERY",
        source_ref="query-filter",
    )
    _, expected = apply_followup_operation(
        reference.columns, source_rows, operation
    )
    columns, actual = parquet.read_for_followup(
        reference, current_scope=scope, operation=operation
    )
    assert columns == reference.columns
    assert actual == expected
def test_extrema_and_difference_followups_preserve_labels_and_calculate_gap():
    columns = ("区域", "销售额")
    rows = (
        {"区域": "华东", "销售额": 120},
        {"区域": "华南", "销售额": 80},
        {"区域": "华北", "销售额": 100},
    )

    extrema = plan_dataset_followup("找出销售额最高和最低区域", columns, rows)
    assert extrema == {"type": "extrema", "field": "销售额"}
    extrema_columns, extrema_rows = apply_followup_operation(columns, rows, extrema)
    assert extrema_columns == ("区域", "销售额", "极值类型")
    assert extrema_rows == [
        {"区域": "华东", "销售额": 120, "极值类型": "最高"},
        {"区域": "华南", "销售额": 80, "极值类型": "最低"},
    ]

    difference = plan_dataset_followup(
        "再算销售额最高比最低高多少", extrema_columns, extrema_rows
    )
    assert difference == {
        "type": "extrema_difference",
        "field": "销售额",
        "result_field": "销售额差额",
    }
    assert apply_followup_operation(extrema_columns, extrema_rows, difference) == (
        ("销售额差额",),
        [{"销售额差额": 40}],
    )


def test_elliptical_month_extrema_is_routed_to_existing_result():
    question = "哪个月最高？比最低月高多少？"
    assert is_dataset_operation_followup(question) is True
    assert plan_dataset_followup(
        question,
        ("交易月份", "含税销售总额"),
        (
            {"交易月份": "2025-10", "含税销售总额": 100},
            {"交易月份": "2025-11", "含税销售总额": 60},
            {"交易月份": "2025-12", "含税销售总额": 80},
        ),
    ) == {
        "type": "extrema",
        "field": "含税销售总额",
        "include_difference": True,
        "result_field": "含税销售总额差额",
    }
