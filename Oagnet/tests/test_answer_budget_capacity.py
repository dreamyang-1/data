from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

from capacity_control import AslCapacityController, build_asl_capacity_policy


def test_asl_service_has_no_queue_or_generation_deadline():
    import api
    policy = api._asl_capacity.policy
    assert policy.request_deadline_seconds is None
    assert policy.operation_timeout_seconds is None
    assert policy.queue_timeout_seconds is None
    assert policy.max_concurrency > 0


def test_two_reports_with_three_asl_calls_each_can_queue_without_raising_concurrency():
    policy = build_asl_capacity_policy(max_concurrency=1, queue_timeout_seconds=90,
                                      llm_timeout_seconds=45, request_deadline_seconds=175)
    # Scale actual service budgets 100x; keep admission and release semantics.
    scaled = replace(policy, queue_timeout_seconds=policy.queue_timeout_seconds / 100,
                     operation_timeout_seconds=policy.operation_timeout_seconds / 100,
                     request_deadline_seconds=policy.request_deadline_seconds / 100)
    controller = AslCapacityController(scaled)
    lock, active, peak = threading.Lock(), 0, 0
    def operation(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.15)
            return value
        finally:
            with lock:
                active -= 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        values = list(pool.map(lambda value: controller.run(lambda: operation(value)), range(6)))
    assert sorted(values) == list(range(6)) and peak == 1 and active == 0
