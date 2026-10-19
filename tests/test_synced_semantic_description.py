"""Read the real model_sync artifacts when no platform download is registered."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description as sd
from app.planning.task_dag import SingleQuestionPlanner


def settings():
    return SimpleNamespace(
        mysql_host="catalog.example", mysql_user="reader",
        mysql_password=SecretStr("fixture"), mysql_database="catalog",
        semantic_description_base_url="https://descriptions.example/",
    )


def install_catalog(monkeypatch, rows):
    import pymysql
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = rows
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: connection)
    return connection, cursor


def install_storage(monkeypatch, handler):
    original = httpx.AsyncClient
    def client(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(sd.httpx, "AsyncClient", client)


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [None, "", "  "])
async def test_active_model_reads_actual_sync_document_without_registered_url(monkeypatch, registered):
    connection, cursor = install_catalog(monkeypatch, [{"semantic_desc_file_url": registered}])
    requests = []
    async def storage(request):
        requests.append(request)
        assert str(request.url) == "https://descriptions.example/files/semantic_model_81.md"
        return httpx.Response(200, text="# 业务语义规范\n| 指标 | 单位 |\n| 合作时长 | 月 |")
    install_storage(monkeypatch, storage)
    result = await sd.load_semantic_description(settings(), 81)
    assert result["available"] and "合作时长" in result["content"]
    assert result["source"] == "semantic_model_81.md"
    assert len(requests) == 1 and requests[0].headers["cache-control"] == "no-cache"
    sql, params = cursor.execute.call_args.args
    assert "is_deleted = 0" in sql and params == (81,)
    connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_missing_or_deleted_model_never_reads_leftover_sync_artifact(monkeypatch):
    install_catalog(monkeypatch, [None])
    async def storage(request):
        pytest.fail("An inactive model must not reach storage")
    install_storage(monkeypatch, storage)
    assert await sd.load_semantic_description(settings(), 81) == {
        "source": "semantic_model_81.md", "available": False, "content": "",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 403])
async def test_registered_filename_is_authoritative_even_on_download_failure(monkeypatch, status):
    url = "https://descriptions.example/files/platform-renamed.md"
    install_catalog(monkeypatch, [{"semantic_desc_file_url": url}])
    requests = []
    async def storage(request):
        requests.append(str(request.url))
        return httpx.Response(status, text="# Registered description")
    install_storage(monkeypatch, storage)
    result = await sd.load_semantic_description(settings(), 81)
    assert result["available"] == (status == 200)
    assert requests == [url]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_missing_sync_file_remains_unavailable_without_other_content(monkeypatch, status):
    install_catalog(monkeypatch, [{"semantic_desc_file_url": None}])
    requests = []
    async def storage(request):
        requests.append(str(request.url))
        return httpx.Response(status)
    install_storage(monkeypatch, storage)
    assert not (await sd.load_semantic_description(settings(), 81))["available"]
    assert requests == ["https://descriptions.example/files/semantic_model_81.md"]


@pytest.mark.asyncio
async def test_sync_reads_remain_fresh_and_model_specific(monkeypatch):
    install_catalog(monkeypatch, [{"semantic_desc_file_url": None}] * 3)
    requests = []
    async def storage(request):
        requests.append(request.url.path)
        return httpx.Response(200, text=request.url.path + f" revision {len(requests)}")
    install_storage(monkeypatch, storage)
    first = await sd.load_semantic_description(settings(), 81)
    second = await sd.load_semantic_description(settings(), 81)
    other = await sd.load_semantic_description(settings(), 82)
    assert first["content"] != second["content"]
    assert "semantic_model_82.md" in other["content"] and "semantic_model_81.md" not in other["content"]
    assert requests == ["/files/semantic_model_81.md", "/files/semantic_model_81.md", "/files/semantic_model_82.md"]


@pytest.mark.asyncio
async def test_full_sync_document_reaches_actual_planning_model(monkeypatch):
    install_catalog(monkeypatch, [{"semantic_desc_file_url": None}])
    document = "# 业务语义规范\n| 指标 | 同义词 | 单位 |\n| 合作时长 | 合作月数 | 月 |\n" + "说明内容\n" * 400 + "说明文件结尾"
    async def storage(request):
        return httpx.Response(200, text=document)
    install_storage(monkeypatch, storage)
    captured = []
    async def model(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"intent": "CHAT", "tasks": []})}}]})
    config = Settings(_env_file=None, env="test", intent_model_api_key=SecretStr("fixture"),
        mysql_host="catalog.example", mysql_user="reader", mysql_password="fixture",
        mysql_database="catalog", semantic_description_base_url="https://descriptions.example")
    planner = SingleQuestionPlanner(config, transport=httpx.MockTransport(model))
    await planner._model_decompose("查询合作时长大于12个月的经销商名单", semantic_model_id=81)
    system, user = captured[0]["messages"]
    assert document in system["content"]
    assert user["content"] == "查询合作时长大于12个月的经销商名单"
    assert "scoped_planning_catalog" not in system["content"]
