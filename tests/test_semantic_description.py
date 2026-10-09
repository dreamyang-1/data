"""Platform metadata lookup, fresh storage reads and no static fallback."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from app.domain import semantic_description as sd


def _settings(**overrides):
    base = {
        "mysql_host": "catalog.example", "mysql_user": "reader",
        "mysql_password": SecretStr("fixture-only"), "mysql_database": "catalog",
        "semantic_description_base_url": "https://descriptions.example",
        "semantic_description_timeout_seconds": 3.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _install(monkeypatch, urls, responses):
    lookups, requests = [], []
    def lookup(settings, model_id):
        lookups.append(model_id)
        value = urls[model_id]
        if isinstance(value, Exception):
            raise value
        return value
    async def handler(request):
        requests.append(request)
        value = responses[str(request.url)]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, content=value)
    client = httpx.AsyncClient
    monkeypatch.setattr(sd, "_description_url", lookup)
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    return lookups, requests


@pytest.mark.asyncio
async def test_remote_description_fetched_by_model_id(monkeypatch):
    url = "https://descriptions.example/files/模型_最新.md"
    lookups, requested = _install(monkeypatch, {121: url}, {str(httpx.URL(url)): "# 当前模型\n实体：科室"})
    reference = await sd.load_semantic_description(_settings(), 121)
    assert lookups == [121]
    assert str(requested[0].url) == str(httpx.URL(url))
    assert requested[0].headers["cache-control"] == "no-cache"
    # This is a stable logical label, NOT the actual download filename.
    assert reference == {"source": "semantic_model_121.md", "available": True, "content": "# 当前模型\n实体：科室"}


@pytest.mark.asyncio
async def test_remote_failure_falls_back_to_local(monkeypatch):
    # STALE_TEST: user removed local fallback; failure must return unavailable.
    url = "https://descriptions.example/files/model.md"
    _install(monkeypatch, {121: url}, {url: httpx.ConnectError("fixture outage")})
    reference = await sd.load_semantic_description(_settings(), 121)
    assert reference == {"source": "semantic_model_121.md", "available": False, "content": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", [None, 0, -1, True, "121", "1 OR 1=1", 1.5])
async def test_missing_model_id_uses_local_only(monkeypatch, model_id):
    # STALE_TEST: a missing/invalid model never selects a generic local model.
    lookups, requested = _install(monkeypatch, {}, {})
    reference = await sd.load_semantic_description(_settings(), model_id)
    assert lookups == requested == []
    assert not reference["available"] and reference["content"] == ""


@pytest.mark.asyncio
async def test_cache_hits_within_ttl(monkeypatch):
    # STALE_TEST: both the metadata pointer and bytes are fresh on EVERY use.
    first, second = "https://descriptions.example/first.md", "https://descriptions.example/second.md"
    urls, documents = {121: first}, {first: "旧版", second: "新版"}
    lookups, requested = _install(monkeypatch, urls, documents)
    assert (await sd.load_semantic_description(_settings(), 121))["content"] == "旧版"
    urls[121] = second
    assert (await sd.load_semantic_description(_settings(), 121))["content"] == "新版"
    assert lookups == [121, 121] and len(requested) == 2


@pytest.mark.asyncio
async def test_expired_cache_refetches(monkeypatch):
    url = "https://descriptions.example/file.md"
    docs = {url: "初始内容"}
    lookups, requested = _install(monkeypatch, {121: url}, docs)
    assert (await sd.load_semantic_description(_settings(), 121))["content"] == "初始内容"
    docs[url] = "同一地址的新内容"
    assert (await sd.load_semantic_description(_settings(), 121))["content"] == "同一地址的新内容"
    assert lookups == [121, 121] and len(requested) == 2


@pytest.mark.asyncio
async def test_failure_is_not_cached_and_other_models_are_isolated(monkeypatch):
    url = "https://descriptions.example/one.md"
    docs = {url: httpx.ConnectError("fixture")}
    lookups, requested = _install(monkeypatch, {120: url, 121: ""}, docs)
    assert not (await sd.load_semantic_description(_settings(), 120))["available"]
    docs[url] = "恢复"
    assert (await sd.load_semantic_description(_settings(), 120))["content"] == "恢复"
    assert not (await sd.load_semantic_description(_settings(), 121))["available"]
    assert lookups == [120, 120, 121] and len(requested) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "ftp://descriptions.example/a.md",
    "https://untrusted.example/a.md", "https://descriptions.example:444/a.md",
    "https://user:password@descriptions.example/a.md", "https://descriptions.example/a.md#fragment",
    "https://descriptions.example:bad/a.md"])
async def test_bad_or_unregistered_storage_never_falls_back(monkeypatch, url):
    lookups, requested = _install(monkeypatch, {121: url}, {})
    assert not (await sd.load_semantic_description(_settings(), 121))["available"]
    assert lookups == [121] and requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    httpx.Response(404), httpx.Response(403), httpx.Response(500),
    httpx.Response(302, headers={"location": "https://untrusted.example/secret"}),
    b"", b"  ", b"\xff", b"\x00", b"<!doctype html><html>login",
    httpx.Response(200, text="login", headers={"content-type": "text/html"}),
])
async def test_invalid_download_is_unavailable(monkeypatch, value):
    url = "https://descriptions.example/model.md"
    _install(monkeypatch, {121: url}, {url: value})
    assert not (await sd.load_semantic_description(_settings(), 121))["available"]


@pytest.mark.asyncio
async def test_document_size_and_bom(monkeypatch):
    url = "https://descriptions.example/model.md"
    docs = {url: b"\xef\xbb\xbf# semantic\n"}
    _install(monkeypatch, {121: url}, docs)
    assert (await sd.load_semantic_description(_settings(), 121))["content"] == "# semantic"
    assert not (await sd.load_semantic_description(_settings(semantic_description_max_bytes=2), 121))["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("secure,endpoint", [(False, "minio.example:9000"), (True, "minio.example"), (True, "https://minio.example")])
async def test_registered_minio_download_url_supported(monkeypatch, secure, endpoint):
    url = ("https://minio.example" if secure else "http://minio.example:9000") + "/bucket/generated.md?signature=fixture"
    _install(monkeypatch, {121: url}, {url: "MinIO reference"})
    reference = await sd.load_semantic_description(_settings(minio_endpoint=endpoint, minio_secure=secure), 121)
    assert reference["available"] and reference["content"] == "MinIO reference"


@pytest.mark.asyncio
async def test_catalog_errors_and_missing_config_do_not_leak_secrets(monkeypatch, caplog):
    lookups, requests = _install(monkeypatch, {121: RuntimeError("password=secret")}, {})
    assert not (await sd.load_semantic_description(_settings(), 121))["available"]
    assert not (await sd.load_semantic_description(_settings(mysql_host=None), 121))["available"]
    assert lookups == [121] and requests == []
    assert "secret" not in caplog.text


@pytest.mark.parametrize("row", [None, {"semantic_desc_file_url": None},
    {"semantic_desc_file_url": " https://descriptions.example/模型.md "}])
def test_metadata_query_binds_id_and_closes_connection(monkeypatch, row):
    import pymysql
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    monkeypatch.setattr(pymysql, "connect", lambda **kw: connection)
    result = sd._description_url(_settings(), 121)
    sql, params = cursor.execute.call_args.args
    assert "WHERE id = %s AND is_deleted = 0" in sql and params == (121,)
    assert "semantic_desc_file_url" in sql
    assert result == (row.get("semantic_desc_file_url") or "").strip() if row else result == ""
    connection.close.assert_called_once()


def test_metadata_query_closes_connection_on_error(monkeypatch):
    import pymysql
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("fixture")
    monkeypatch.setattr(pymysql, "connect", lambda **kw: connection)
    with pytest.raises(RuntimeError):
        sd._description_url(_settings(), 121)
    connection.close.assert_called_once()


def test_static_semantic_sources_are_removed():
    from pathlib import Path
    from app.planning.task_dag import extraction_embedded_spec, extraction_parser_prompt
    assert not (Path(__file__).resolve().parents[1] / "语义描述文件.md").exists()
    assert extraction_embedded_spec() == ""
    assert "输出字段与定义" in extraction_parser_prompt()
