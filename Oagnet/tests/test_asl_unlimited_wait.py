from concurrent.futures import ThreadPoolExecutor
import threading

from capacity_control import AslCapacityController, build_asl_capacity_policy


def test_unlimited_policy_waits_for_queued_and_running_work_without_extra_concurrency():
    policy=build_asl_capacity_policy(max_concurrency=1,queue_timeout_seconds=None,
        llm_timeout_seconds=None,request_deadline_seconds=None)
    assert policy.queue_timeout_seconds is None and policy.request_deadline_seconds is None
    controller=AslCapacityController(policy)
    started,release,second_started=threading.Event(),threading.Event(),threading.Event()
    def first():
        started.set()
        assert release.wait(2)
        return 1
    def second():
        second_started.set()
        return 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(controller.run,first)
        try:
            assert started.wait(1)
            b=pool.submit(controller.run,second)
            assert not second_started.wait(.05)
        finally:release.set()
        assert a.result(timeout=2)==1 and b.result(timeout=2)==2
    assert controller.run(lambda:3)==3


def test_asl_model_has_no_sdk_deadline(monkeypatch):
    import agent
    captured={}
    def init(**kwargs):captured.update(kwargs);return object()
    monkeypatch.setattr(agent,'init_chat_model',init)
    monkeypatch.setattr(agent,'API_KEY','synthetic-test-key')
    agent._get_chat_model.cache_clear()
    try:
        agent._get_chat_model()
        assert captured['timeout'] is None
    finally:agent._get_chat_model.cache_clear()
