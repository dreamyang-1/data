from datetime import datetime, timezone
import pytest
from app.adapters.asl_notices import binding_notices, attach_binding_notices
from app.domain.models import ChatRequest, TrustedIdentity, DataQueryResult, Dataset
from test_orchestrator import service
from test_surface_asl_client import response, Client, run
from app.services.progress import progress_scope


def repairs():
    return [{"type": "DROP_UNMATCHED_SURFACE_MENTION", "mention": "某品牌",
             "source": "SURFACE_MENTION_RECALL"},
            {"type": "SURFACE_MATCH_NOTICE", "reason": "TIED_CANDIDATES",
             "mention": "某地区", "resolved_field": "hospital.city", "canonical_value": "某地区",
             "source": "SURFACE_MENTION_RECALL", "candidates": [
                 {"field": "hospital.city", "value": "某地区", "score": 1.0},
                 {"field": "dealer.city", "value": "某地区", "score": 1.0}]}]


def test_notice_details_and_advisory_source_only():
    messages = binding_notices(repairs())
    assert "未应用" in messages[0] and "可能扩大" in messages[0]
    assert "dealer.city" in messages[1] and "随机" in messages[1]
    assert binding_notices([{**repairs()[0], "source": "CALLER_CONTRACT"}]) == []


def test_surface_client_preserves_repair_records_without_changing_asl():
    data = response()
    data["asl_repair"] = repairs()
    plan = run(Client(data))
    assert plan["asl_repair"] == repairs()
    assert plan["asl"] == data["result"]


@pytest.mark.asyncio
async def test_nonblocking_notice_reaches_final_answer_and_reliability():
    orchestrator = service()
    orchestrator.settings.surface_asl_execution_enabled = True
    async def query_surface(request, identity, *, mentions):
        result = DataQueryResult(asl={"version": "2.0", "metrics": [],
            "dimensions": [{"name": "hospital.hospital_name"}], "filters": [],
            "time_context": None, "ambiguity": [], "projection_mode": "DISTINCT"},
            sql="SELECT hospital_name FROM hospital",
            dataset=Dataset(columns=["医院名称"], rows=[{"医院名称": "示例医院"}],
                row_count=1, data_as_of=datetime.now(timezone.utc), snapshot_id="notice-test"))
        return attach_binding_notices(result, repairs())
    orchestrator.adapters.query.retrieval.query_surface = query_surface
    chat = ChatRequest(question="请提供南京市哪些医院使用费森尤斯产品。", conversation_id="notice-test",
                       semantic_model_id=81, message_id="m1", application_id="app")
    chat._completed_question_execution = True
    events = []
    with progress_scope(events.append):
        result = await orchestrator._handle(chat, TrustedIdentity(tenant_id="t", user_id="u"))
    assert result.status == "COMPLETED"
    assert "示例医院" in result.answer
    assert "查询条件提示" in result.answer and "dealer.city" in result.answer
    assert "本次未应用该项筛选" in result.answer
    assert any("随机" in text for text in result.reliability.warnings)
    assert result.reliability.level == "LIMITED"
    stages = [event["stage"] for event in events]
    assert stages.index("RELIABILITY_CHECK") < stages.index("INSIGHT_ANALYSIS")
