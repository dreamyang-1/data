"""Publication errors stay actionable in Chinese without changing API failure codes."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import api
from catalog_release import CatalogEvidenceError
from publication_errors import publication_failure_message


@pytest.mark.parametrize("error,stage,expected", [
    (CatalogEvidenceError("CATALOG_SOURCE_ID_COLLISION"), "semantic_rebuild",
     "（阶段：语义向量重建）语义目录源记录标识冲突"),
    (ValueError("PUBLICATION_EMBEDDING_INVALID: dimension or values differ"), "semantic_embedding",
     "（阶段：语义向量生成）待发布向量的维数或数值不正确"),
    (TimeoutError("request timed out"), "attribute_value_read", "（阶段：属性值读取）服务请求超时"),
    (Exception("Can't connect to MySQL server (Connection refused)"), "physical_prepare",
     "（阶段：物理目录准备）服务连接失败"),
    (CatalogEvidenceError("CATALOG_NEW_FAILURE"), "future_stage", "（阶段：发布处理）语义目录校验失败"),
    (RuntimeError("unexpected backend error"), None, "服务处理异常，请查看服务日志"),
    (RuntimeError("读取属性配置超时"), None, "读取属性配置超时"),
    (Exception(), None, "服务处理异常，请查看服务日志"),
])
def test_publication_failure_is_chinese(error, stage, expected):
    assert publication_failure_message(error, stage) == expected


def test_rebuild_collision_returns_chinese_and_logs_original(monkeypatch):
    error = CatalogEvidenceError("CATALOG_SOURCE_ID_COLLISION")
    prepare = Mock(return_value=SimpleNamespace(attribute_enums={}))
    rebuild = Mock(side_effect=error)
    publish = Mock()
    log = Mock()
    monkeypatch.setattr(api, "mysql_advisory_lock", lambda *a: nullcontext())
    monkeypatch.setattr(api, "consistent_catalog_read", lambda: nullcontext())
    monkeypatch.setattr(api, "prepare_physical_catalog", prepare)
    monkeypatch.setattr(api, "rebuild_index_by_scope", rebuild)
    monkeypatch.setattr(api, "publish_physical_catalog", publish)
    monkeypatch.setattr(api.logger, "warning", log)
    response = TestClient(api.app).post("/vector/rebuild", json={"semantic_model_id": 6})
    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["code"] == "VECTOR_SOURCE_INVALID"
    assert payload["message"] == "向量重建失败：（阶段：语义向量重建）语义目录源记录标识冲突"
    assert payload["message"] == payload["detail"]["message"]
    publish.assert_not_called()
    assert log.call_args.args[1:] == ("semantic_rebuild", "CatalogEvidenceError", error)


@pytest.mark.parametrize("route,code", [
    ("rebuild", "ENTITY_ATTRIBUTE_SOURCE_INVALID"),
    ("sync", "ENTITY_ATTRIBUTE_SOURCE_INVALID"),
])
def test_attribute_publication_uses_same_translation(monkeypatch, route, code):
    monkeypatch.setattr(api, "mysql_advisory_lock", lambda *a: nullcontext())
    monkeypatch.setattr(api, "replace_entity_attribute_index", Mock(
        side_effect=CatalogEvidenceError("CATALOG_SOURCE_ID_COLLISION")))
    response = TestClient(api.app).post(f"/vector/entity-attributes/{route}", json={
        "semantic_model_id": 999901, "business_domain_id": 999902,
        "event_id": f"chinese-error-{route}",
    })
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == code
    assert payload["message"].endswith("语义目录源记录标识冲突")
    assert payload["detail"]["message"] == payload["message"]
