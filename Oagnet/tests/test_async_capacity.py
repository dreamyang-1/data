"""FIFO admission, cancellation and actual worker lifetime under concurrent load."""
import asyncio
import contextvars
import threading
import time

import pytest

from capacity_control import AslCapacityController, AslCapacityExhausted, build_asl_capacity_policy


def controller(concurrency=4, waiters=32):
    return AslCapacityController(build_asl_capacity_policy(
        max_concurrency=concurrency, max_waiters=waiters,
        queue_timeout_seconds=None, llm_timeout_seconds=None,
        request_deadline_seconds=None,
    ))


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 2)


def test_36_requests_use_four_workers_and_leave_event_loop_responsive():
    c = controller()
    release = threading.Event()
    def work(value):
        assert release.wait(3)
        return value
    async def scenario():
        jobs = [asyncio.create_task(c.arun(lambda i=i: work(i))) for i in range(36)]
        try:
            await until(lambda: c.snapshot()['waiting'] == 32)
            assert c.snapshot()['active'] == 4
            with pytest.raises(AslCapacityExhausted):
                await c.arun(lambda: pytest.fail('overload must not execute'))
            # A second async activity can still run with the entire queue occupied.
            await asyncio.wait_for(asyncio.sleep(.01), .5)
        finally:
            release.set()
        assert await asyncio.gather(*jobs) == list(range(36))
        assert c.snapshot()['active'] == c.snapshot()['waiting'] == 0
    asyncio.run(scenario())


def test_fifo_cancel_waiter_and_reuse_queue_capacity():
    c = controller(1, 3)
    release = threading.Event()
    order = []
    async def scenario():
        first = asyncio.create_task(c.arun(lambda: release.wait(3)))
        await until(lambda: c.snapshot()['active'] == 1)
        jobs = [asyncio.create_task(c.arun(lambda i=i: order.append(i))) for i in range(3)]
        await until(lambda: c.snapshot()['waiting'] == 3)
        jobs[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await jobs[1]
        assert c.snapshot()['waiting'] == 2
        last = asyncio.create_task(c.arun(lambda: order.append(3)))
        await until(lambda: c.snapshot()['waiting'] == 3)
        release.set()
        await asyncio.gather(first, jobs[0], jobs[2], last)
        assert order == [0, 2, 3]
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_cancel_running_request_does_not_free_slot_early():
    c = controller(1, 0)
    release = threading.Event()
    async def scenario():
        task = asyncio.create_task(c.arun(lambda: release.wait(3)))
        await until(lambda: c.snapshot()['active'] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(AslCapacityExhausted):
            await c.arun(lambda: 'must not run')
        release.set()
        await until(lambda: c.snapshot()['active'] == 0)
        assert await c.arun(lambda: 'next') == 'next'
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_disconnected_waiter_is_removed_without_execution():
    c = controller(1, 1)
    release = threading.Event()
    async def disconnected():
        return True
    async def scenario():
        active = asyncio.create_task(c.arun(lambda: release.wait(3)))
        await until(lambda: c.snapshot()['active'] == 1)
        with pytest.raises(asyncio.CancelledError):
            await c.arun(lambda: pytest.fail('disconnected waiter executed'), disconnected=disconnected)
        assert c.snapshot()['waiting'] == 0
        release.set()
        await active
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_worker_reuse_preserves_each_request_context_and_error_isolation():
    scope = contextvars.ContextVar('synthetic_scope', default=None)
    c = controller(2, 20)
    async def request(value):
        token = scope.set((value, value + 100))
        try:
            return await c.arun(lambda: (time.sleep(.002), scope.get())[1])
        finally:
            scope.reset(token)
    async def scenario():
        assert await asyncio.gather(*(request(i) for i in range(20))) == [(i, i + 100) for i in range(20)]
        def fail():
            raise ValueError('isolated error')
        with pytest.raises(ValueError, match='isolated error'):
            await c.arun(fail)
        assert await c.arun(scope.get) is None
    asyncio.run(scenario())


def test_async_model_timeout_is_not_reclassified_as_queue_deadline():
    c = controller()
    def timeout():
        raise TimeoutError('model read timeout')
    async def scenario():
        with pytest.raises(TimeoutError, match='model read timeout'):
            await c.arun(timeout)
        assert await c.arun(lambda: 1) == 1
    asyncio.run(scenario())


def test_http_queue_over_nine_requests_preserves_independent_model_scopes(monkeypatch):
    import json
    import httpx
    import api
    c = controller()
    release = threading.Event()
    def generate(query, **kwargs):
        assert release.wait(3)
        model = kwargs['semantic_model_id']
        domains = kwargs['business_domain_ids']
        fingerprint = 'sha256:' + 'a' * 64
        return {'result': json.dumps({'request': query, 'model': model}),
                'semantic_evidence': {'evidence_version': '1.0', 'producer': 'OAGNET',
                    'semantic_model_id': model, 'requested_business_domain_ids': domains,
                    'resolved_business_domain_ids': domains, 'selected_metrics': [],
                    'asl_signature': fingerprint, 'evidence_fingerprint': fingerprint}}
    monkeypatch.setattr(api, '_asl_capacity', c)
    monkeypatch.setattr(api, 'main', generate)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
            jobs = [asyncio.create_task(client.post('/agent/query', json={
                'query': f'isolated-{i}', 'semantic_model_id': 100 + i,
                'business_domain_ids': [200 + i]})) for i in range(36)]
            try:
                await until(lambda: c.snapshot()['waiting'] == 32)
                refused = await client.post('/agent/query', json={'query': 'excess', 'semantic_model_id': 999})
                assert refused.status_code == 429
                assert refused.json()['code'] == 'ASL_CAPACITY_EXHAUSTED'
                assert refused.headers['Retry-After'] == '5'
                assert (await client.get('/openapi.json')).status_code == 200
            finally:
                release.set()
            responses = await asyncio.gather(*jobs)
            for i, response in enumerate(responses):
                assert response.status_code == 200
                body = response.json()
                assert body['semantic_model_id'] == 100 + i
                assert body['business_domain_ids'] == [200 + i]
                assert json.loads(body['result']) == {'request': f'isolated-{i}', 'model': 100 + i}
    try:
        asyncio.run(scenario())
    finally:
        release.set()
