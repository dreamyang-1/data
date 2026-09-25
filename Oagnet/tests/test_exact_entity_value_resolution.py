import pytest

import mysql_tool


class _Cursor:
    def __init__(self, row, calls):
        self.row = row
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, args):
        self.calls.append((sql, args))

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row, calls):
        self.row = row
        self.calls = calls
        self.closed = False

    def cursor(self):
        return _Cursor(self.row, self.calls)

    def close(self):
        self.closed = True


def _metadata_row(**overrides):
    row = {
        "entity_code": "product",
        "business_domain_id": 205,
        "mapping_table": "product",
        "mapping_column": "product_name",
        "data_source_id": 58,
        "db_type": "MySQL",
        "host": "source.internal",
        "port": "3306",
        "username": "reader",
        "password": "secret",
        "db_name": "sales",
    }
    row.update(overrides)
    return row


def test_exact_entity_value_resolution_is_scoped_and_parameterized(monkeypatch):
    metadata_calls = []
    source_calls = []
    connections = []

    def metadata_query(sql, args):
        metadata_calls.append((sql, args))
        return [_metadata_row()]

    def connect(**kwargs):
        connection = _Connection((1,), source_calls)
        connections.append((kwargs, connection))
        return connection

    monkeypatch.setattr(mysql_tool, "_query", metadata_query)
    monkeypatch.setattr(mysql_tool.pymysql, "connect", connect)

    fields = mysql_tool.resolve_exact_entity_value_fields(
        81,
        [205],
        [{
            "entity_code": "product",
            "field": "product.product_name",
            "business_domain_id": 205,
        }],
        "超声血管导引穿刺套件",
    )

    assert fields == ["product.product_name"]
    metadata_sql, metadata_args = metadata_calls[0]
    assert "b.semantic_model_id = m.id" in metadata_sql
    assert "e.business_domain_id = %s" in metadata_sql
    assert "COALESCE(a.is_main_attribute, 0) = 1" in metadata_sql
    assert "semantic_model_table" in metadata_sql
    assert "semantic_model_field" in metadata_sql
    assert "semantic_model_data_source" in metadata_sql
    assert metadata_args == (81, "product", 205, "product", "product_name")
    assert len(source_calls) == 1
    assert source_calls[0][0].startswith("SELECT 1 FROM `product` WHERE REPLACE(")
    assert source_calls[0][0].endswith(" = %s LIMIT 1")
    assert source_calls[0][1] == ("超声血管导引穿刺套件",)
    assert connections[0][0]["database"] == "sales"
    assert connections[0][1].closed is True


def test_exact_entity_value_resolution_returns_zero_or_multiple_fields(monkeypatch):
    source_calls = []
    rows = [
        _metadata_row(),
        _metadata_row(
            entity_code="dealer",
            mapping_table="dealer",
            mapping_column="dealer_name",
            data_source_id=59,
        ),
    ]
    monkeypatch.setattr(mysql_tool, "_query", lambda *_args: rows)
    monkeypatch.setattr(
        mysql_tool.pymysql,
        "connect",
        lambda **_kwargs: _Connection((1,), source_calls),
    )

    fields = mysql_tool.resolve_exact_entity_value_fields(
        81,
        205,
        [
            {
                "entity_code": "product",
                "field": "product.product_name",
                "business_domain_id": 205,
            },
            {
                "entity_code": "dealer",
                "field": "dealer.dealer_name",
                "business_domain_id": 205,
            },
        ],
        "同名值",
    )
    assert fields == ["dealer.dealer_name", "product.product_name"]

    monkeypatch.setattr(
        mysql_tool.pymysql,
        "connect",
        lambda **_kwargs: _Connection(None, source_calls),
    )
    assert mysql_tool.resolve_exact_entity_value_fields(
        81,
        205,
        [{
            "entity_code": "product",
            "field": "product.product_name",
            "business_domain_id": 205,
        }],
        "不存在",
    ) == []


def test_exact_entity_value_resolution_rejects_unapproved_identifier(monkeypatch):
    monkeypatch.setattr(
        mysql_tool,
        "_query",
        lambda *_args: pytest.fail("metadata query must not run"),
    )

    with pytest.raises(ValueError, match="非法字段名"):
        mysql_tool.resolve_exact_entity_value_fields(
            81,
            205,
            [{
                "entity_code": "product",
                "field": "product.product_name;DROP_TABLE",
                "business_domain_id": 205,
            }],
            "任意值",
        )


def test_exact_entity_attribute_value_resolution_allows_non_main_business_field(monkeypatch):
    metadata_calls = []
    rows = [
        _metadata_row(
            entity_code="main_data_domain_ent_manufacturer",
            mapping_table="manufacturer",
            mapping_column="manufacturer_name",
        ),
        _metadata_row(
            entity_code="main_data_domain_ent_manufacturer",
            mapping_table="manufacturer",
            mapping_column="parent_brand",
        ),
    ]

    def metadata_query(sql, args):
        metadata_calls.append((sql, args))
        return rows

    monkeypatch.setattr(mysql_tool, "_query", metadata_query)
    monkeypatch.setattr(
        mysql_tool,
        "_data_source_has_exact_value",
        lambda _source, _table, column, value: (
            column == "parent_brand" and value == "BD"
        ),
    )

    fields = mysql_tool.resolve_exact_entity_attribute_value_fields(
        81,
        205,
        [
            {
                "entity_code": "main_data_domain_ent_manufacturer",
                "field": "manufacturer.manufacturer_name",
                "business_domain_id": 205,
            },
            {
                "entity_code": "main_data_domain_ent_manufacturer",
                "field": "manufacturer.parent_brand",
                "business_domain_id": 205,
            },
        ],
        "BD",
    )

    assert fields == ["manufacturer.parent_brand"]
    metadata_sql, metadata_args = metadata_calls[0]
    assert "semantic_model_attribute_config" in metadata_sql
    # Model-copy evidence shows entity IDs are reused across semantic models.
    # Non-main attributes remain allowed, within the current model only.
    assert "a.semantic_model_id = m.id" in metadata_sql
    assert "COALESCE(a.is_main_attribute, 0) = 1" not in metadata_sql
    assert metadata_args == (
        81,
        "main_data_domain_ent_manufacturer", 205,
        "manufacturer", "manufacturer_name",
        "main_data_domain_ent_manufacturer", 205,
        "manufacturer", "parent_brand",
    )


def test_exact_entity_attribute_value_resolution_rejects_cross_scope_candidate(monkeypatch):
    monkeypatch.setattr(
        mysql_tool,
        "_query",
        lambda *_args: pytest.fail("metadata query must not run"),
    )

    assert mysql_tool.resolve_exact_entity_attribute_value_fields(
        81,
        205,
        [{
            "entity_code": "main_data_domain_ent_manufacturer",
            "field": "manufacturer.parent_brand",
            "business_domain_id": 999,
        }],
        "BD",
    ) == []


def test_catalog_match_returns_canonical_value_and_match_strength(monkeypatch):
    normalized = [
        (
            "main_data_domain_ent_manufacturer",
            205,
            "manufacturer",
            "manufacturer_name",
        ),
        ("product_category", 205, "product_category", "product_type"),
    ]
    rows = [
        _metadata_row(
            entity_code="main_data_domain_ent_manufacturer",
            mapping_table="manufacturer",
            mapping_column="manufacturer_name",
            is_main_attribute=1,
        ),
        _metadata_row(
            entity_code="product_category",
            mapping_table="product_category",
            mapping_column="product_type",
            is_main_attribute=1,
        ),
    ]
    monkeypatch.setattr(
        mysql_tool,
        "_authorized_entity_field_rows",
        lambda *_args, **_kwargs: (normalized, rows),
    )
    monkeypatch.setattr(
        mysql_tool,
        "_data_source_catalog_matches",
        lambda _source, _table, column, _value: (
            ["江苏苏云医疗器材有限公司"]
            if column == "manufacturer_name"
            else ["江苏"]
        ),
    )

    matches = mysql_tool.resolve_entity_attribute_catalog_matches(
        81,
        205,
        [
            {
                "entity_code": "main_data_domain_ent_manufacturer",
                "field": "manufacturer.manufacturer_name",
                "business_domain_id": 205,
            },
            {
                "entity_code": "product_category",
                "field": "product_category.product_type",
                "business_domain_id": 205,
            },
        ],
        "江苏苏云",
    )

    assert matches[0] == {
        "entity_code": "main_data_domain_ent_manufacturer",
        "field": "manufacturer.manufacturer_name",
        "canonical_value": "江苏苏云医疗器材有限公司",
        "match_type": "CANONICAL_CONTAINS_MENTION",
        "is_main_attribute": True,
    }
    assert matches[1]["match_type"] == "MENTION_CONTAINS_CANONICAL"


def test_catalog_match_rejects_unapproved_identifier(monkeypatch):
    monkeypatch.setattr(
        mysql_tool,
        "_query",
        lambda *_args: pytest.fail("metadata query must not run"),
    )

    with pytest.raises(ValueError, match="非法字段名"):
        mysql_tool.resolve_entity_attribute_catalog_matches(
            81,
            205,
            [{
                "entity_code": "product_category",
                "field": "product_category.product_type;DROP_TABLE",
                "business_domain_id": 205,
            }],
            "低值耗材",
        )


def test_unicode_dash_variant_resolves_to_source_canonical_value(monkeypatch):
    normalized = [("product", 205, "product", "specification")]
    rows = [_metadata_row(
        mapping_column="specification",
        is_main_attribute=0,
    )]
    monkeypatch.setattr(
        mysql_tool,
        "_authorized_entity_field_rows",
        lambda *_args, **_kwargs: (normalized, rows),
    )
    monkeypatch.setattr(
        mysql_tool,
        "_data_source_catalog_matches",
        lambda _source, _table, _column, value: ["TDC-3"]
        if value == "TDC-3" else [],
    )

    matches = mysql_tool.resolve_entity_attribute_catalog_matches(
        81,
        205,
        [{
            "entity_code": "product",
            "field": "product.specification",
            "business_domain_id": 205,
        }],
        "TDC‑3",
    )

    assert matches == [{
        "entity_code": "product",
        "field": "product.specification",
        "canonical_value": "TDC-3",
        "match_type": "EXACT",
        "is_main_attribute": False,
    }]
