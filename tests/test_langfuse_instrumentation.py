from __future__ import annotations

import json
import asyncio
import time
from contextvars import ContextVar

import pytest

from app.config import Settings
from app.observability import langfuse_client as telemetry


class FakeObservation:
    def __init__(self, client=None, name="", parent=None) -> None:
        self.updates: list[dict] = []
        self.client, self.name, self.parent = client, name, parent
        self.started = time.time_ns()
        self.ended = None

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self, *, end_time=None):
        self.ended = end_time or time.time_ns()

    def start_observation(self, **kwargs):
        return self.client.start_observation(parent=self, **kwargs)

    def start_as_current_observation(self, **kwargs):
        return FakeContext(self.start_observation(**kwargs))


class FakeContext:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation

    def __enter__(self):
        self.token = self.observation.client.current.set(self.observation)
        return self.observation

    def __exit__(self, *_args):
        self.observation.end()
        self.observation.client.current.reset(self.token)
        return False


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.observations: list[FakeObservation] = []
        self.current = ContextVar("fake_current", default=None)

    def start_observation(self, parent=None, **kwargs):
        self.calls.append(kwargs)
        observation = FakeObservation(self, kwargs.get("name"), parent or self.current.get())
        self.observations.append(observation)
        return observation

    def start_as_current_observation(self, **kwargs):
        return FakeContext(self.start_observation(**kwargs))


def enable_fake(monkeypatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(telemetry, "_client", client)
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    monkeypatch.setattr(telemetry, "_hash_key", b"test-only-salt")
    monkeypatch.setattr(telemetry, "_structural_by_trace", {})
    monkeypatch.setattr(telemetry, "_structural_activity", {})
    monkeypatch.setattr(telemetry, "_structural_parents", {})
    monkeypatch.setattr(telemetry, "_current_trace_key", lambda: 123)
    return client


def test_test_environment_never_enables_export() -> None:
    settings = Settings(
        env="test",
        langfuse_enabled=True,
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_host="https://langfuse.invalid",
    )

    assert telemetry.configure_langfuse(settings) is False
    assert telemetry.is_enabled() is False


def test_text_metadata_omits_content_by_default(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_capture_content", False)

    value = telemetry.text_metadata("查询上海市销售额")

    assert value["chars"] == 8
    assert value["sha256"]
    assert "content" not in value


def test_explicit_content_capture_still_redacts_credentials(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_capture_content", True)
    monkeypatch.setattr(telemetry, "_max_content_chars", 500)

    value = telemetry.text_metadata("Authorization: Bearer top-secret")

    assert "top-secret" not in value["content"]
    assert "[REDACTED]" in value["content"]


def test_generation_records_usage_without_raw_prompt_or_output(monkeypatch) -> None:
    client = enable_fake(monkeypatch)

    with telemetry.trace_generation(
        name="intent-recognition",
        model="model-x",
        messages=[{"role": "user", "content": "敏感业务问题"}],
    ) as generation:
        generation.set_response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "敏感模型答案"},
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    serialized = json.dumps(
        {"calls": client.calls, "updates": client.observations[0].updates},
        ensure_ascii=False,
    )
    assert "敏感业务问题" not in serialized
    assert "敏感模型答案" not in serialized
    assert client.observations[0].updates[0]["usage_details"]["total"] == 18


def test_stage_tracker_exports_only_bounded_metadata(monkeypatch) -> None:
    client = enable_fake(monkeypatch)
    tracker = telemetry.StageSpanTracker()

    tracker.handle({
        "stage": "SQL_EXECUTION",
        "status": "FAILED",
        "message": "SELECT sensitive_column FROM private_table",
        "task_id": "customer-task-id",
    })

    serialized = json.dumps(
        {"calls": client.calls, "updates": client.observations[0].updates},
        ensure_ascii=False,
    )
    assert "SELECT sensitive_column" not in serialized
    assert "customer-task-id" not in serialized
    assert any(update.get("level") == "ERROR" for update in client.observations[0].updates)


def test_observation_start_failure_does_not_break_business_block(monkeypatch) -> None:
    class BrokenClient:
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(telemetry, "_client", BrokenClient())
    monkeypatch.setattr(telemetry, "_enabled", True)

    with telemetry.observe_span(name="safe-noop") as observation:
        observation.update(output={"status": "COMPLETED"})


def test_all_visible_stages_share_request_parent_and_end_at_own_work(monkeypatch):
    client = enable_fake(monkeypatch)
    with telemetry.observe_span(name="request") as root:
        telemetry.register_structural_span("request", root)
        tracker = telemetry.StageSpanTracker()
        for code in ("INTENT_RECOGNITION", "TASK_PLANNING", "ASL_GENERATION",
                     "SQL_EXECUTION", "RELIABILITY_CHECK", "INSIGHT_ANALYSIS", "FINAL_OUTPUT"):
            tracker.handle({"stage": code, "status": "COMPLETED", "message": "done"})
            time.sleep(.001)
        telemetry.end_structural_spans()
        assert root.ended is None
    stages = client.observations[1:]
    assert [s.name for s in stages] == ["意图识别", "任务拆分与规划", "解析校验", "调度执行", "结果校验", "数据洞察分析", "最终输出"]
    assert all(s.parent is root for s in stages)
    assert stages[0].ended < stages[-1].started
    assert all(s.started <= s.ended for s in stages)
    assert not telemetry._structural_by_trace


@pytest.mark.asyncio
async def test_parallel_tasks_and_requests_keep_stage_and_tool_parents(monkeypatch):
    from app.services.progress import task_progress_scope
    from app.observability.call_timing import track_operation
    client = enable_fake(monkeypatch)
    trace = ContextVar("test_trace")
    monkeypatch.setattr(telemetry, "_current_trace_key", trace.get)

    async def request(trace_id):
        trace.set(trace_id)
        with telemetry.observe_span(name=f"request-{trace_id}") as root:
            telemetry.register_structural_span("request", root)
            async def task(index):
                with task_progress_scope(task_id=f"task-{index}", task_index=index, task_count=2, task_question="查询问题"):
                    with track_operation("upstream", "upstream.sql.execution"):
                        await asyncio.sleep(.001)
                        with telemetry.trace_http_call("POST", "/execute", {"sql": "select 1"}) as span:
                            telemetry.record_http_result(span, {"rows": [{"count": 1}]})
            await asyncio.gather(task(0), task(1))
            telemetry.end_structural_spans()
    await asyncio.gather(request(1), request(2))
    tools = [s for s in client.observations if s.name == "POST /execute"]
    assert len(tools) == 4
    for tool in tools:
        assert tool.parent.name == "SQL执行"
        assert tool.parent.parent.name in {"任务1", "任务2"}
        assert tool.parent.parent.parent.name == "调度执行"
        assert tool.parent.parent.parent.parent.name in {"request-1", "request-2"}
    assert len([s for s in client.observations if s.name == "调度执行"]) == 2
    assert not telemetry._structural_by_trace


@pytest.mark.asyncio
async def test_stream_telemetry_is_queued_without_waiting_for_sdk(monkeypatch):
    from app.api import _forward_traced_progress
    client = enable_fake(monkeypatch)
    tracker = telemetry.StageSpanTracker()
    original = tracker.handle
    def slow_handle(event):
        time.sleep(.1)
        original(event)
    monkeypatch.setattr(tracker, "handle", slow_handle)
    delivered = []
    with telemetry.observe_span(name="request") as root:
        telemetry.register_structural_span("request", root)
        await asyncio.wait_for(_forward_traced_progress(
            tracker, delivered.append, {"stage": "SQL_EXECUTION", "status": "COMPLETED"}
        ), timeout=.03)
        assert delivered
        await asyncio.to_thread(tracker.finish)
        telemetry.end_structural_spans()
    assert any(s.name == "调度执行" and s.parent is root for s in client.observations)


def test_definition_generation_has_only_intent_and_final_stages(monkeypatch):
    client = enable_fake(monkeypatch)
    with telemetry.observe_span(name="request") as root:
        telemetry.register_structural_span("request", root)
        telemetry.ensure_structural_span("comprehension", name="问题理解与补全")
        with telemetry.trace_generation(name="metric-definition-answer", messages=[]) as generation:
            generation.set_response({})
        telemetry.end_structural_spans()
    assert [s.name for s in client.observations if s.parent is root] == ["意图识别", "最终输出"]
    assert client.observations[-1].parent.name == "最终输出"


def test_failed_and_cancelled_operations_preserve_original_exception(monkeypatch):
    client = enable_fake(monkeypatch)
    for error in (ValueError("failure"), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            with telemetry.trace_operation("upstream.sql.execution"):
                raise error
    operations = [s for s in client.observations if s.name == "SQL执行"]
    assert [s.updates[-1]["output"]["status"] for s in operations] == ["FAILED", "CANCELLED"]
    assert all(s.ended for s in operations)
    telemetry.end_structural_spans()


def test_evidence_review_is_grouped_under_validation_inside_synthesis(monkeypatch):
    client = enable_fake(monkeypatch)
    with telemetry.observe_span(name="request") as root:
        telemetry.register_structural_span("request", root)
        with telemetry.trace_operation("analysis.synthesis"):
            with telemetry.trace_generation(name="analysis-synthesis"):
                pass
            with telemetry.trace_generation(name="analysis-evidence-review"):
                pass
        telemetry.end_structural_spans()
    synthesis = next(s for s in client.observations if s.name == "analysis-synthesis")
    review = next(s for s in client.observations if s.name == "analysis-evidence-review")
    assert synthesis.parent.name == "数据洞察生成"
    assert review.parent.name == "结果校验"
    assert review.parent.parent is root


def test_content_capture_redacts_nested_credentials_and_bounds_rows(monkeypatch):
    enable_fake(monkeypatch)
    monkeypatch.setattr(telemetry, "_capture_content", True)
    monkeypatch.setattr(telemetry, "_max_content_chars", 1000)
    result = telemetry.value_metadata({"sql": "select count(*)", "nested": {"access_token": "test-credential"}, "rows": list(range(1000))})
    assert "select count(*)" in result["content"]
    assert "test-credential" not in result["content"]
    assert "[REDACTED]" in result["content"]
    assert len(result["content"]) <= 1000


def test_broken_sdk_does_not_interrupt_timed_work(monkeypatch):
    class BrokenClient:
        def start_observation(self, **kwargs):
            raise RuntimeError("unavailable")
        start_as_current_observation = start_observation
    enable_fake(monkeypatch)
    monkeypatch.setattr(telemetry, "_client", BrokenClient())
    with telemetry.trace_operation("upstream.sql.execution"):
        with telemetry.trace_http_call("POST", "/execute", {}) as span:
            telemetry.record_http_result(span, {"ok": True})
    telemetry.StageSpanTracker().handle({"stage": "SQL_EXECUTION", "status": "FAILED"})


def test_stream_api_exports_complete_trace_without_changing_visible_order(monkeypatch):
    from uuid import uuid4
    from test_api import TestClient, build_test_app
    from app.domain.models import AgentResponse, PrimaryIntent
    from app.services.progress import emit_progress
    app = build_test_app()
    class Workflow:
        async def ainvoke(self, state):
            for code in ("INTENT_RECOGNITION", "TASK_PLANNING", "ASL_GENERATION",
                         "SQL_EXECUTION", "RELIABILITY_CHECK", "INSIGHT_ANALYSIS"):
                await emit_progress(code, "COMPLETED", "步骤完成")
            return {"response": AgentResponse(request_id=uuid4(), conversation_id=state["chat"].conversation_id,
                status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="测试结果")}
    with TestClient(app) as client:
        fake = enable_fake(monkeypatch)
        object.__setattr__(app.state.container, "workflow", Workflow())
        response = client.post("/agent_chat/stream", headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"}, json={
            "semantic_model_id": 81, "application_id": "app", "conversation_id": "trace-order",
            "message_id": "m1", "question": "查询销售额"})
    assert response.status_code == 200
    events = [json.loads(block.removeprefix("data: ")) for block in response.text.strip().split("\n\n")]
    content = "".join(e.get("content", "") for e in events if e.get("type") == "message_chunk")
    names = ["意图识别", "任务拆分与规划", "解析校验", "调度执行", "结果校验", "数据洞察分析", "最终输出"]
    positions = [content.index("#### ◉ " + name) for name in names[:-1]]
    assert positions == sorted(positions)
    assert any(e.get("type") == "complete" and e.get("status") == "COMPLETED" for e in events)
    root = next(s for s in fake.observations if s.name == "data-analysis-request")
    assert [s.name for s in fake.observations if s.parent is root] == names
    final = next(s for s in fake.observations if s.name == "最终输出")
    assert final.updates[-1]["output"]["status"] == "COMPLETED"
    assert all(s.ended for s in fake.observations)
    assert not telemetry._structural_by_trace
