import pytest

from analysis_contract import normalize_contract, normalize_result, validate_result


CONTRACT = {
    "operator": "price_volume_decomposition",
    "required_columns": {
        "period_role": ["period_role", "期间角色"],
        "price": ["price", "单价"],
        "quantity": ["quantity", "销量"],
    },
    "minimum_rows": 2,
    "maximum_rows": 2,
}


def test_multidimensional_attribution_contract_is_accepted():
    contract = normalize_contract({
        "operator": "multidimensional_attribution",
        "required_columns": {
            "dimension_name": ["dimension_name"],
            "element_value": ["element_value"],
            "baseline": ["baseline"],
            "current": ["current"],
        },
        "minimum_rows": 4,
        "instructions": ["各维度总量必须一致"],
    })

    assert contract["operator"] == "multidimensional_attribution"
    assert contract["minimum_rows"] == 4


def test_sql_boundary_proves_satisfied_contract():
    proof = validate_result(CONTRACT, ["period_role", "单价", "销量"], [
        {"period_role": "BASE", "单价": 10, "销量": 100},
        {"period_role": "CURRENT", "单价": 12, "销量": 110},
    ])
    assert proof["contract_satisfied"] is True
    assert proof["producer"] == "SQL_TRANSLATOR"
    assert proof["resolved_columns"] == {
        "period_role": "period_role", "price": "单价", "quantity": "销量"
    }


def test_sql_boundary_reports_shape_and_type_violations():
    proof = validate_result(CONTRACT, ["price", "quantity"], [
        {"price": "bad", "quantity": 1},
    ])
    assert proof["contract_satisfied"] is False
    assert proof["violations"]["missing_roles"] == ["period_role"]
    assert proof["violations"]["invalid_numeric_roles"] == ["price"]
    assert proof["violations"]["row_count_error"]


def test_sql_boundary_rejects_unknown_operator():
    with pytest.raises(ValueError, match="operator"):
        normalize_contract({**CONTRACT, "operator": "execute_python"})


def test_price_volume_detail_rows_are_normalized_into_two_months():
    columns, rows, proof = normalize_result(
        CONTRACT,
        ["paid_at", "unit_price", "quantity"],
        [
            {"paid_at": "2026-07-01", "unit_price": 10, "quantity": 2},
            {"paid_at": "2026-07-20", "unit_price": 20, "quantity": 1},
            {"paid_at": "2026-08-01", "unit_price": 12, "quantity": 2},
            {"paid_at": "2026-08-20", "unit_price": 18, "quantity": 2},
        ],
    )
    assert columns == ["period_role", "price", "quantity"]
    assert rows[0] == pytest.approx({"period_role": "BASE", "price": 40 / 3, "quantity": 3})
    assert rows[1] == pytest.approx({"period_role": "CURRENT", "price": 15, "quantity": 4})
    assert proof["applied"] is True
    assert proof["periods"] == ["2026-07", "2026-08"]
    assert validate_result(CONTRACT, columns, rows)["contract_satisfied"] is True


@pytest.mark.parametrize(
    ("columns", "rows", "reason"),
    [
        (
            ["paid_at", "price", "quantity"],
            [
                {"paid_at": "2026-06-01", "price": 10, "quantity": 1},
                {"paid_at": "2026-07-01", "price": 10, "quantity": 1},
                {"paid_at": "2026-08-01", "price": 10, "quantity": 1},
            ],
            "expected_exactly_two_months",
        ),
        (
            ["paid_at", "created_date", "price", "quantity"],
            [
                {"paid_at": "2026-07-01", "created_date": "2026-07-01", "price": 10, "quantity": 1},
                {"paid_at": "2026-08-01", "created_date": "2026-08-01", "price": 10, "quantity": 1},
            ],
            "temporal_column_ambiguous",
        ),
        (
            ["paid_at", "price", "quantity"],
            [
                {"paid_at": "2026-07-01", "price": None, "quantity": 1},
                {"paid_at": "2026-08-01", "price": 10, "quantity": 1},
            ],
            "invalid_time_or_numeric_value",
        ),
    ],
)
def test_price_volume_normalization_rejects_unsafe_inputs(columns, rows, reason):
    output_columns, output_rows, proof = normalize_result(CONTRACT, columns, rows)
    assert output_columns == columns
    assert output_rows == rows
    assert proof["applied"] is False
    assert proof["reason"] == reason
