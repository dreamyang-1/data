import pytest

from app.tools import DatabaseLoadError, build_database_load_tool


def test_database_load_tool_builds_request_scoped_selection():
    output = build_database_load_tool()(5)

    assert output == {
        "success": True,
        "tool": "load_database",
        "database_id": 5,
        "data_source_id": "5",
        "scope": "request",
    }


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "5", 1.5])
def test_database_load_tool_rejects_invalid_identifiers(value):
    with pytest.raises(DatabaseLoadError):
        build_database_load_tool().load(value)


def test_database_load_tool_rejects_values_larger_than_signed_bigint():
    with pytest.raises(DatabaseLoadError):
        build_database_load_tool().load(9_223_372_036_854_775_808)
