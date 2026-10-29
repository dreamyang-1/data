from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    PrimaryIntent,
    TrustedIdentity,
)
from app.main import create_app
from app.observability.bridge_profile import bridge_capability_profile
from app.observability.call_timing import (
    RequestTimingTracker,
    log_performance_trace,
    timing_scope,
    track_operation,
)
from app.semantic_v2.recognition_client import RecognitionModelClient
from app.services.progress import emit_progress


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        runtime_mode="V2_CONTEXT_V1_EXECUTION",
        adapter_mode="mock",
        session_store_mode="memory",
        long_term_memory_mode="disabled",
        business_question_collection_enabled=False,
        allow_missing_trusted_identity_headers=True,
        trusted_backend_token=SecretStr("test-token"),
        intent_model_enabled=False,
    )


def test_bridge_profile_freezes_mainline_and_lists_requested_timing_points():
    profile = bridge_capability_profile(
        active_runtime_mode="V2_CONTEXT_V1_EXECUTION"
    )
    operations = {
        item["timing_operation"] for item in profile["capabilities"]
    }

    assert profile["active"] is True
    assert profile["pure_v2_execution_expansion"] == "PAUSED"
    assert profile["timing_trace_version"] == "bridge-timing-v1"
    assert profile["semantic_decision_version"] == "semantic-decision-v1"
    assert profile["semantic_handoff"] == {
        "transport_visibility": "INTERNAL_PRIVATE_ATTRIBUTE",
        "accepted_source": "V2_AUTHORIZED_PLAN",
        "accepted_v1_intent_model": "SKIPPED",
        "fallback_source": "V1_SEMANTIC_FALLBACK",
        "fallback_reason_recorded": True,
        "v1_execution_safety_validation": "REQUIRED",
    }
    assert {
        "v2.catalog.load",
        "v2.model.current_turn",
        "v2.model.semantic_edits",
        "semantic.contract.build",
        "semantic.contract.validation",
        "v1.question_rewrite",
        "v1.intent_recognition",
        "v1.task_decomposition",
        "upstream.oagnet.asl_generation",
        "upstream.sql.translation",
        "upstream.sql.execution",
        "validation.result_reliability",
        "analysis.deterministic",
        "analysis.synthesis",
        "mcp.chart_render",
    }.issubset(operations)


def test_stream_complete_exposes_content_free_request_timing_trace():
    class Handler:
        uses_v1_ingress = True

        async def check_message_conflict(self, *_args):
            return None

        async def handle(self, chat, _identity):
            await emit_progress(
                "INTENT_RECOGNITION",
                "RUNNING",
                "processing",
                progress_phase="V2_CONTEXT_START",
            )
            with track_operation("V2_CONTEXT", "v2.catalog.load") as timing:
                timing.mark_first_result()
            return AgentResponse(
                request_id=uuid4(),
                conversation_id=chat.conversation_id,
                status="COMPLETED",
                intent=PrimaryIntent.METRIC_QUERY,
                answer="ok",
            )

    app = create_app(_settings(), isolated_chat_handler=Handler())
    with TestClient(app) as client:
        response = client.post(
            "/agent_chat/stream",
            headers={"Authorization": "Bearer test-token"},
            json={
                "application_id": "app",
                "conversation_id": "timing-conversation",
                "message_id": "timing-message",
                "question": "query",
                "semantic_model_id": 81,
                "business_domain_ids": [205],
            },
        )
        profile_response = client.get(
            "/v1/data-analysis/diagnostics/bridge-profile",
            headers={"Authorization": "Bearer test-token"},
        )

    events = [
        json.loads(block.removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
    ]
    complete = next(item for item in events if item["type"] == "complete")
    trace = complete["performance_trace"]

    assert response.status_code == 200
    assert trace["runtime_mode"] == "V2_CONTEXT_V1_EXECUTION"
    assert trace["terminal_status"] == "COMPLETED"
    assert trace["operations"][0]["operation"] == "v2.catalog.load"
    assert trace["operations"][0]["first_result_after_ms"] is not None
    assert trace["progress"][0]["progress_phase"] == "V2_CONTEXT_START"
    encoded = json.dumps(trace, ensure_ascii=False).casefold()
    assert "query" not in encoded
    assert "sql" not in encoded
    assert profile_response.status_code == 200
    assert profile_response.json()["active"] is True


@pytest.mark.asyncio
async def test_v2_stream_model_records_first_result_and_completion():
    chunks = [
        {"choices": [{"delta": {"content": '{"value":'}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": '"ok"}'}, "finish_reason": "stop"}]},
    ]
    body = "".join(
        "data: " + json.dumps(item) + "\n\n" for item in chunks
    ) + "data: [DONE]\n\n"

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    class OutputModel(BaseModel):
        value: str

    settings = _settings().model_copy(
        update={
            "intent_model_api_key": SecretStr("test-model-key"),
            "intent_model_name": "test-model",
            "intent_model_max_retries": 0,
        }
    )
    client = RecognitionModelClient(
        settings,
        httpx.MockTransport(handler),
        force_stream=True,
    )
    tracker = RequestTimingTracker(runtime_mode=settings.runtime_mode)
    with timing_scope(tracker):
        result = await client.complete(
            stage="v2_current_turn",
            instruction="Return JSON.",
            context={"question": "private business question"},
            output_model=OutputModel,
        )
    trace = tracker.finish("COMPLETED")

    assert result.value == "ok"
    assert len(trace.operations) == 1
    operation = trace.operations[0]
    assert operation.operation == "v2.model.current_turn"
    assert operation.first_result_after_ms is not None
    assert operation.duration_ms >= operation.first_result_after_ms
    assert operation.attributes == {"model": "test-model", "stream": True}
    assert "private business question" not in trace.model_dump_json()


@pytest.mark.asyncio
async def test_http_data_chain_records_each_real_upstream_boundary():
    class StubClient:
        def __init__(self):
            self.responses = iter(
                [
                    {
                        "success": True,
                        "result": json.dumps(
                            {
                                "version": "2.0",
                                "metrics": [
                                    {
                                        "name": "average_transaction_value",
                                        "alias": "average transaction value",
                                    }
                                ],
                                "ambiguity": [],
                            }
                        ),
                    },
                    {"success": True, "sql": "SELECT 1", "modelId": "8"},
                    {
                        "success": True,
                        "sql": "SELECT 1",
                        "data": [{"average transaction value": 1}],
                        "columns": ["average transaction value"],
                        "row_count": 1,
                    },
                ]
            )

        async def post(self, *_args, **_kwargs):
            return next(self.responses)

    request = CanonicalAnalysisRequest(
        conversation_id="timing-data-chain",
        tenant_id="tenant",
        user_id="user",
        original_question="private business question",
        primary_intent=PrimaryIntent.METRIC_QUERY,
    )
    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with timing_scope(tracker):
        result = await HttpDataRetrievalAdapter(
            Settings(_env_file=None, adapter_mode="http"),
            StubClient(),
        ).query(
            request,
            TrustedIdentity(tenant_id="tenant", user_id="user"),
            semantic_model_id=8,
            business_domain_id=13,
        )
    trace = tracker.finish("COMPLETED")

    assert result.dataset.rows == [{"average transaction value": 1}]
    assert [item.operation for item in trace.operations] == [
        "upstream.oagnet.asl_generation",
        "upstream.sql.translation",
        "upstream.sql.execution",
    ]
    assert all(item.first_result_after_ms is not None for item in trace.operations)
    assert "private business question" not in trace.model_dump_json()


def test_request_trace_is_written_to_the_uvicorn_server_log(caplog):
    tracker = RequestTimingTracker(runtime_mode="V2_CONTEXT_V1_EXECUTION")
    with timing_scope(tracker):
        with track_operation("V2_CONTEXT", "v2.catalog.load") as timing:
            timing.mark_first_result()
    trace = tracker.finish("COMPLETED")

    caplog.set_level("INFO", logger="uvicorn.error")
    log_performance_trace(trace)

    assert "bridge_performance_trace=" in caplog.text
    assert "v2.catalog.load" in caplog.text
