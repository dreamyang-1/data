from dataclasses import replace
import inspect
import json
from types import MethodType
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    ChatRequest,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.main import create_app
from app.planning import MultiQuestionPlanner
from app.semantic_v2.authorized_contract import (
    ScopedArtifact,
    contract_digest,
)
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.context_state_store import RedisContextStateStore
from app.semantic_v2.current_catalog import CurrentAuthorizedCatalog
from app.semantic_v2.context_v1_execution import (
    ResolvedContextTurn,
    V2ContextV1ExecutionBridge,
    _revalidate_context_artifacts,
)
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.state_machine import ConversationState
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.question_rewriter import QuestionRewriter
from app.stores import InMemorySessionStore
from test_v2_authorized_catalog_bridge import IDENTITY, provider, request
from test_v2_context_followup_critical_slice import context_case, context_catalog
from test_v2_limited_scalar_deployment import DeploymentRedis
from test_v2_persisted_scalar_api import NOW
from test_v2_raw_turn_recognition import planner as scripted_planner


def response(chat: ChatRequest, answer: str = "V1 result") -> AgentResponse:
    return AgentResponse(
        request_id=uuid4(),
        conversation_id=chat.conversation_id,
        status="COMPLETED",
        intent=PrimaryIntent.DETAIL_QUERY,
        answer=answer,
        semantic_model_id=chat.semantic_model_id,
        requested_business_domain_ids=list(chat.business_domain_ids),
        business_domain_selection_mode=(
            "EXPLICIT" if chat.business_domain_ids else "AUTO"
        ),
    )


def state_artifact(provider, chat: ChatRequest, version: int) -> ScopedArtifact:
    session = ScopedPlanSession(chat, IDENTITY, provider[0])
    state = ConversationState(
        conversation_id=chat.conversation_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        application_id=chat.application_id,
        state_version=version,
    )
    session.accept_catalog()
    return session.seal(kind="CONVERSATION", payload=state)


def handler(provider, redis, v1):
    class NoModel:
        async def complete(self, **_kwargs):
            raise AssertionError("test installs an explicit context result")

    return V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:test",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=provider[0],
        model=NoModel(),
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )


def install_resolution(target, provider, *, completed=None):
    calls = []

    async def resolve(self, chat, identity, snapshot, catalog):
        calls.append((chat, identity, snapshot, catalog))
        question = completed or chat.question
        return (
            ResolvedContextTurn(
                completed_question=question,
                next_state=state_artifact(
                    provider, chat, snapshot.state_version + 1
                ),
                plan_state=None,
                bridge_route="V2_RESOLVED_COMPLETED_QUESTION",
            ),
            {"catalog_version": "request-version"},
        )

    target._resolve = MethodType(resolve, target)
    return calls


@pytest.mark.asyncio
async def test_bridge_changes_only_completed_question_history_and_internal_mode(provider):
    redis = DeploymentRedis()
    seen = []

    async def v1(chat, identity):
        seen.append((chat, identity))
        return response(chat)

    bridge = handler(provider, redis, v1)
    source = request(
        [],
        question="换今年",
        message_id="m2",
        database_id=7,
        knowledge_base_names=["kb-a"],
        dataset_id="dataset-a",
        department="ORG_ADMIN",
        history=[{"role": "user", "content": "older"}],
    )
    install_resolution(
        bridge, provider, completed="查询2026年江苏省订单笔数。"
    )

    result = await bridge.handle(source, IDENTITY)

    assert result.status == "COMPLETED"
    assert len(seen) == 1
    execution, identity = seen[0]
    assert identity == IDENTITY
    assert execution.question == "查询2026年江苏省订单笔数。"
    assert execution.history == []
    assert execution._completed_question_execution is True
    for field in (
        "semantic_model_id",
        "business_domain_id",
        "business_domain_ids",
        "database_id",
        "knowledge_base_names",
        "dataset_id",
        "department",
        "application_id",
        "conversation_id",
        "message_id",
    ):
        assert getattr(execution, field) == getattr(source, field)


@pytest.mark.asyncio
async def test_standalone_new_task_passthrough_reaches_v1_byte_for_byte(provider):
    redis = DeploymentRedis()
    seen = []

    async def v1(chat, _identity):
        seen.append(chat)
        return response(chat)

    bridge = handler(provider, redis, v1)
    question = "查询空心纤维血液透析器产品合作的经销商名单。"
    chat = request(question=question, message_id="standalone")
    install_resolution(bridge, provider)

    await bridge.handle(chat, IDENTITY)

    assert seen[0].question == question
    assert seen[0].history == []


@pytest.mark.asyncio
async def test_same_message_json_sse_style_reuse_calls_context_and_v1_once(provider):
    redis = DeploymentRedis()
    v1_calls = 0

    async def v1(chat, _identity):
        nonlocal v1_calls
        v1_calls += 1
        return response(chat)

    bridge = handler(provider, redis, v1)
    resolve_calls = install_resolution(bridge, provider)
    chat = request(question="查询销售总额", message_id="same-message")

    first = await bridge.handle(chat, IDENTITY)
    await bridge.check_message_conflict(chat, IDENTITY)
    second = await bridge.handle(chat, IDENTITY)

    assert first == second
    assert len(resolve_calls) == 1
    assert v1_calls == 1


@pytest.mark.asyncio
async def test_downstream_failure_is_returned_without_retry_or_prior_result(provider):
    redis = DeploymentRedis()
    calls = 0

    async def v1(chat, _identity):
        nonlocal calls
        calls += 1
        result = response(chat, "upstream failed")
        result.status = "SAFE_FALLBACK"
        result.error_code = "DEPENDENCY_UNAVAILABLE"
        return result

    bridge = handler(provider, redis, v1)
    install_resolution(bridge, provider)

    result = await bridge.handle(
        request(question="换今年", message_id="one-attempt"), IDENTITY
    )

    assert calls == 1
    assert result.status == "SAFE_FALLBACK"
    assert result.error_code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_context_failure_never_calls_v1(provider):
    redis = DeploymentRedis()
    calls = 0

    async def v1(chat, _identity):
        nonlocal calls
        calls += 1
        return response(chat)

    bridge = handler(provider, redis, v1)

    async def fail(self, *_args):
        raise RecognitionFailure("V2_UNRESOLVED_TEST")

    bridge._resolve = MethodType(fail, bridge)
    result = await bridge.handle(
        request(question="那呢", message_id="unresolved"), IDENTITY
    )

    assert calls == 0
    assert result.status == "SAFE_FALLBACK"


@pytest.mark.parametrize(
    "change",
    [
        {"conversation_id": "other"},
        {"application_id": "other"},
        {"semantic_model_id": 82},
        {"business_domain_ids": []},
        {"database_id": 9},
        {"knowledge_base_names": ["other"]},
    ],
)
def test_stable_key_isolates_identity_and_requested_authorization_scope(change):
    store = RedisContextStateStore(
        DeploymentRedis(),
        prefix="youo:data-analysis:v2-context-live:test",
        ttl_seconds=3600,
        idempotency_ttl_seconds=7200,
    )
    baseline = request()
    changed = baseline.model_copy(update=change)
    assert store.key(baseline, IDENTITY) != store.key(changed, IDENTITY)
    other_user = TrustedIdentity(tenant_id="tenant", user_id="other")
    other_tenant = TrustedIdentity(tenant_id="other", user_id="user")
    assert store.key(baseline, IDENTITY) != store.key(baseline, other_user)
    assert store.key(baseline, IDENTITY) != store.key(baseline, other_tenant)


def test_stable_key_has_no_catalog_publication_or_vector_input():
    store = RedisContextStateStore(
        DeploymentRedis(),
        prefix="youo:data-analysis:v2-context-live:test",
        ttl_seconds=3600,
        idempotency_ttl_seconds=7200,
    )
    chat = request()
    expected = contract_digest(
        {
            "schema_namespace": "v2-context-live-state-v1",
            **store.identity(chat, IDENTITY),
        }
    )
    assert store.key(chat, IDENTITY).endswith(expected)
    assert "catalog" not in json.dumps(store.identity(chat, IDENTITY)).lower()
    assert "vector" not in json.dumps(store.identity(chat, IDENTITY)).lower()
    assert "publication" not in json.dumps(store.identity(chat, IDENTITY)).lower()


def _current_catalog_with_fake_authority(*, model_domains):
    calls = []

    class Release:
        @staticmethod
        def catalog_scope(semantic_model_id, business_domain_ids=()):
            domains = list(business_domain_ids)
            return {
                "semantic_model_id": semantic_model_id,
                "business_domain_ids": domains,
                "scope_mode": "EXPLICIT_DOMAINS" if domains else "MODEL_WIDE",
            }

        @classmethod
        def capture_catalog(cls, semantic_model_id, business_domain_ids=()):
            domains = tuple(business_domain_ids)
            calls.append((semantic_model_id, domains))
            return {
                "scope": cls.catalog_scope(semantic_model_id, domains),
                "catalog_version": "current-version",
                "source_identity_hash": "current-source",
                "physical_catalog": {"tables": []},
            }

    class Generation:
        @staticmethod
        def build_catalog_records(_snapshot, _embed):
            return [], {"complete": True}

    class Mysql:
        @staticmethod
        def get_business_domains(semantic_model_id):
            assert semantic_model_id == 81
            return [{"id": value} for value in model_domains]

    catalog = object.__new__(CurrentAuthorizedCatalog)
    catalog._modules = {
        "catalog_release": Release,
        "catalog_generation": Generation,
        "mysql_tool": Mysql,
    }
    return catalog, calls


def test_model_wide_current_catalog_materializes_authorized_model_domain_only():
    catalog, calls = _current_catalog_with_fake_authority(model_domains=[205])

    current = catalog.for_request(81, ())

    assert calls == [(81, (205,))]
    assert current.requested_business_domain_ids == ()
    assert current.resolved_business_domain_ids == (205,)
    assert current._snapshot["scope"] == {
        "semantic_model_id": 81,
        "business_domain_ids": [205],
        "scope_mode": "EXPLICIT_DOMAINS",
    }


def test_explicit_current_catalog_scope_is_not_rematerialized():
    catalog, calls = _current_catalog_with_fake_authority(model_domains=[999])

    current = catalog.for_request(81, (205,))

    assert calls == [(81, (205,))]
    assert current.requested_business_domain_ids == (205,)
    assert current.resolved_business_domain_ids == (205,)


def test_model_wide_context_session_uses_resolved_catalog_without_changing_request(
    provider,
):
    chat = request([], question="查询去年江苏省订单笔数", message_id="model-wide")

    state, plans, pending, provenance = _revalidate_context_artifacts(
        chat,
        IDENTITY,
        provider[0],
        state=None,
        plans=(),
        pending=None,
        resolved_business_domain_ids=(205,),
    )

    assert state is None and plans == () and pending is None
    assert chat.business_domain_ids == []
    assert chat.authorized_semantic_scope.scope_mode == "MODEL_WIDE"
    assert provenance["catalog_version"]


@pytest.mark.asyncio
async def test_catalog_generation_change_keeps_key_and_revalidates_current_bindings(provider):
    chat = request(question="查询销售总额", message_id="catalog-change")
    current = ScopedPlanSession(chat, IDENTITY, provider[0])
    current_context = current.context
    current.accept_catalog()
    old_pin = current_context.catalog_pin.model_copy(
        update={
            "catalog_version": "old-catalog",
            "vector_index_version": "old-vector",
            "catalog_publish_id": "old-publication",
            "activation_id": "old-activation",
        }
    )
    old_context = current_context.model_copy(update={"catalog_pin": old_pin})
    state = ConversationState(
        conversation_id=chat.conversation_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        application_id=chat.application_id,
        state_version=1,
    )
    payload = state.model_dump(mode="json")
    old_artifact = ScopedArtifact(
        kind="CONVERSATION",
        context=old_context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )

    rebound, plans, pending, provenance = _revalidate_context_artifacts(
        chat,
        IDENTITY,
        provider[0],
        state=old_artifact,
        plans=(),
        pending=None,
    )

    assert rebound is not None
    assert rebound.payload == old_artifact.payload
    assert rebound.context == current_context
    assert plans == () and pending is None
    assert provenance["catalog_version"] == current_context.catalog_pin.catalog_version


@pytest.mark.asyncio
async def test_live_bridge_runs_real_v2_new_task_and_followup_state(
    context_catalog,
):
    steps = context_case(4)
    scripted, transport = scripted_planner(context_catalog, steps)
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:integration",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )

    for index, (question, _parse, _draft) in enumerate(steps):
        result = await bridge.handle(
            request(
                question=question,
                message_id=f"context-integration-{index}",
                conversation_id="context-integration",
            ),
            IDENTITY,
        )
        assert result.status == "COMPLETED"

    assert len(executions) == 3
    assert executions[0].question == steps[0][0]
    assert executions[1].history == executions[2].history == []
    assert "2026" in executions[2].question
    assert "2025" not in executions[2].question
    assert len(transport.calls) == 6
    snapshot = await bridge.store.load(
        request(
            question=steps[-1][0],
            message_id="after",
            conversation_id="context-integration",
        ),
        IDENTITY,
    )
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.state_version == 3
    topic = state.topics[state.active_topic_id]
    task = state.tasks[topic.active_task_id]
    assert task.active_version == 3


@pytest.mark.asyncio
async def test_pre_resolved_v1_entry_preserves_fields_and_clears_history():
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    seen = []

    async def fake_handle(chat, identity):
        seen.append((chat, identity))
        return response(chat)

    orchestrator.handle = fake_handle
    source = request(
        question="查询2026年江苏省订单笔数。",
        history=[{"role": "user", "content": "查询去年江苏省订单笔数"}],
        dataset_id="dataset-a",
        database_id=7,
        knowledge_base_names=["kb-a"],
    )

    await DataAnalysisOrchestrator.execute_v1_from_completed_question(
        orchestrator, source, IDENTITY
    )

    execution, identity = seen[0]
    assert identity == IDENTITY
    assert execution.history == []
    assert execution._completed_question_execution is True
    for field in (
        "question",
        "semantic_model_id",
        "business_domain_ids",
        "database_id",
        "knowledge_base_names",
        "dataset_id",
    ):
        assert getattr(execution, field) == getattr(source, field)


def test_live_bridge_api_uses_original_v1_ingress(monkeypatch):
    calls = []

    class Handler:
        uses_v1_ingress = True

        async def handle(self, chat, identity):
            calls.append(("handle", chat.question, identity.user_id))
            return response(chat)

        async def check_message_conflict(self, *_args):
            return None

    async def collect(_request, chat):
        calls.append(("collect", chat.question))

    async def bind(_request, chat, _identity):
        calls.append(("spreadsheet", chat.question))

    monkeypatch.setattr("app.api._collect_business_question", collect)
    monkeypatch.setattr("app.api.bind_chat_spreadsheet", bind)
    settings = Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        session_store_mode="memory",
        long_term_memory_mode="disabled",
        business_question_collection_enabled=False,
        allow_missing_trusted_identity_headers=True,
        trusted_backend_token=SecretStr("test-token"),
    )
    app = create_app(settings, isolated_chat_handler=Handler())
    with TestClient(app) as client:
        result = client.post(
            "/agent_chat",
            json={
                "conversation_id": "conversation",
                "message_id": "message",
                "question": "查询销售总额",
                "application_id": "app",
                "semantic_model_id": 81,
                "business_domain_ids": [205],
            },
            headers={"Authorization": "Bearer test-token"},
        )
    assert result.status_code == 200
    assert [item[0] for item in calls] == ["collect", "spreadsheet", "handle"]


def test_json_then_sse_same_message_reuses_live_bridge_response(provider):
    redis = DeploymentRedis()
    v1_calls = []

    async def v1(chat, _identity):
        v1_calls.append(chat.question)
        return response(chat)

    bridge = handler(provider, redis, v1)
    resolve_calls = install_resolution(bridge, provider)
    settings = Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        session_store_mode="memory",
        long_term_memory_mode="disabled",
        business_question_collection_enabled=False,
        allow_missing_trusted_identity_headers=True,
        trusted_backend_token=SecretStr("test-token"),
    )
    app = create_app(settings, isolated_chat_handler=bridge)
    payload = {
        "conversation_id": "json-sse-conversation",
        "message_id": "json-sse-message",
        "question": "查询销售总额",
        "application_id": "app",
        "semantic_model_id": 81,
        "business_domain_ids": [205],
    }
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        first = client.post("/agent_chat", json=payload, headers=headers)
        with client.stream(
            "POST", "/agent_chat/stream", json=payload, headers=headers
        ) as replay:
            replay_body = replay.read().decode("utf-8")

    assert first.status_code == 200
    assert replay.status_code == 200
    assert '"type": "complete"' in replay_body
    assert len(resolve_calls) == 1
    assert v1_calls == ["查询销售总额"]


def test_live_bridge_source_has_no_cutover_or_limited_execution_dependencies():
    from app.semantic_v2 import context_v1_execution as live_bridge

    source = inspect.getsource(live_bridge)
    forbidden = (
        "CatalogPinIdentity",
        "RedisScalarSessionStore",
        "DemoExecutionEnvelope",
        "AUTO_REFRESH",
        "CatalogPublicationRuntime",
        "demo_catalog_fallback",
        "demo_retry_without_time",
        "prior_successful_result",
    )
    assert all(name not in source for name in forbidden)


class _CapturingQueryTool:
    def __init__(self, delegate):
        self.delegate = delegate
        self.requests: list[CanonicalAnalysisRequest] = []

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        return await self.delegate.query(request, identity, **kwargs)

    async def discover_metrics(self, request, identity, **kwargs):
        return await self.delegate.discover_metrics(request, identity, **kwargs)

    async def discover_attribute_details(self, request, identity, **kwargs):
        return await self.delegate.discover_attribute_details(
            request, identity, **kwargs
        )


class _CountingClassifier(RuleBasedIntentClassifier):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.rules = self

    def classify(self, question, identity, conversation_id):
        self.calls += 1
        return super().classify(question, identity, conversation_id)


class _CountingPlanner(MultiQuestionPlanner):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = 0

    async def plan(self, question):
        self.calls += 1
        return await super().plan(question)


def _parity_orchestrator():
    settings = Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        multi_question_enabled=True,
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    base = build_mock_adapters()
    query = _CapturingQueryTool(base.query)
    adapters = replace(base, semantic_query=query)
    classifier = _CountingClassifier()
    planner = _CountingPlanner(settings)
    orchestrator = DataAnalysisOrchestrator(
        settings=settings,
        classifier=classifier,
        adapters=adapters,
        sessions=InMemorySessionStore(),
        task_planner=planner,
        question_rewriter=QuestionRewriter(None),
    )
    return orchestrator, query, classifier, planner


@pytest.mark.asyncio
async def test_pure_v1_and_bridge_passthrough_have_execution_contract_parity():
    question = "查询空心纤维血液透析器产品合作的经销商名单。"
    chat = ChatRequest(
        application_id="app-parity",
        conversation_id="conversation-parity",
        message_id="message-parity",
        question=question,
        semantic_model_id=81,
        business_domain_ids=[205],
        database_id=58,
        knowledge_base_names=["business-kb"],
        department="ORG_ADMIN",
        history=[],
    )
    pure, pure_query, pure_classifier, pure_planner = _parity_orchestrator()
    bridged, bridged_query, bridged_classifier, bridged_planner = (
        _parity_orchestrator()
    )

    pure_response = await pure.handle(chat, IDENTITY)
    bridge_response = await bridged.execute_v1_from_completed_question(
        chat, IDENTITY
    )

    assert pure_response.status == bridge_response.status == "COMPLETED"
    assert pure_classifier.calls > 0 and bridged_classifier.calls > 0
    assert pure_planner.calls == bridged_planner.calls == 1
    assert len(pure_query.requests) == len(bridged_query.requests) == 1
    left, right = pure_query.requests[0], bridged_query.requests[0]
    for field in (
        "semantic_model_id",
        "business_domain_ids",
        "resolved_business_domain_ids",
        "business_domain_selection_mode",
        "database_id",
        "knowledge_base_names",
        "source_dataset_id",
        "primary_intent",
        "metrics",
        "dimensions",
        "filters",
        "time_range",
        "entity",
        "fields",
    ):
        assert getattr(right, field) == getattr(left, field), field
    assert left.original_question == right.original_question == question
    assert left.rewritten_question == right.rewritten_question
