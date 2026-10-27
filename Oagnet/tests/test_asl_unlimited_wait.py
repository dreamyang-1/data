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


def test_asl_model_transport_is_bounded_without_queue_deadline(monkeypatch):
    # User approved bounded transport protection on 2026-10-27. The old assertion
    # timeout=None is STALE_TEST; unlimited normal queue waiting remains covered.
    import ast
    from pathlib import Path
    import agent
    captured = {}
    def init(**kwargs):
        captured.update(kwargs)
        return object()
    # The offline runner replaces the module factory; execute its actual source
    # with only model construction mocked so this check works in both runners.
    tree = ast.parse(Path(agent.__file__).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_get_chat_model")
    namespace = dict(vars(agent), init_chat_model=init, API_KEY="synthetic-test-key")
    exec(compile(ast.Module(body=[node], type_ignores=[]), agent.__file__, "exec"), namespace)
    namespace["_get_chat_model"]()
    timeout = captured["timeout"]
    assert timeout.connect == agent.LLM_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == agent.LLM_READ_TIMEOUT_SECONDS
    assert 0 < timeout.connect <= timeout.read
    assert timeout.pool == timeout.connect
