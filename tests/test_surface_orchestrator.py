from datetime import datetime, timezone

import pytest

from test_orchestrator import service
from app.domain.models import ChatRequest, TrustedIdentity, DataQueryResult, Dataset
from app.adapters.base import AdapterError


@pytest.mark.asyncio
async def test_completed_question_uses_surface_executor_and_shared_result_pipeline():
    orchestrator = service()
    orchestrator.settings.surface_asl_execution_enabled = True
    calls = []

    async def query_surface(request, identity, *, mentions):
        calls.append((request.rewritten_question, mentions))
        return DataQueryResult(asl={"version": "2.0", "metrics": [],
            "dimensions": [{"name": "hospital.hospital_name"}], "filters": [],
            "time_context": None, "ambiguity": [], "projection_mode": "DISTINCT"},
            sql="SELECT hospital_name FROM hospital",
            dataset=Dataset(columns=["医院名称"], rows=[{"医院名称": "示例医院"}],
                row_count=1, data_as_of=datetime.now(timezone.utc),
                quality_status="PASS", snapshot_id="surface-test"))

    orchestrator.adapters.query.retrieval.query_surface = query_surface
    chat = ChatRequest(question="请提供医院名称明细", conversation_id="surface-test",
        semantic_model_id=81, message_id="m1", application_id="app")
    chat._completed_question_execution = True
    chat._semantic_extraction_items = ({"surface": "医院", "labels": ("业务对象",)},)
    result = await orchestrator._handle(chat, TrustedIdentity(tenant_id="t", user_id="u"))
    assert len(calls) == 1
    assert calls[0][0] == chat.question
    assert calls[0][1] == [{"text": "医院", "role_hint": "业务对象"}]
    assert result.status == "COMPLETED"
    assert "示例医院" in result.answer


@pytest.mark.asyncio
async def test_surface_ambiguity_uses_existing_pending_clarification_state():
    orchestrator = service()
    orchestrator.settings.surface_asl_execution_enabled = True
    async def query_surface(request, identity, *, mentions):
        raise AdapterError("ASL_AMBIGUOUS", "ambiguous", details=[{
            "type": "entity_role", "phrase": "合作方", "question": "合作方是医院还是经销商？",
            "candidates": ["医院", "经销商"],
        }])
    orchestrator.adapters.query.retrieval.query_surface = query_surface
    chat = ChatRequest(question="请提供医院名称明细", conversation_id="surface-pending",
        semantic_model_id=81, message_id="m1", application_id="app")
    chat._completed_question_execution = True
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    result = await orchestrator._handle(chat, identity)
    assert result.status == "NEEDS_CLARIFICATION", result.model_dump()
    pending = await orchestrator.sessions.get_pending("t", "u", "app", "surface-pending")
    assert pending is not None
    assert pending.request.rewritten_question == chat.question
