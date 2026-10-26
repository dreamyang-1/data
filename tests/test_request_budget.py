import asyncio
import pytest
from app.services.request_budget import run_with_request_budget, exclude_unlimited_wait


@pytest.mark.asyncio
async def test_real_http_asl_boundary_suspends_outer_request_budget(monkeypatch):
    import httpx
    from app.adapters.http import PlatformHttpClient
    from app.config import Settings
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):
            await asyncio.sleep(.08)
            return httpx.Response(200,json={'success':True})
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    result=await run_with_request_budget(
        PlatformHttpClient(Settings()).post('http://test','/agent/query',{},timeout=0),timeout=.03)
    assert result=={'success':True}


@exclude_unlimited_wait
async def dependency(delay, *, timeout=None):
    await asyncio.sleep(delay)
    return 'ok'


@pytest.mark.asyncio
async def test_asl_wait_can_exceed_outer_budget():
    assert await run_with_request_budget(dependency(.08,timeout=0),timeout=.03)=='ok'


@pytest.mark.asyncio
async def test_parallel_asl_waits_resume_only_after_last_finishes():
    async def operation():
        return await asyncio.gather(dependency(.05,timeout=0),dependency(.12,timeout=0))
    assert await run_with_request_budget(operation(),timeout=.03)==['ok','ok']


@pytest.mark.asyncio
async def test_ordinary_dependency_still_times_out():
    with pytest.raises(TimeoutError):
        await run_with_request_budget(dependency(.1),timeout=.02)


@pytest.mark.asyncio
async def test_remaining_work_cannot_reset_its_budget_each_asl_call():
    async def operation():
        await asyncio.sleep(.03)
        await dependency(.08,timeout=0)
        await asyncio.sleep(.03)
        await dependency(.08,timeout=0)
        await asyncio.sleep(.05)
    with pytest.raises(TimeoutError):
        await run_with_request_budget(operation(),timeout=.08)


@pytest.mark.asyncio
async def test_user_cancel_is_not_suspended():
    task=asyncio.create_task(run_with_request_budget(dependency(10,timeout=0),timeout=.02))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,.1)


@pytest.mark.asyncio
async def test_unlimited_request_does_not_pause_another_request():
    a=asyncio.create_task(run_with_request_budget(dependency(.1,timeout=0),timeout=.02))
    with pytest.raises(TimeoutError):
        await run_with_request_budget(dependency(.1),timeout=.02)
    assert await a=='ok'


@pytest.mark.asyncio
async def test_failed_asl_resumes_remaining_budget():
    @exclude_unlimited_wait
    async def failed(*,timeout=0):
        await asyncio.sleep(.06)
        raise ValueError('upstream failure')
    async def operation():
        with pytest.raises(ValueError):await failed(timeout=0)
        await asyncio.sleep(.06)
    with pytest.raises(TimeoutError):
        await run_with_request_budget(operation(),timeout=.03)
