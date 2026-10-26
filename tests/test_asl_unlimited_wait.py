import asyncio

import httpx
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import PlatformHttpClient
from app.config import Settings


@pytest.mark.asyncio
async def test_zero_timeout_waits_for_asl_past_normal_dependency_budget(monkeypatch):
    timeouts=[]
    class Client:
        def __init__(self,**kwargs):timeouts.append(kwargs['timeout'])
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):
            await asyncio.sleep(.03)
            return httpx.Response(200,json={'success':True})
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    result=await asyncio.wait_for(PlatformHttpClient(Settings(request_timeout_seconds=.01)).post(
        'http://test','/agent/query',{},timeout=0),timeout=.5)
    assert result=={'success':True} and timeouts==[None]


@pytest.mark.asyncio
async def test_unlimited_asl_wait_still_obeys_caller_cancellation(monkeypatch):
    started,cancelled=asyncio.Event(),asyncio.Event()
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    task=asyncio.create_task(PlatformHttpClient(Settings()).post('http://test','/agent/query',{},timeout=0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_no_deadline_does_not_add_infinite_retries(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):
            calls.append(1)
            return httpx.Response(503,json={'detail':{'code':'ASL_CAPACITY_EXHAUSTED'}})
    async def sleep(_):pass
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    monkeypatch.setattr('app.adapters.http.asyncio.sleep',sleep)
    with pytest.raises(AdapterError):
        await PlatformHttpClient(Settings(http_max_retries=1)).post('http://test','/agent/query',{},timeout=0)
    assert len(calls)==2
