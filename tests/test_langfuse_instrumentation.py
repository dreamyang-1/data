from __future__ import annotations

import json

from app.config import Settings
from app.observability import langfuse_client as telemetry


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class FakeContext:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation

    def __enter__(self):
        return self.observation

    def __exit__(self, *_args):
        return False


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.observations: list[FakeObservation] = []

    def start_as_current_observation(self, **kwargs):
        self.calls.append(kwargs)
        observation = FakeObservation()
        self.observations.append(observation)
        return FakeContext(observation)


def enable_fake(monkeypatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(telemetry, "_client", client)
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    monkeypatch.setattr(telemetry, "_hash_key", b"test-only-salt")
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
    assert client.observations[0].updates[0]["level"] == "ERROR"


def test_observation_start_failure_does_not_break_business_block(monkeypatch) -> None:
    class BrokenClient:
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(telemetry, "_client", BrokenClient())
    monkeypatch.setattr(telemetry, "_enabled", True)

    with telemetry.observe_span(name="safe-noop") as observation:
        observation.update(output={"status": "COMPLETED"})
