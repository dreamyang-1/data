"""SPRINT-20260917-C (REVISION_2_FINAL_SMALL_FIX): never display unconfirmed candidates.

Production replay shape: the dealer-list query for 血液净化管路 fails on
``商品品类`` while the dependency error simultaneously lists ``产品编码`` /
``产品名称``. Upstream does not currently produce candidate evidence carrying
a business semantic identity, and equal generic roles or storage types prove
shape only, never business meaning. All four candidate-displaying branches
therefore never display candidates; the message keeps the concrete failed
subject and value, states that no available candidate can be confirmed, and
never asserts the catalog is missing (missing metadata can also mean
incomplete recall, binding, or diagnostics). The raw error details stay
untouched throughout.

Adjustment record: the former positive example that expected a same-identity
candidate to be listed was removed. It relied on a ``semantic_role``
equivalence that upstream has never produced as a confirmation contract, so
it is replaced by adversarial checks proving that equal role values
(DIMENSION, unknown, name, category) are not confirmation evidence.
"""

import pytest

from app.adapters.base import AdapterError
from app.services.dependency_error_messages import render_dependency_error


def test_category_failure_does_not_list_adjacent_product_candidates():
    """Bare upstream candidates carry no identity proof; do not pad with neighbors."""
    exc = AdapterError(
        "ASL_FILTER_INVALID",
        "required filter field cannot be uniquely resolved",
        upstream_code="ASL_FILTER_INVALID",
        details={
            "semantic_field": "商品品类",
            "expected_filter": {
                "field": "商品品类",
                "operator": "EQ",
                "value": "血液净化管路",
            },
            # Real Oagnet shape: physical fields without semantic identity.
            "candidates": ["product.product_code", "product.product_name"],
        },
    )

    message = render_dependency_error(exc)

    assert message is not None
    assert "商品品类" in message
    assert "血液净化管路" in message
    assert "产品编码" not in message
    assert "产品名称" not in message
    assert "product_code" not in message
    assert "product_name" not in message
    assert "当前无法确认可用的候选字段" in message
    assert "语义层缺少" not in message
    assert "用户可补充" in message
    assert "语义层需检查" in message
    # Raw diagnostic details remain untouched for operator inspection.
    assert exc.details["candidates"] == [
        "product.product_code", "product.product_name",
    ]


@pytest.mark.parametrize("role", ["FILTER_FIELD", "varchar", "DIMENSION",
                                  "unknown", "name", "category"])
def test_equal_role_or_type_value_is_not_confirmation_evidence(role):
    """REVISION_2: matching semantic_role values never confirm a candidate."""
    exc = AdapterError(
        "ASL_FILTER_INVALID",
        "binding failure",
        upstream_code="ASL_FILTER_INVALID",
        details={
            "expected_filter": {
                "field": "商品品类", "value": "测试品类", "semantic_role": role,
            },
            "candidates": [{"label": "UNRELATED_CANDIDATE", "semantic_role": role}],
        },
    )

    message = render_dependency_error(exc)

    assert message is not None
    assert "UNRELATED_CANDIDATE" not in message
    assert "当前无法确认可用的候选字段" in message
    assert "商品品类" in message


def test_missing_metadata_never_asserts_missing_catalog_configuration():
    """No evidence only proves candidates cannot be confirmed."""
    exc = AdapterError(
        "ASL_DIMENSION_INVALID",
        "binding failure",
        upstream_code="ASL_DIMENSION_INVALID",
        details={
            "missing_dimensions": ["城市"],
            "candidates": ["dim_city.city_name"],
        },
    )

    message = render_dependency_error(exc)

    assert message is not None
    assert "城市" in message
    assert "语义层缺少" not in message
    assert "dim_city" not in message
    assert "当前无法确认可用的候选字段" in message
    assert "仅当存在明确的缺失证据时才能判定为未配置" in message


def test_relationship_anchor_without_identity_is_not_padded():
    exc = AdapterError(
        "ASL_RELATIONSHIP_ANCHOR_INVALID",
        "relationship anchor not unique",
        upstream_code="ASL_RELATIONSHIP_ANCHOR_INVALID",
        details={
            "relationship_anchor": {"name": "经销商"},
            "candidates": ["dealer.dealer_name", "hospital.hospital_name"],
        },
    )

    message = render_dependency_error(exc)

    assert message is not None
    assert "经销商" in message
    assert "当前无法确认可用的候选实体" in message
    assert "hospital_name" not in message


def test_detail_projection_never_displays_candidates():
    exc = AdapterError(
        "ASL_DETAIL_FIELDS_INCOMPLETE",
        "projection incomplete",
        upstream_code="ASL_DETAIL_FIELDS_INCOMPLETE",
        details={
            "missing_fields": ["经销商名称"],
            "candidates": [{"label": "经销商编码", "semantic_role": "name"}],
        },
    )

    message = render_dependency_error(exc)

    assert message is not None
    assert "经销商名称" in message
    assert "经销商编码" not in message
    assert "当前无法确认可用的候选字段" in message


def test_unknown_error_code_stays_sanitized():
    exc = AdapterError(
        "INTERNAL_UNKNOWN_FAILURE",
        "raw upstream text with 10.0.0.7:5432 credentials must not leak",
        upstream_code="SOME_VENDOR_INTERNAL_CODE",
        details={"raw": "connection string postgres://user:pass@10.0.0.7"},
    )

    assert render_dependency_error(exc) is None
