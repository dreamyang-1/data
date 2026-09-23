from unittest.mock import Mock, patch

import pytest
from minio.error import S3Error

from data_exporter import format_query_result


@pytest.mark.parametrize("count", [0, 20, 200])
def test_small_results_do_not_export(count):
    rows = [{"value": n} for n in range(count)]
    with patch("data_exporter.get_exporter") as exporter:
        result = format_query_result(["value"], rows, count)
    exporter.assert_not_called()
    assert result["data"] == rows
    assert result["download_url"] is None
    assert "export_error" not in result


@pytest.mark.parametrize("count", [201, 1523])
@pytest.mark.parametrize("fails", [False, True])
def test_large_results_always_return_twenty_but_export_all(count, fails):
    rows = [{"value": n} for n in range(count)]
    exporter = Mock()
    exporter.export_to_excel.return_value = "https://files.example/result.xlsx"
    if fails:
        exporter.export_to_excel.side_effect = RuntimeError("private-secret-record")
    with patch("data_exporter.get_exporter", return_value=exporter):
        result = format_query_result(["value"], rows, count)
    exporter.export_to_excel.assert_called_once_with(["value"], rows)
    assert result["success"] is True
    assert result["row_count"] == count
    assert result["data"] == rows[:20]
    assert result["preview_count"] == 20
    assert result["preview_truncated"] is True
    assert "private-secret-record" not in str(result)
    if fails:
        assert result["download_url"] is None
        assert "附件导出失败" in result["export_error"]
    else:
        assert result["download_url"] == "https://files.example/result.xlsx"
        assert "export_error" not in result


def test_exporter_initialization_failure_preserves_preview_and_safe_cause(caplog):
    error = S3Error(response=None, code="AccessDenied", message="private-secret",
                    resource="private-resource", request_id="r", host_id="h")
    rows = [{"value": n} for n in range(1523)]
    with patch("data_exporter.get_exporter", side_effect=error):
        result = format_query_result(["value"], rows, len(rows))
    assert len(result["data"]) == 20
    assert "AccessDenied" in result["export_error"]
    assert "private-secret" not in str(result) + caplog.text
    assert "storage_access_denied=True" in caplog.text
