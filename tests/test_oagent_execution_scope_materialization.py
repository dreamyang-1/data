import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    PrimaryIntent,
    TrustedIdentity,
)
from app.services.authorized_scope import bind_authorized_scope


IDENTITY = TrustedIdentity(tenant_id="scope-test", user_id="scope-test")


class RecordingClient:
    def __init__(self) -> None:
        self.calls = []

    async def post(self, base_url, path, payload, **kwargs):
        self.calls.append((base_url, path, payload, kwargs))
        return {"success": False}


def scoped_request(
    *,
    requested: list[int],
    resolved: list[int],
    department: str | None = None,
) -> tuple[ChatRequest, CanonicalAnalysisRequest]:
    chat_values = dict(
        semantic_model_id=81,
        business_domain_ids=requested,
        application_id="scope-app",
        conversation_id="scope-conversation",
        message_id="scope-message",
        question="查询去年江苏省订单笔数",
    )
    if department is not None:
        chat_values["department"] = department
    chat = ChatRequest(**chat_values)
    request = CanonicalAnalysisRequest(
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        original_question=chat.question,
        rewritten_question="查询2025年江苏省订单笔数。",
        primary_intent=PrimaryIntent.METRIC_QUERY,
    )
    bind_authorized_scope(request, chat.authorized_semantic_scope)
    request.resolved_business_domain_ids = resolved
    return chat, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "resolved", "department"),
    [
        ([], [205], None),
        ([205], [205], None),
        ([], [205], "ORG_ADMIN"),
    ],
)
async def test_oagent_single_domain_execution_scope_is_materialized_consistently(
    requested,
    resolved,
    department,
):
    chat, request = scoped_request(
        requested=requested,
        resolved=resolved,
        department=department,
    )
    unchanged = request.model_dump(mode="json")
    client = RecordingClient()

    await HttpDataRetrievalAdapter(
        Settings(env="test"), client
    ).discover_metrics(
        request,
        IDENTITY,
        semantic_model_id=81,
        business_domain_id=205,
    )

    payload = client.calls[0][2]
    assert payload["business_domain_id"] == 205
    assert payload["business_domain_ids"] == [205]
    assert request.model_dump(mode="json") == unchanged
    assert chat.department == (department or "")


@pytest.mark.asyncio
async def test_main_oagent_query_uses_resolved_model_wide_execution_scope():
    _chat, request = scoped_request(requested=[], resolved=[205])
    unchanged = request.model_dump(mode="json")
    client = RecordingClient()

    with pytest.raises(AdapterError) as failure:
        await HttpDataRetrievalAdapter(
            Settings(env="test"), client
        ).query(
            request,
            IDENTITY,
            semantic_model_id=81,
            business_domain_id=205,
        )

    assert failure.value.code == "ASL_GENERATION_FAILED"
    payload = client.calls[0][2]
    assert client.calls[0][1] == "/agent/query"
    assert payload["business_domain_id"] == 205
    assert payload["business_domain_ids"] == [205]
    assert request.model_dump(mode="json") == unchanged


@pytest.mark.asyncio
async def test_oagent_model_wide_multi_domain_execution_fails_closed():
    _chat, request = scoped_request(requested=[], resolved=[205, 206])
    client = RecordingClient()

    with pytest.raises(AdapterError) as failure:
        await HttpDataRetrievalAdapter(
            Settings(env="test"), client
        ).discover_metrics(
            request,
            IDENTITY,
            semantic_model_id=81,
            business_domain_id=None,
        )

    assert failure.value.code == "OAGENT_MULTI_DOMAIN_CONTRACT_UNSUPPORTED"
    assert client.calls == []


@pytest.mark.asyncio
async def test_oagent_unresolved_model_wide_execution_fails_before_call():
    _chat, request = scoped_request(requested=[], resolved=[])
    client = RecordingClient()

    with pytest.raises(AdapterError) as failure:
        await HttpDataRetrievalAdapter(
            Settings(env="test"), client
        ).discover_metrics(
            request,
            IDENTITY,
            semantic_model_id=81,
            business_domain_id=None,
        )

    assert failure.value.code == "OAGENT_EXECUTION_SCOPE_UNRESOLVED"
    assert client.calls == []
