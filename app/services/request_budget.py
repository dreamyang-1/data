"""Keep the request budget for bounded work, excluding unlimited ASL waits."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps


_budget: ContextVar[RequestBudget | None] = ContextVar('request_budget', default=None)


class RequestBudget:
    def __init__(self, guard: asyncio.Timeout, seconds: float):
        self.guard = guard
        self.remaining = seconds
        self.waiters = 0
        self.closed = False

    @asynccontextmanager
    async def unlimited_wait(self):
        if self.closed or self.guard.expired():
            yield
            return
        if self.waiters == 0:
            deadline = self.guard.when()
            self.remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            self.guard.reschedule(None)
        self.waiters += 1
        try:
            yield
        finally:
            self.waiters -= 1
            if self.waiters == 0 and not self.closed and not self.guard.expired():
                self.guard.reschedule(asyncio.get_running_loop().time() + self.remaining)


async def run_with_request_budget(operation, *, timeout: float):
    async with asyncio.timeout(timeout) as guard:
        budget = RequestBudget(guard, timeout)
        token = _budget.set(budget)
        try:
            return await operation
        finally:
            budget.closed = True
            _budget.reset(token)


def exclude_unlimited_wait(function):
    """Only internal HTTP calls explicitly using timeout=0 suspend the budget."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        budget = _budget.get()
        if kwargs.get('timeout') == 0 and budget is not None:
            async with budget.unlimited_wait():
                return await function(*args, **kwargs)
        return await function(*args, **kwargs)
    return wrapped
