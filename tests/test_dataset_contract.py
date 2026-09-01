from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.models import Dataset


def valid(**updates):
    values = {
        "columns": ["月份", "销售额"],
        "rows": [{"月份": "2026-01", "销售额": 10}],
        "row_count": 1,
        "snapshot_id": "s1",
        "data_as_of": datetime.now(timezone.utc),
    }
    values.update(updates)
    return Dataset(**values)


def test_dataset_normalizes_quality_status() -> None:
    assert valid(quality_status=" pass ").quality_status == "PASS"


def test_dataset_accepts_valid_optional_source_watermark() -> None:
    dataset = valid(
        source_data_as_of=datetime(2025, 12, 30, 23, 59, 58),
        source_watermark_field="sales_order.created_date",
    )
    assert dataset.source_data_as_of == datetime(2025, 12, 30, 23, 59, 58)
    assert dataset.source_watermark_field == "sales_order.created_date"


@pytest.mark.parametrize(
    "updates",
    [
        {"source_data_as_of": datetime(2025, 12, 30)},
        {"source_watermark_field": "sales_order.created_date"},
        {
            "source_data_as_of": datetime(2025, 12, 30),
            "source_watermark_field": "sales_order.created_date;DROP TABLE x",
        },
    ],
)
def test_dataset_rejects_partial_or_unsafe_source_watermark(updates) -> None:
    with pytest.raises(ValidationError, match="source_"):
        valid(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"columns": ["销售额", "销售额"]},
        {"columns": ["月份", " "]},
        {"row_count": 2},
        {"rows": [{"月份": "2026-01", "销售额": 10, "secret": "x"}]},
        {"snapshot_id": ""},
        {"data_as_of": datetime.now()},
    ],
)
def test_dataset_rejects_invalid_contract(updates) -> None:
    with pytest.raises(ValidationError):
        valid(**updates)


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")],
)
def test_dataset_rejects_non_finite_numbers(bad_value) -> None:
    with pytest.raises(ValidationError, match="finite|NaN|Infinity"):
        valid(rows=[{"月份": "2026-01", "销售额": bad_value}])
