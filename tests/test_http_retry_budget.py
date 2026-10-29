import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import PlatformHttpClient
from app.config import Settings


@pytest.mark.parametrize('header,expected', [('5', 5), ('0.25', .25), ('-1', 0), ('NaN', 0), ('inf', 0), ('bad', 0)])
def test_retry_after_seconds(header, expected):
    assert PlatformHttpClient._retry_after(httpx.Response(429, headers={'Retry-After': header})) == expected


def test_retry_after_date():
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert 8 <= PlatformHttpClient._retry_after(httpx.Response(503, headers={'Retry-After': header})) <= 10


@pytest.mark.asyncio
async def test_capacity_retry_waits_for_server_and_keeps_request_identity(monkeypatch):
    calls, waits = [], []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, path, **kwargs):
            calls.append(kwargs)
            return httpx.Response(429, headers={'Retry-After': '5'}, json={'detail': {'code': 'ASL_CAPACITY_EXHAUSTED'}}) if len(calls) == 1 else httpx.Response(200, json={'ok': True})
    async def sleep(delay): waits.append(delay)
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient', Client)
    monkeypatch.setattr('app.adapters.http.asyncio.sleep', sleep)
    client = PlatformHttpClient(Settings(http_max_retries=2))
    result = await client.post('http://local', '/agent/query', {'semantic_model_id': 130}, idempotency_key='fixed', bearer_token='test')
    assert result == {'ok': True} and waits == [5]
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_retries_remain_bounded_and_nonretryable_sql_is_not_retried(monkeypatch):
    calls = []
    started = asyncio.Event()
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, path, **kwargs):
            calls.append(path)
            return httpx.Response(429, headers={'Retry-After': '60'}, json={'detail': {'code': 'ASL_CAPACITY_EXHAUSTED'}})
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient', Client)
    waits = []
    async def sleep(delay): waits.append(delay)
    monkeypatch.setattr('app.adapters.http.asyncio.sleep', sleep)
    client = PlatformHttpClient(Settings(http_max_retries=2))
    for retryable in [True, False]:
        with pytest.raises(AdapterError):
            await client.post('http://local', '/query', {}, timeout=.02, retryable=retryable)
    assert len(calls) == 4
    assert waits == [60, 60]


@pytest.mark.asyncio
async def test_waiting_request_is_cancelled_only_when_caller_cancels(monkeypatch):
    calls = []
    started = asyncio.Event()
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, path, **kwargs):
            calls.append(path)
            started.set()
            await asyncio.Event().wait()
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient', Client)
    task = asyncio.create_task(PlatformHttpClient(Settings(http_max_retries=2)).post('http://local', '/query', {}))
    await started.wait()
    await asyncio.sleep(.03)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
