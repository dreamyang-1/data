# -*- coding: utf-8 -*-
"""语义描述文件加载器：远程拉取、失败回退本地、TTL缓存。"""
from types import SimpleNamespace

import httpx
import pytest

from app.domain import semantic_description as sd


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=None  # type: ignore[arg-type]
            )


@pytest.fixture(autouse=True)
def _reset_cache():
    sd.clear_cache()
    yield
    sd.clear_cache()


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "semantic_description_base_url": "http://192.168.1.49:8012",
        "semantic_description_timeout_seconds": 3.0,
        "semantic_description_cache_ttl_seconds": 30,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _install_client(monkeypatch, responses: dict[str, object]) -> list[str]:
    """替换 AsyncClient，按 URL 返回预设响应，记录请求过的 URL。"""
    requested: list[str] = []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str):
            requested.append(url)
            result = responses.get(url, "fetch-error")
            if result == "fetch-error":
                raise httpx.ConnectError("connection refused")
            if result is None:
                return _FakeResponse("", status=404)
            return _FakeResponse(str(result))

    monkeypatch.setattr(sd.httpx, "AsyncClient", _FakeClient)
    return requested


@pytest.mark.asyncio
async def test_remote_description_fetched_by_model_id(monkeypatch):
    url = "http://192.168.1.49:8012/files/semantic_model_81.md"
    requested = _install_client(monkeypatch, {url: "# 业务语义规范\n指标：医院总数"})

    reference = await sd.load_semantic_description(_settings(), 81)

    assert requested == [url]
    assert reference["source"] == "semantic_model_81.md"
    assert reference["available"] is True
    assert reference["content"].startswith("# 业务语义规范")


@pytest.mark.asyncio
async def test_remote_failure_falls_back_to_local(monkeypatch):
    _install_client(monkeypatch, {})

    reference = await sd.load_semantic_description(_settings(), 81)

    assert reference["source"] == "语义描述文件.md"
    assert reference["available"] == bool(sd.read_local_description())


@pytest.mark.asyncio
async def test_missing_model_id_uses_local_only(monkeypatch):
    requested = _install_client(monkeypatch, {})

    reference = await sd.load_semantic_description(_settings(), None)

    assert requested == []
    assert reference["source"] == "语义描述文件.md"


@pytest.mark.asyncio
async def test_cache_hits_within_ttl(monkeypatch):
    url = "http://192.168.1.49:8012/files/semantic_model_81.md"
    requested = _install_client(monkeypatch, {url: "内容"})

    await sd.load_semantic_description(_settings(), 81)
    await sd.load_semantic_description(_settings(), 81)

    assert requested == [url]


@pytest.mark.asyncio
async def test_expired_cache_refetches(monkeypatch):
    url = "http://192.168.1.49:8012/files/semantic_model_81.md"
    requested = _install_client(monkeypatch, {url: "内容"})
    settings = _settings(semantic_description_cache_ttl_seconds=0)

    await sd.load_semantic_description(settings, 81)
    await sd.load_semantic_description(settings, 81)

    assert requested == [url, url]
