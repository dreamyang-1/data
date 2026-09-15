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
    EvidenceItem,
    MetricRef,
    PrimaryIntent,
    SemanticFilterBinding,
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
from app.semantic_v2.context_question import (
    _preferred_context_attribute_code,
    _replace_filter,
    build_context_question,
    canonical_matches_execution,
    is_contextual_ellipsis,
    is_self_contained_execution_question,
    resolve_context_references,
)
from app.semantic_v2.models import ContextQuestionFilter, ContextQuestionState
from app.semantic_v2.current_catalog import CurrentAuthorizedCatalog
from app.semantic_v2.context_v1_execution import (
    ResolvedContextTurn,
    V2ContextV1ExecutionBridge,
    _revalidate_context_artifacts,
)
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.state_machine import ConversationState
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from app.services.question_rewriter import QuestionRewriter
from app.stores import InMemorySessionStore
from test_v2_authorized_catalog_bridge import (
    IDENTITY,
    authority,
    provider,
    publish,
    request,
    reseal,
)
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


def handler(
    provider,
    redis,
    v1,
    *,
    v1_context_reader=None,
    v1_context_value_resolver=None,
    v1_pending_answer_probe=None,
    v1_pending_executor=None,
):
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
        v1_context_reader=v1_context_reader,
        v1_context_value_resolver=v1_context_value_resolver,
        v1_pending_answer_probe=v1_pending_answer_probe,
        v1_pending_executor=v1_pending_executor,
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


def query_response(chat: ChatRequest, answer: str = "V1 query result") -> AgentResponse:
    result = response(chat, answer)
    result.evidence = [EvidenceItem(
        evidence_id="query-result",
        kind="QUERY_RESULT",
        source_ref="data-source:test",
        payload={"row_count": 1},
    )]
    result.dataset_id = "dataset-result"
    return result


def v1_context(chat: ChatRequest, *, filter_value: str) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        original_question=chat.question,
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=chat.semantic_model_id,
        business_domain_ids=list(chat.business_domain_ids),
        entity="经销商",
        fields=["经销商名称"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": filter_value}],
        semantic_filter_bindings=[SemanticFilterBinding(
            filter_index=0,
            input_value=filter_value,
            canonical_value=filter_value,
            canonical_name="商品名称",
            attribute_code="product_name",
            score=1.0,
        )],
    )


@pytest.mark.asyncio
async def test_uploaded_file_bypasses_v2_semantic_recognition_and_reaches_v1(
    provider,
):
    calls = []

    async def v1(chat, identity):
        calls.append((chat, identity))
        return response(chat, "文件分析完成")

    bridge = handler(provider, DeploymentRedis(), v1)
    current = request(
        question="分析一下",
        message_id="uploaded-file-m1",
        temp_file_paths=["uploads/E-Commerce.xlsx"],
        mcp=[{
            "mcp_server_url": "https://mcp.example/sse",
            "connect_type": "sse",
        }],
    )

    result = await bridge.handle(current, IDENTITY)

    assert result.answer == "文件分析完成"
    assert len(calls) == 1
    assert calls[0][0].temp_file_paths == ["uploads/E-Commerce.xlsx"]
    assert calls[0][0].mcp[0].connect_type == "sse"
    assert calls[0][0]._completed_question_execution is True


def publish_province_dimension(provider, *, publication_id: str) -> None:
    source = authority()
    document = source["documents"][0]
    document["entities"][0]["attributes"].append({
        "attribute_id": 1299,
        "attr_code": "province_name",
        "attr_name": "省份名称",
        "is_main_attribute": False,
        "field_mapping": "hospitals.province_name",
    })
    source["physical_catalog"]["tables"][0]["fields"].append({
        "field_id": 1299,
        "field_name": "province_name",
        "table_id": 1,
    })
    document["dimensions"].append({
        "dim_code": "province",
        "dim_name": "省份",
        "synonyms": ["省"],
        "bind_entities": [{
            "entity": "205",
            "attr": "1299",
            "businessDomain": "205",
        }],
    })
    provider[-1][(81, (205,))] = reseal(source)
    publish(provider[0], publication_id=publication_id)


@pytest.mark.parametrize(
    "request_change",
    [
        {"database_id": 99},
        {"knowledge_base_names": ["other-kb"]},
        {"source_dataset_id": "other-dataset"},
        {"business_domain_ids": [999]},
    ],
)
def test_v1_context_evidence_requires_the_exact_v1_retrieval_request(request_change):
    chat = request(
        database_id=7,
        knowledge_base_names=["kb-a"],
        dataset_id="dataset-a",
    )
    candidate = v1_context(chat, filter_value="空心纤维血液透析器")
    candidate = candidate.model_copy(update={
        "database_id": chat.database_id,
        "knowledge_base_names": list(chat.knowledge_base_names),
        "source_dataset_id": chat.dataset_id,
        **request_change,
    })

    assert canonical_matches_execution(
        candidate, chat=chat, identity=IDENTITY
    ) is False


def test_v1_context_evidence_accepts_output_dataset_from_the_exact_execution():
    chat = request(business_domain_ids=[])
    response_for_execution = query_response(chat)
    candidate = v1_context(chat, filter_value="空心纤维血液透析器").model_copy(
        update={
            "request_id": response_for_execution.request_id,
            "resolved_business_domain_ids": [205],
            "business_domain_selection_mode": "MODEL_WIDE",
            "source_dataset_id": response_for_execution.dataset_id,
        }
    )

    assert canonical_matches_execution(
        candidate,
        chat=chat,
        identity=IDENTITY,
        response=response_for_execution,
    ) is True

    unrelated = response_for_execution.model_copy(update={"request_id": uuid4()})
    assert canonical_matches_execution(
        candidate,
        chat=chat,
        identity=IDENTITY,
        response=unrelated,
    ) is False


def test_context_frame_keeps_query_object_separate_from_metric_calculation_subject():
    chat = request(
        question="统计上海市各个经销商的区域医院覆盖率",
        message_id="coverage-context-frame",
    )
    canonical = CanonicalAnalysisRequest(
        conversation_id=chat.conversation_id,
        application_id=chat.application_id,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        original_question=chat.question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=chat.semantic_model_id,
        business_domain_ids=list(chat.business_domain_ids),
        metrics=[MetricRef(
            input="区域医院覆盖率",
            canonical_name="区域医院覆盖率",
        )],
        entity="经销商",
        metric_subject_entity="hospital",
        dimensions=["dealer"],
        filters=[{
            "field": "dim_city.city_name",
            "operator": "EQ",
            "value": "上海市",
        }],
    )

    frame = build_context_question(
        chat=chat,
        parse=None,
        v1_request=canonical,
        catalog_version="current",
    )

    assert frame.entity == "经销商"
    assert frame.dimensions == ["dealer"]
    assert frame.metrics == ["区域医院覆盖率"]
    assert "metric_subject_entity" not in frame.model_dump(mode="json")


@pytest.mark.asyncio
async def test_bridge_streams_context_progress_before_resolution(provider):
    redis = DeploymentRedis()

    async def v1(chat, _identity):
        return response(chat)

    bridge = handler(provider, redis, v1)
    install_resolution(bridge, provider)
    events = []

    with progress_scope(events.append):
        result = await bridge.handle(
            request(
                question="查询去年江苏省订单笔数",
                message_id="context-progress",
            ),
            IDENTITY,
        )

    assert result.status == "COMPLETED"
    assert events[0]["stage"] == "INTENT_RECOGNITION"
    assert events[0]["status"] == "RUNNING"
    assert events[0]["progress_phase"] == "V2_CONTEXT_START"
    assert "正在理解当前问题" in events[0]["message"]
    assert events[1]["progress_phase"] == "V2_CONVERSATION_STATE_READY"
    assert events[1]["message"] == "对话状态识别：独立新问题。"
    assert events[1]["resolution_source"] == "DETERMINISTIC_EMPTY_CONTEXT"
    assert events[2]["progress_phase"] == "V2_SEMANTIC_CATALOG_READY"
    assert events[2]["message"] == (
        "业务域语义目录已加载，正在提取当前问题的查询要素。"
    )
    resolved_context = next(
        event for event in events
        if event.get("progress_phase") == "V2_RESOLVED_INTENT_CONTEXT_READY"
    )
    assert resolved_context["stage"] == "INTENT_RECOGNITION"
    assert resolved_context["status"] == "RUNNING"
    assert "用户原始问题：查询去年江苏省订单笔数" in resolved_context["message"]
    assert "补全后的问题：" in resolved_context["message"]
    assert "业务域：" in resolved_context["message"]


@pytest.mark.parametrize(
    "question",
    [
        "查询去年江苏省订单笔数",
        "北京华康容信医疗器械有限公司销售了哪些产品？",
        "统计各医院等级的订单笔数。",
    ],
)
def test_self_contained_execution_question_accepts_complete_current_requests(question):
    assert is_self_contained_execution_question(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "费森尤斯呢",
        "换今年",
        "那订单笔数是多少？",
        "它主要供货哪些医院？",
        "这些医院里采购金额最高的是哪家？",
        "不要订单笔数",
    ],
)
def test_self_contained_execution_question_rejects_context_dependent_turns(question):
    assert is_self_contained_execution_question(question) is False


@pytest.mark.asyncio
async def test_complete_current_question_uses_v1_when_current_turn_schema_fails(provider):
    redis = DeploymentRedis()
    executions = []

    class SchemaFailingModel:
        async def complete(self, **_kwargs):
            raise RecognitionFailure("V2_MODEL_DYNAMIC_SCHEMA_VIOLATION")

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:complete-current-fallback",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=provider[0],
        model=SchemaFailingModel(),
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    chat = request(
        question="北京华康容信医疗器械有限公司销售了哪些产品？",
        message_id="complete-current-schema-failure",
        conversation_id="complete-current-schema-failure",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[0].question == chat.question
    assert executions[0].history == []
    snapshot = await bridge.store.load(chat, IDENTITY)
    assert snapshot.message(chat.message_id)["bridge_route"] == (
        "V1_EXECUTION_FALLBACK_NEW_TASK"
    )
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.state_version == 1
    assert state.active_topic_id is not None
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    current = next(
        item for item in task.versions if item.version == task.active_version
    )
    assert current.context_question.execution_question == chat.question


@pytest.mark.asyncio
async def test_complete_current_question_fallback_restores_existing_task_bindings(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, _transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:stateful-current-fallback",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "stateful-complete-current-fallback"
    first = request(
        question=first_step[0],
        message_id="stateful-current-first",
        conversation_id=conversation_id,
    )
    assert (await bridge.handle(first, IDENTITY)).status == "COMPLETED"

    class SchemaFailingModel:
        async def complete(self, **_kwargs):
            raise RecognitionFailure("V2_MODEL_DYNAMIC_SCHEMA_VIOLATION")

    bridge.model = SchemaFailingModel()
    second = request(
        question="查询测试医院采购了哪些产品？",
        message_id="stateful-current-second",
        conversation_id=conversation_id,
    )
    result = await bridge.handle(second, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == second.question
    assert executions[-1].history == []
    snapshot = await bridge.store.load(second, IDENTITY)
    assert snapshot.message(second.message_id)["bridge_route"] == (
        "V1_EXECUTION_FALLBACK_NEW_TASK"
    )
    state = ConversationState.model_validate(snapshot.state.payload)
    assert len(state.tasks) == 2
    active = state.tasks[state.topics[state.active_topic_id].active_task_id]
    version = next(
        item for item in active.versions if item.version == active.active_version
    )
    assert version.context_question.execution_question == second.question


def install_fallback_resolution(target, provider):
    async def resolve(self, chat, identity, snapshot, catalog):
        if snapshot.state is None:
            next_state = state_artifact(
                provider, chat, snapshot.state_version + 1
            )
        else:
            current = ConversationState.model_validate(snapshot.state.payload)
            material = current.model_dump(mode="python")
            material["state_version"] = current.state_version + 1
            material["active_topic_id"] = None
            material["recent_turn_ids"] = [
                *current.recent_turn_ids, chat.message_id
            ][-100:]
            session = ScopedPlanSession(chat, identity, catalog)
            session.accept_catalog()
            next_state = session.seal(
                kind="CONVERSATION",
                payload=ConversationState.model_validate(material),
            )
        return (
            ResolvedContextTurn(
                completed_question=chat.question,
                next_state=next_state,
                plan_state=None,
                bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
                standalone_parse=None,
                fallback_reason="V2_QUERY_SHAPE_CONFLICT",
            ),
            {"catalog_version": "request-version"},
        )

    target._resolve = MethodType(resolve, target)


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
async def test_invalid_request_catalog_returns_visible_safe_fallback_without_v1(provider):
    redis = DeploymentRedis()
    v1_calls = []

    class InvalidCatalog:
        def for_request(self, _semantic_model_id, _business_domain_ids):
            raise RuntimeError("CATALOG_VALUE_SOURCE_MAPPING_INVALID")

    async def v1(chat, _identity):
        v1_calls.append(chat)
        return response(chat)

    bridge = handler((InvalidCatalog(),), redis, v1)
    chat = request(
        question="袁飞在2026年5月1日至5月10日设备维修的总时长是多少？",
        message_id="invalid-request-catalog",
        semantic_model_id=91,
        domains=(233,),
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    assert result.error_code == "SEMANTIC_CATALOG_INVALID"
    assert "语义模型目录配置不完整" in result.answer
    assert v1_calls == []
    snapshot = await bridge.store.load(chat, IDENTITY)
    record = snapshot.message(chat.message_id)
    assert record["status"] == "SUCCEEDED"
    assert record["response"]["error_code"] == "SEMANTIC_CATALOG_INVALID"


def test_invalid_request_catalog_stream_has_visible_answer_and_complete_event(provider):
    class InvalidCatalog:
        def for_request(self, _semantic_model_id, _business_domain_ids):
            raise RuntimeError("CATALOG_VALUE_SOURCE_MAPPING_INVALID")

    async def v1(chat, _identity):
        raise AssertionError("invalid catalog must not enter V1 execution")

    bridge = handler((InvalidCatalog(),), DeploymentRedis(), v1)
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
        "application_id": "eam-agent",
        "conversation_id": "invalid-catalog-stream",
        "message_id": "invalid-catalog-stream-message",
        "question": "袁飞在2026年5月1日至5月10日设备维修的总时长是多少？",
        "semantic_model_id": 91,
        "business_domain_ids": [233],
    }

    with TestClient(app) as client:
        response = client.post(
            "/agent_chat/stream",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert "当前语义模型目录配置不完整" in response.text
    assert '"error_code": "SEMANTIC_CATALOG_INVALID"' in response.text
    assert '"type": "complete"' in response.text
    assert '"type": "error"' not in response.text


@pytest.mark.asyncio
async def test_unexpected_request_catalog_error_is_not_hidden(provider):
    class BrokenCatalog:
        def for_request(self, _semantic_model_id, _business_domain_ids):
            raise RuntimeError("unexpected database failure")

    async def v1(chat, _identity):
        raise AssertionError("unexpected catalog failure must not enter V1")

    bridge = handler((BrokenCatalog(),), DeploymentRedis(), v1)

    with pytest.raises(RuntimeError, match="unexpected database failure"):
        await bridge.handle(
            request(message_id="unexpected-request-catalog"), IDENTITY
        )


@pytest.mark.asyncio
async def test_successful_v1_fallback_publishes_context_task_and_replaces_product(
    provider,
):
    redis = DeploymentRedis()
    executions = []
    first_question = "查询空心纤维血液透析器产品合作的经销商名单。"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="空心纤维血液透析器")

    async def resolve_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        if surface != "费森尤斯" or expected_family != "COMMERCIAL_PRODUCT":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="母厂牌",
            attribute_code="parent_brand",
            score=1.0,
        )

    bridge = handler(
        provider,
        redis,
        v1,
        v1_context_reader=read_context,
        v1_context_value_resolver=resolve_value,
    )
    install_fallback_resolution(bridge, provider)
    first = request(
        question=first_question,
        message_id="context-question-first",
        conversation_id="context-question-conversation",
    )

    await bridge.handle(first, IDENTITY)

    snapshot = await bridge.store.load(first, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.state_version == 1
    assert len(state.tasks) == 1
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[0].context_question
    assert task.status == "PROVISIONAL"
    assert frame.execution_question == first_question
    assert frame.filters[0].surface == "空心纤维血液透析器"
    assert executions[0].question == first_question

    # Restore the real bridge resolver. V1's existing entity retriever supplies
    # the semantic evidence; V1 still grounds the completed full question.
    del bridge._resolve
    followup = request(
        question="费森尤斯呢",
        message_id="context-question-followup",
        conversation_id=first.conversation_id,
    )

    result = await bridge.handle(followup, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[1].question == "查询费森尤斯产品合作的经销商名单。"
    assert executions[1].history == []
    updated = await bridge.store.load(followup, IDENTITY)
    updated_state = ConversationState.model_validate(updated.state.payload)
    updated_task = updated_state.tasks[task.task_id]
    assert updated_task.active_version == 2
    updated_frame = updated_task.versions[-1].context_question
    assert updated_frame.filters[0].attribute_code == "parent_brand"
    assert updated_frame.last_edit.operation == "REPLACE"
    assert updated_frame.last_edit.slot == "filter_expression"


@pytest.mark.asyncio
async def test_relationship_anchor_prefers_proven_actor_and_singular_pronoun(
    context_catalog,
):
    redis = DeploymentRedis()
    executions = []
    company = "上海福荫商贸有限公司"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    async def read_context(chat, _identity):
        # Reproduce the live V1 evidence defect: the relation verb was folded
        # into a product value even though the original question names a dealer.
        return v1_context(chat, filter_value="福荫商贸有限公司供货")

    async def resolve_value(
        _chat, _identity, surface, expected_family, _preferred_attribute_code=None
    ):
        if surface != company or expected_family != "PARTNER":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="经销商名称",
            attribute_code="dealer_name",
            score=1.0,
            business_domain_id=205,
        )

    bridge = handler(
        context_catalog,
        redis,
        v1,
        v1_context_reader=read_context,
        v1_context_value_resolver=resolve_value,
    )
    install_fallback_resolution(bridge, context_catalog)
    conversation_id = "relationship-actor-pronoun"
    first = request(
        question=f"{company}供货哪些医院？",
        message_id="relationship-actor-first",
        conversation_id=conversation_id,
    )
    assert (await bridge.handle(first, IDENTITY)).status == "COMPLETED"
    del bridge._resolve

    snapshot = await bridge.store.load(first, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        (company, "PARTNER"),
    ]

    result = await bridge.handle(request(
        question="那它的订单笔数是多少？",
        message_id="relationship-actor-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == f"查询{company}的订单笔数。"
    assert executions[-1].history == []


@pytest.mark.asyncio
async def test_subjectless_relationship_followups_reuse_one_proven_actor(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []
    company = "杭州琅骏医疗科技有限公司"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            source_dataset_id=execution_responses[-1].dataset_id,
            entity="商品",
            fields=["商品名称"],
            dimensions=["商品"],
            filters=[{
                "field": "经销商名称",
                "operator": "EQ",
                "value": company,
            }],
            semantic_filter_bindings=[SemanticFilterBinding(
                filter_index=0,
                input_value=company,
                canonical_value=company,
                canonical_name="经销商名称",
                attribute_code="dealer_name",
                score=1.0,
                business_domain_id=205,
            )],
        )

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "relationship-target-ellipsis"
    await bridge.handle(request(
        question=f"{company}销售了哪些产品？",
        message_id="relationship-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    hospital = await bridge.handle(request(
        question="它主要供货哪些医院？",
        message_id="relationship-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    manufacturer = await bridge.handle(request(
        question="合作的厂家有哪些？",
        message_id="relationship-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert hospital.status == manufacturer.status == "COMPLETED"
    assert executions[-2].question == f"{company}主要供货哪些医院？"
    assert executions[-1].question == f"查询{company}合作的厂家有哪些。"
    assert executions[-2].history == executions[-1].history == []

    snapshot = await bridge.store.load(request(
        question="inspect",
        message_id="relationship-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    assert task.active_version == 3
    assert task.versions[-2].context_question.entity == "医院"
    assert task.versions[-2].context_question.metrics == []
    assert task.versions[-2].context_question.dimensions == ["医院"]
    assert task.versions[-1].context_question.entity == "厂家"
    assert task.versions[-1].context_question.dimensions == ["厂家"]


@pytest.mark.asyncio
async def test_subjectless_relationship_followup_does_not_guess_between_actors(
    provider,
):
    redis = DeploymentRedis()
    calls = 0
    execution_responses = []

    async def v1(chat, _identity):
        nonlocal calls
        calls += 1
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            semantic_model_id=chat.semantic_model_id,
            source_dataset_id=execution_responses[-1].dataset_id,
            filters=[
                {"field": "商品名称", "operator": "EQ", "value": "测试产品"},
                {"field": "医院名称", "operator": "EQ", "value": "测试医院"},
            ],
            semantic_filter_bindings=[
                SemanticFilterBinding(
                    filter_index=0,
                    input_value="测试产品",
                    canonical_value="测试产品",
                    canonical_name="商品名称",
                    attribute_code="product_name",
                    score=1.0,
                ),
                SemanticFilterBinding(
                    filter_index=1,
                    input_value="测试医院",
                    canonical_value="测试医院",
                    canonical_name="医院名称",
                    attribute_code="hospital_name",
                    score=1.0,
                ),
            ],
        )

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "relationship-ambiguous-actor"
    await bridge.handle(request(
        question="查询测试医院采购测试产品的情况。",
        message_id="relationship-ambiguous-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="合作的厂家有哪些？",
        message_id="relationship-ambiguous-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert calls == 1


@pytest.mark.asyncio
async def test_product_replacement_selects_product_from_multiple_filter_families(
    provider,
):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []
    first_question = "查询上海地区空心纤维血液透析器产品合作的经销商名单。"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            resolved_business_domain_ids=[205],
            source_dataset_id=execution_responses[-1].dataset_id,
            entity="经销商",
            fields=["经销商名称"],
            filters=[
                {"field": "省份名称", "operator": "EQ", "value": "上海"},
                {
                    "field": "商品名称",
                    "operator": "EQ",
                    "value": "空心纤维血液透析器",
                },
            ],
            semantic_filter_bindings=[
                SemanticFilterBinding(
                    filter_index=0,
                    input_value="上海",
                    canonical_value="上海",
                    canonical_name="省份名称",
                    attribute_code="province_name",
                    score=1.0,
                    business_domain_id=205,
                ),
                SemanticFilterBinding(
                    filter_index=1,
                    input_value="空心纤维血液透析器",
                    canonical_value="空心纤维血液透析器",
                    canonical_name="商品名称",
                    attribute_code="product_name",
                    score=1.0,
                    business_domain_id=205,
                ),
            ],
        )

    resolver_calls = []

    async def resolve_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        resolver_calls.append((surface, expected_family))
        if surface != "费森尤斯" or expected_family != "COMMERCIAL_PRODUCT":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="母厂牌",
            attribute_code="parent_brand",
            score=1.0,
            business_domain_id=205,
        )

    bridge = handler(
        provider,
        redis,
        v1,
        v1_context_reader=read_context,
        v1_context_value_resolver=resolve_value,
    )
    install_fallback_resolution(bridge, provider)
    first = request(
        question=first_question,
        message_id="multi-filter-first",
        conversation_id="multi-filter-conversation",
        business_domain_ids=[],
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    result = await bridge.handle(
        request(
            question="费森尤斯呢",
            message_id="multi-filter-followup",
            conversation_id=first.conversation_id,
            business_domain_ids=[],
        ),
        IDENTITY,
    )

    assert result.status == "COMPLETED"
    assert executions[-1].question == "查询上海地区费森尤斯产品合作的经销商名单。"
    assert ("费森尤斯", "REGION") in resolver_calls
    assert ("费森尤斯", "COMMERCIAL_PRODUCT") in resolver_calls
    snapshot = await bridge.store.load(first, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("上海", "REGION"),
        ("费森尤斯", "COMMERCIAL_PRODUCT"),
    ]


@pytest.mark.asyncio
async def test_filter_replacement_remains_blocked_when_value_matches_two_families():
    frame = ContextQuestionState(
        original_question="查询上海地区旧商品合作的经销商名单。",
        execution_question="查询上海地区旧商品合作的经销商名单。",
        filters=[
            ContextQuestionFilter(
                surface="上海",
                canonical_value="上海",
                canonical_name="省份名称",
                attribute_code="province_name",
                semantic_family="REGION",
                evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
            ),
            ContextQuestionFilter(
                surface="旧商品",
                canonical_value="旧商品",
                canonical_name="商品名称",
                attribute_code="product_name",
                semantic_family="COMMERCIAL_PRODUCT",
                evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
            ),
        ],
        source_message_id="ambiguous-first",
    )

    async def ambiguous_resolver(
        surface, expected_family, _preferred_attribute_code=None
    ):
        field = {
            "REGION": ("省份名称", "province_name"),
            "COMMERCIAL_PRODUCT": ("商品名称", "product_name"),
        }.get(expected_family)
        if field is None:
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name=field[0],
            attribute_code=field[1],
            score=1.0,
            business_domain_id=205,
        )

    assert await _replace_filter(frame, "同名值呢", ambiguous_resolver) is None


@pytest.mark.asyncio
async def test_structured_task_filter_restriction_adds_then_replaces_one_family(
    context_catalog,
):
    """Short value edits use semantic families and preserve the rest of TaskState."""

    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def resolve_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        if expected_family != "REGION" or surface not in {"广东省", "江苏省"}:
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="省份名称",
            attribute_code="province_name",
            score=1.0,
            business_domain_id=205,
        )

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:structured-filter-edit",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        v1_context_value_resolver=resolve_value,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "structured-filter-edit"

    first = await bridge.handle(request(
        question=first_step[0],
        message_id="structured-filter-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    add = await bridge.handle(request(
        question="只看广东省的。",
        message_id="structured-filter-add",
        conversation_id=conversation_id,
    ), IDENTITY)
    replace_result = await bridge.handle(request(
        question="江苏省的呢？",
        message_id="structured-filter-replace",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert first.status == add.status == replace_result.status == "COMPLETED"
    assert executions[1].question == "查询2025年广东省的销售额。"
    assert executions[2].question == "查询2025年江苏省的销售额。"
    assert executions[1].history == executions[2].history == []
    # The two short edits are deterministic and do not add model calls.
    assert len(transport.calls) == 2

    snapshot = await bridge.store.load(request(
        question="江苏省的呢？",
        message_id="structured-filter-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    assert task.active_version == 3
    frame = task.versions[-1].context_question
    assert frame.execution_question == "查询2025年江苏省的销售额。"
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("江苏省", "REGION"),
    ]
    assert frame.last_edit.operation == "REPLACE"
    assert frame.last_edit.slot == "filter_expression"


@pytest.mark.asyncio
async def test_structured_task_explicit_filter_replacement_uses_same_family(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def resolve_value(
        _chat, _identity, surface, expected_family, _preferred_attribute_code=None
    ):
        if surface != "江苏省" or expected_family != "REGION":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="省份名称",
            attribute_code="province_name",
            score=1.0,
            business_domain_id=205,
        )

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:explicit-filter-replace",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        v1_context_value_resolver=resolve_value,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "explicit-filter-replace"
    first = request(
        question=first_step[0],
        message_id="explicit-filter-first",
        conversation_id=conversation_id,
    )
    await bridge.handle(first, IDENTITY)
    result = await bridge.handle(request(
        question="换成江苏省呢？",
        message_id="explicit-filter-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert [item.question for item in executions] == [
        first_step[0],
        "查询2025年江苏省的销售额。",
    ]
    assert len(transport.calls) == 2
    snapshot = await bridge.store.load(first, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert frame.last_edit.operation == "REPLACE"
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("江苏省", "REGION"),
    ]


@pytest.mark.asyncio
async def test_explicit_filter_replacement_does_not_add_a_different_family():
    frame = ContextQuestionState(
        original_question="查询上海地区销售额。",
        execution_question="查询上海地区销售额。",
        metrics=["销售额"],
        filters=[ContextQuestionFilter(
            surface="上海",
            canonical_value="上海",
            canonical_name="省份名称",
            attribute_code="province_name",
            semantic_family="REGION",
            evidence_source="V1_SUCCESSFUL_QUERY_EVIDENCE",
        )],
        source_message_id="explicit-cross-family-first",
    )

    async def product_only_resolver(
        surface, expected_family, _preferred_attribute_code=None
    ):
        if surface != "测试产品" or expected_family != "COMMERCIAL_PRODUCT":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="商品名称",
            attribute_code="product_name",
            score=1.0,
            business_domain_id=205,
        )

    assert await _replace_filter(
        frame, "换成测试产品呢？", product_only_resolver
    ) is None


@pytest.mark.asyncio
async def test_named_entity_return_replaces_entity_and_metric_from_current_text(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def resolve_value(
        _chat, _identity, surface, expected_family, _preferred_attribute_code=None
    ):
        if surface not in {"江苏省", "广东省"} or expected_family != "REGION":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="省份名称",
            attribute_code="province_name",
            score=1.0,
            business_domain_id=205,
        )

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:named-return",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        v1_context_value_resolver=resolve_value,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "named-return"
    questions = (
        first_step[0],
        "江苏省的呢？",
        "广东省的呢？",
        "再回到江苏省，它的订单笔数是多少？",
    )
    for index, question in enumerate(questions):
        result = await bridge.handle(request(
            question=question,
            message_id=f"named-return-{index}",
            conversation_id=conversation_id,
        ), IDENTITY)
        assert result.status == "COMPLETED"

    assert executions[-1].question == "查询2025年江苏省的订单笔数。"
    assert executions[-1].history == []
    assert len(transport.calls) == 2
    snapshot = await bridge.store.load(request(
        question="inspect",
        message_id="named-return-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert frame.metrics == ["订单笔数"]
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("江苏省", "REGION"),
    ]
    assert frame.last_edit.operation == "REPLACE"


@pytest.mark.asyncio
async def test_structured_filter_shortcut_requires_one_semantic_family(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def ambiguous_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        if expected_family not in {"REGION", "COMMERCIAL_PRODUCT"}:
            return None
        name, code = (
            ("省份名称", "province_name")
            if expected_family == "REGION"
            else ("商品名称", "product_name")
        )
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name=name,
            attribute_code=code,
            score=1.0,
            business_domain_id=205,
        )

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:ambiguous-filter-edit",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        v1_context_value_resolver=ambiguous_value,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "ambiguous-filter-edit"
    await bridge.handle(request(
        question=first_step[0],
        message_id="ambiguous-filter-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    # A value resolving to two semantic families must not be guessed by the
    # deterministic path. The existing V2 recognizer receives the turn.
    result = await bridge.handle(request(
        question="同名值呢？",
        message_id="ambiguous-filter-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    assert result.status == "SAFE_FALLBACK"
    assert len(executions) == 1
    assert len(transport.calls) > 2


@pytest.mark.asyncio
async def test_recent_pair_total_uses_two_values_from_the_same_task(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def resolve_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        if expected_family != "REGION" or surface not in {"湖南省", "湖北省"}:
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="省份名称",
            attribute_code="province_name",
            score=1.0,
            business_domain_id=205,
        )

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:recent-pair-total",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        v1_context_value_resolver=resolve_value,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "recent-pair-total"
    for message_id, question in (
        ("pair-first", first_step[0]),
        ("pair-second", "只看湖南省的。"),
        ("pair-third", "湖北省的呢？"),
        ("pair-fourth", "这两个省加起来是多少？"),
    ):
        result = await bridge.handle(request(
            question=question,
            message_id=message_id,
            conversation_id=conversation_id,
        ), IDENTITY)
        assert result.status == "COMPLETED"

    assert executions[-1].question == (
        "查询2025年湖南省和湖北省的销售额合计。"
    )
    assert executions[-1].history == []
    assert len(transport.calls) == 2
    snapshot = await bridge.store.load(request(
        question="inspect",
        message_id="pair-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert task.active_version == 4
    assert frame.dimensions == []
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("湖南省", "REGION"),
        ("湖北省", "REGION"),
    ]


@pytest.mark.asyncio
async def test_recent_pair_total_does_not_invent_a_missing_second_value(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:missing-pair-total",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "missing-pair-total"
    await bridge.handle(request(
        question=first_step[0],
        message_id="missing-pair-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    result = await bridge.handle(request(
        question="这两个省加起来是多少？",
        message_id="missing-pair-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert len(executions) == 1
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_recent_two_task_products_are_compared_from_same_conversation(
    context_catalog,
):
    redis = DeploymentRedis()
    executions = []
    first_product = "紫杉醇释放冠脉球囊导管"
    second_product = "一次性使用灭菌橡胶外科手套"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    async def read_context(chat, _identity):
        product = (
            first_product if first_product in chat.question else second_product
        )
        return v1_context(chat, filter_value=product)

    bridge = handler(
        context_catalog,
        redis,
        v1,
        v1_context_reader=read_context,
    )
    install_fallback_resolution(bridge, context_catalog)
    conversation_id = "recent-two-task-product-comparison"
    await bridge.handle(request(
        question=f"查询{first_product}的含税销售总额。",
        message_id="product-comparison-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    await bridge.handle(request(
        question=f"切换话题：查询{second_product}的含税销售总额。",
        message_id="product-comparison-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="这两个产品谁的销售额更高？",
        message_id="product-comparison-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == (
        f"比较{first_product}和{second_product}的销售额，判断哪个更高。"
    )
    assert executions[-1].history == []
    snapshot = await bridge.store.load(request(
        question="inspect",
        message_id="product-comparison-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    assert len(state.tasks) == 2
    active = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = active.versions[-1].context_question
    assert active.active_version == 2
    assert frame.metrics == ["销售额"]
    assert [item.surface for item in frame.filters] == [
        first_product,
        second_product,
    ]


@pytest.mark.asyncio
async def test_demo_pair_comparison_retries_one_real_v1_query(context_catalog):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []
    first_product = "紫杉醇释放冠脉球囊导管"
    second_product = "一次性使用灭菌橡胶外科手套"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if chat.question.startswith("比较"):
            failed = response(chat)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "DEPENDENCY_UNAVAILABLE"
            failed._upstream_error_code = "ASL_TIME_ANCHOR_MISSING"
            return failed
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        product = (
            first_product if first_product in chat.question else second_product
        )
        return v1_context(chat, filter_value=product).model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
        })

    bridge = handler(
        context_catalog,
        redis,
        v1,
        v1_context_reader=read_context,
    )
    bridge.demo_mode = True
    install_fallback_resolution(bridge, context_catalog)
    conversation_id = "demo-pair-comparison-retry"
    await bridge.handle(request(
        question=f"查询{first_product}的销售额。",
        message_id="demo-pair-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    await bridge.handle(request(
        question=f"切换话题：查询{second_product}的销售额。",
        message_id="demo-pair-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="这两个产品谁的销售额更高？",
        message_id="demo-pair-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-2].question == (
        f"比较{first_product}和{second_product}的销售额，判断哪个更高。"
    )
    assert executions[-1].question == (
        f"查询{first_product}和{second_product}的销售额。"
    )
    assert executions[-1].history == []


@pytest.mark.asyncio
async def test_recent_two_task_comparison_requires_two_distinct_products(
    context_catalog,
):
    redis = DeploymentRedis()
    executions = []
    product = "紫杉醇释放冠脉球囊导管"

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value=product)

    bridge = handler(
        context_catalog,
        redis,
        v1,
        v1_context_reader=read_context,
    )
    install_fallback_resolution(bridge, context_catalog)
    conversation_id = "missing-two-task-product-comparison"
    await bridge.handle(request(
        question=f"查询{product}的含税销售总额。",
        message_id="missing-product-comparison-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="这两个产品谁的销售额更高？",
        message_id="missing-product-comparison-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_nationwide_clear_removes_region_and_preserves_product_metric(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.METRIC_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            source_dataset_id=execution_responses[-1].dataset_id,
            metrics=[MetricRef(
                input="含税销售总额", canonical_name="含税销售总额"
            )],
            filters=[
                {"field": "省份名称", "operator": "EQ", "value": "湖北省"},
                {"field": "商品名称", "operator": "EQ", "value": "疝修补补片"},
            ],
            semantic_filter_bindings=[
                SemanticFilterBinding(
                    filter_index=0,
                    input_value="湖北省",
                    canonical_value="湖北省",
                    canonical_name="省份名称",
                    attribute_code="province_name",
                    score=1.0,
                ),
                SemanticFilterBinding(
                    filter_index=1,
                    input_value="疝修补补片",
                    canonical_value="疝修补补片",
                    canonical_name="商品名称",
                    attribute_code="product_name",
                    score=1.0,
                ),
            ],
        )

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "nationwide-clear"
    await bridge.handle(request(
        question="查询湖北省疝修补补片的含税销售总额。",
        message_id="nationwide-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    cleared = await bridge.handle(request(
        question="那全国整体呢？",
        message_id="nationwide-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert cleared.status == "COMPLETED"
    assert executions[-1].question == "查询疝修补补片的含税销售总额。"
    assert executions[-1].history == []
    snapshot = await bridge.store.load(request(
        question="inspect",
        message_id="nationwide-inspect",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("疝修补补片", "COMMERCIAL_PRODUCT"),
    ]
    assert frame.metrics == ["含税销售总额"]
    assert frame.last_edit.operation == "CLEAR"

    repeated = await bridge.handle(request(
        question="不限地区。",
        message_id="nationwide-third",
        conversation_id=conversation_id,
    ), IDENTITY)
    assert repeated.status == "NEEDS_CLARIFICATION"
    assert len(executions) == 2


@pytest.mark.asyncio
async def test_successful_v1_fallback_context_task_supports_time_replace(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return query_response(chat)

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="空心纤维血液透析器")

    bridge = handler(
        provider, redis, v1, v1_context_reader=read_context
    )
    install_fallback_resolution(bridge, provider)
    first = request(
        question="查询空心纤维血液透析器产品合作的经销商名单。",
        message_id="context-time-first",
        conversation_id="context-time-conversation",
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    followup = request(
        question="换今年",
        message_id="context-time-followup",
        conversation_id=first.conversation_id,
    )
    await bridge.handle(followup, IDENTITY)

    assert executions[-1].question == "查询2026年空心纤维血液透析器产品合作的经销商名单。"
    snapshot = await bridge.store.load(followup, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = task.versions[-1].context_question
    assert frame.time.start.year == 2026
    assert frame.last_edit.slot == "time_spec"


@pytest.mark.asyncio
async def test_full_contextual_question_resolves_unique_region_reference(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.METRIC_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            resolved_business_domain_ids=[205],
            source_dataset_id=execution_responses[-1].dataset_id,
            metrics=[],
            filters=[
                {"field": "省份名称", "operator": "EQ", "value": "山西省"}
            ],
            semantic_filter_bindings=[SemanticFilterBinding(
                filter_index=0,
                input_value="山西省",
                canonical_value="山西省",
                canonical_name="省份名称",
                attribute_code="province_name",
                score=1.0,
                business_domain_id=205,
            )],
        )

    bridge = handler(
        provider, redis, v1, v1_context_reader=read_context
    )
    install_fallback_resolution(bridge, provider)
    conversation_id = "full-region-reference"
    first = request(
        question="山西省的经销商有哪些？",
        message_id="region-reference-first",
        conversation_id=conversation_id,
        business_domain_ids=[],
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    followup = request(
        question="按月统计该省份的含税销售总额。",
        message_id="region-reference-followup",
        conversation_id=conversation_id,
        business_domain_ids=[],
    )
    result = await bridge.handle(followup, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "按月统计山西省的含税销售总额。"
    assert executions[-1].history == []
    snapshot = await bridge.store.load(followup, IDENTITY)
    assert snapshot.message(followup.message_id)["bridge_route"] == (
        "V2_CONTEXT_REFERENCE_COMPLETED"
    )
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.state_version == 2
    assert len(state.tasks) == 1
    task = next(iter(state.tasks.values()))
    assert task.active_version == 2
    active = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = active.versions[-1].context_question
    assert frame.original_question == followup.question
    assert frame.execution_question == "按月统计山西省的含税销售总额。"
    assert [(item.surface, item.semantic_family) for item in frame.filters] == [
        ("山西省", "REGION")
    ]


@pytest.mark.asyncio
async def test_full_contextual_reference_requires_one_prior_slot_per_family(provider):
    redis = DeploymentRedis()
    calls = 0
    execution_responses = []

    async def v1(chat, _identity):
        nonlocal calls
        calls += 1
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            source_dataset_id=execution_responses[-1].dataset_id,
            filters=[
                {"field": "省份名称", "operator": "EQ", "value": "山西省"},
                {"field": "省份名称", "operator": "EQ", "value": "陕西省"},
            ],
            semantic_filter_bindings=[
                SemanticFilterBinding(
                    filter_index=index,
                    input_value=value,
                    canonical_value=value,
                    canonical_name="省份名称",
                    attribute_code="province_name",
                    score=1.0,
                    business_domain_id=205,
                )
                for index, value in enumerate(("山西省", "陕西省"))
            ],
        )

    bridge = handler(
        provider, redis, v1, v1_context_reader=read_context
    )
    install_fallback_resolution(bridge, provider)
    conversation_id = "ambiguous-region-reference"
    first = request(
        question="山西省和陕西省的经销商有哪些？",
        message_id="ambiguous-region-first",
        conversation_id=conversation_id,
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="按月统计该省份的含税销售总额。",
        message_id="ambiguous-region-followup",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert result.answer == "上一任务中的地区条件无法唯一确定，请补充完整的地区。"
    assert calls == 1


@pytest.mark.asyncio
async def test_explicit_current_region_is_not_replaced_by_prior_reference(provider):
    chat = request(question="按月统计北京市的含税销售总额。")
    assert resolve_context_references(
        chat=chat,
        identity=IDENTITY,
        state_artifact=None,
        catalog=provider[0],
        resolved_business_domain_ids=[205],
        now=NOW,
    ) is None


@pytest.mark.asyncio
async def test_failed_v1_fallback_keeps_barrier_without_context_task(provider):
    redis = DeploymentRedis()

    async def v1(chat, _identity):
        result = response(chat)
        result.status = "SAFE_FALLBACK"
        result.error_code = "DEPENDENCY_UNAVAILABLE"
        return result

    bridge = handler(provider, redis, v1)
    install_fallback_resolution(bridge, provider)
    chat = request(
        question="查询复杂关系",
        message_id="failed-context-question",
        conversation_id="failed-context-conversation",
    )

    await bridge.handle(chat, IDENTITY)

    snapshot = await bridge.store.load(chat, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.active_topic_id is None
    assert state.tasks == {}


def test_contextual_ellipsis_requires_short_dependent_form():
    assert is_contextual_ellipsis("费森尤斯呢") is True
    assert is_contextual_ellipsis("那费森尤斯呢？") is True
    assert is_contextual_ellipsis("查询北京医院数量呢") is False
    assert is_contextual_ellipsis("这个呢") is False


def test_prior_region_dimension_selects_the_matching_value_attribute_level():
    assert _preferred_context_attribute_code(
        filters=[], dimensions=["省份"], family="REGION"
    ) == "province_name"
    assert _preferred_context_attribute_code(
        filters=[], dimensions=["城市"], family="REGION"
    ) == "city_name"
    assert _preferred_context_attribute_code(
        filters=[], dimensions=["省份", "城市"], family="REGION"
    ) is None


@pytest.mark.asyncio
async def test_new_successful_fallback_context_does_not_reactivate_older_task(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.question)
        return query_response(chat)

    async def read_context(chat, _identity):
        value = "旧产品" if "旧产品" in chat.question else "空心纤维血液透析器"
        return v1_context(chat, filter_value=value)

    async def resolve_value(_chat, _identity, surface, expected_family, _preferred_attribute_code=None):
        if surface != "费森尤斯" or expected_family != "COMMERCIAL_PRODUCT":
            return None
        return SemanticFilterBinding(
            filter_index=0,
            input_value=surface,
            canonical_value=surface,
            canonical_name="母厂牌",
            attribute_code="parent_brand",
            score=1.0,
        )

    bridge = handler(
        provider,
        redis,
        v1,
        v1_context_reader=read_context,
        v1_context_value_resolver=resolve_value,
    )
    install_fallback_resolution(bridge, provider)
    conversation_id = "fallback-new-topic-barrier"
    await bridge.handle(request(
        question="查询旧产品合作的医院名单。",
        message_id="older-fallback",
        conversation_id=conversation_id,
    ), IDENTITY)
    await bridge.handle(request(
        question="查询空心纤维血液透析器产品合作的经销商名单。",
        message_id="current-fallback",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    await bridge.handle(request(
        question="费森尤斯呢",
        message_id="current-followup",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert executions[-1] == "查询费森尤斯产品合作的经销商名单。"
    snapshot = await bridge.store.load(request(
        question="ignored",
        message_id="after",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    assert len(state.tasks) == 2
    active = state.tasks[state.topics[state.active_topic_id].active_task_id]
    assert active.versions[-1].context_question.original_question.startswith(
        "查询空心纤维血液透析器"
    )


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
                "documents": [
                    {
                        "business_domain": {
                            "id": domain_id,
                            "name": f"业务域{domain_id}",
                        }
                    }
                    for domain_id in domains
                ],
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
    assert current.business_domain_labels == ("业务域205",)
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
    assert current.business_domain_labels == ("业务域205",)


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
    # The initial task uses CurrentTurn + SemanticEdits. CLEAR and time
    # replacement are deterministic from the published TaskState.
    assert len(transport.calls) == 2
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
async def test_full_reference_can_use_unique_filter_from_structured_v2_task(
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
            prefix="youo:data-analysis:v2-context-live:structured-reference",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "structured-region-reference"
    await bridge.handle(request(
        question=steps[0][0],
        message_id="structured-reference-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    result = await bridge.handle(request(
        question="按月统计该省份的销售额。",
        message_id="structured-reference-followup",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "按月统计2025年上海的销售额。"
    # Only the first, fully structured turn invokes CurrentTurn and
    # SemanticEdits. The deterministic reference completion invokes neither.
    assert len(transport.calls) == 2
    snapshot = await bridge.store.load(request(
        question="按月统计该省份的销售额。",
        message_id="structured-reference-followup",
        conversation_id=conversation_id,
    ), IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    frame = next(
        item for item in task.versions if item.version == task.active_version
    ).context_question
    assert frame.primary_intent == "METRIC_QUERY"
    assert frame.metrics == ["销售额"]
    assert frame.dimensions == ["月"]
    assert frame.entity is None
    assert frame.fields == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("followup", "expected"),
    [
        ("按月看销售额。", "按月统计2025年上海的销售额。"),
        ("换成按月看。", "按月统计2025年上海的销售额。"),
    ],
)
async def test_grain_only_followup_uses_prior_task_without_inventing_time_range(
    context_catalog, followup, expected,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:grain-followup",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "grain-followup-" + contract_digest(followup)[:8]
    await bridge.handle(request(
        question=first_step[0],
        message_id="grain-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    result = await bridge.handle(request(
        question=followup,
        message_id="grain-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == expected
    assert executions[-1].history == []
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_dimension_ranking_followup_inherits_one_metric_and_filters(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:dimension-ranking",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "dimension-ranking"
    await bridge.handle(request(
        question=first_step[0],
        message_id="ranking-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    result = await bridge.handle(request(
        question="哪个城市最高？",
        message_id="ranking-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "查询2025年上海销售额最高的城市。"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_dimension_ranking_followup_wins_over_broad_replacement_ambiguity(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:dimension-ranking-priority",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "dimension-ranking-priority"
    await bridge.handle(request(
        question=first_step[0],
        message_id="ranking-priority-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    result = await bridge.handle(request(
        question="那最低的城市呢？",
        message_id="ranking-priority-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "查询2025年上海销售额最低的城市。"
    assert executions[-1].history == []
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_metric_only_followup_replaces_metric_and_retains_task_scope(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(
        context_catalog, [first_step, first_step]
    )
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:metric-only-followup",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "metric-only-followup"
    await bridge.handle(request(
        question=first_step[0],
        message_id="metric-only-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    followup = request(
        question="订单笔数是多少？",
        message_id="metric-only-second",
        conversation_id=conversation_id,
    )
    result = await bridge.handle(followup, IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "查询2025年上海订单笔数。"
    assert executions[-1].history == []
    assert len(transport.calls) == 2
    snapshot = await bridge.store.load(followup, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    current = next(
        item for item in task.versions if item.version == task.active_version
    )
    assert current.context_question.metrics == ["订单笔数"]
    assert current.context_question.filters[0].surface == "上海"
    assert current.context_question.time.surface == "2025年"
    assert snapshot.plans == ()

    independent = await bridge.handle(request(
        question=first_step[0],
        message_id="metric-only-independent-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert independent.status == "COMPLETED"
    assert executions[-1].question == first_step[0]
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_ranked_group_result_set_drilldown_preserves_immediate_antecedent(
    provider,
):
    publish_province_dimension(
        provider, publication_id="ranked-group-drilldown-release"
    )
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="测试产品").model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "primary_intent": PrimaryIntent.METRIC_QUERY,
            "entity": None,
            "fields": [],
            "metrics": [MetricRef(input="销售额", canonical_name="销售额")],
            "dimensions": ["省份"],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "ranked-group-result-set-drilldown"
    await bridge.handle(request(
        question="统计测试产品在各省份的销售额。",
        message_id="drilldown-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    second_request = request(
        question="排第一的省份里，有哪些医院在采购？",
        message_id="drilldown-second",
        conversation_id=conversation_id,
    )
    second = await bridge.handle(second_request, IDENTITY)
    after_result_set = await bridge.store.load(second_request, IDENTITY)
    premature_pronoun = resolve_context_references(
        chat=request(
            question="它一共买了多少钱的货？",
            message_id="drilldown-premature-pronoun",
            conversation_id=conversation_id,
        ),
        identity=IDENTITY,
        state_artifact=after_result_set.state,
        catalog=provider[0],
        resolved_business_domain_ids=[205],
        now=NOW,
    )
    assert premature_pronoun is not None
    assert premature_pronoun.completed_question is None
    assert premature_pronoun.clarification_question is not None
    third = await bridge.handle(request(
        question="这些医院里采购金额最高的是哪家？",
        message_id="drilldown-third",
        conversation_id=conversation_id,
    ), IDENTITY)
    fourth_request = request(
        question="它一共买了多少钱的货？",
        message_id="drilldown-fourth",
        conversation_id=conversation_id,
    )
    fourth = await bridge.handle(fourth_request, IDENTITY)

    assert second.status == third.status == fourth.status == "COMPLETED"
    assert [item.question for item in executions] == [
        "统计测试产品在各省份的销售额。",
        "查询测试产品的销售额排名第一的省份中有哪些医院在采购。",
        "查询测试产品的销售额排名第一的省份中采购金额最高的医院。",
        "查询测试产品的销售额排名第一的省份中采购金额最高的医院一共买了多少钱的货。",
    ]
    assert all(item.history == [] for item in executions[1:])
    snapshot = await bridge.store.load(fourth_request, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    current = next(
        item for item in task.versions if item.version == task.active_version
    )
    assert task.active_version == 4
    assert current.context_question.entity == "医院"
    assert current.context_question.dimensions == ["医院"]
    assert current.context_question.filters[0].surface == "测试产品"
    assert current.context_question.execution_question == executions[-1].question


@pytest.mark.asyncio
async def test_ranked_group_drilldown_does_not_reuse_unrelated_dimension(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="测试产品").model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "primary_intent": PrimaryIntent.METRIC_QUERY,
            "entity": None,
            "fields": [],
            "metrics": [MetricRef(input="销售额", canonical_name="销售额")],
            "dimensions": [],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "ranked-group-no-prior-dimension"
    await bridge.handle(request(
        question="查询测试产品的销售额。",
        message_id="no-dimension-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="排第一的省份里，有哪些医院在采购？",
        message_id="no-dimension-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    unrelated_result_set = await bridge.handle(request(
        question="这些医院里采购金额最高的是哪家？",
        message_id="no-dimension-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert unrelated_result_set.status == "NEEDS_CLARIFICATION"
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_generic_object_reference_keeps_current_surface_semantics(provider):
    publish_province_dimension(
        provider, publication_id="province-dimension-release"
    )

    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(
            chat, filter_value="一次性脑电传感器"
        ).model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "metrics": [MetricRef(
                input="含税销售总额", canonical_name="含税销售总额"
            )],
            "dimensions": ["省份"],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "generic-object-reference"
    await bridge.handle(request(
        question="查询一次性脑电传感器的含税销售总额。",
        message_id="generic-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="它在哪个省卖得最好？",
        message_id="generic-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == (
        "查询一次性脑电传感器的含税销售总额最高的省份。"
    )
    assert executions[-1].history == []


@pytest.mark.asyncio
async def test_same_turn_explicit_object_bypasses_history_reference_shortcut(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(
            chat, filter_value="下腔静脉滤器"
        ).model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "metrics": [MetricRef(
                input="含税销售总额", canonical_name="含税销售总额"
            )],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "same-turn-explicit-object"
    await bridge.handle(request(
        question="查询下腔静脉滤器的含税销售总额。",
        message_id="same-turn-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    calls = install_resolution(
        bridge,
        provider,
        completed="查询心排量及压力监测传感器的订单笔数。",
    )

    result = await bridge.handle(request(
        question="再回到心排量及压力监测传感器，它的订单笔数是多少？",
        message_id="same-turn-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert len(calls) == 1
    assert executions[-1].question == "查询心排量及压力监测传感器的订单笔数。"
    assert "下腔静脉滤器" not in executions[-1].question


@pytest.mark.asyncio
async def test_result_count_followup_preserves_opaque_relation_wording(provider):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(
            chat, filter_value="绝对计数管"
        ).model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "dimensions": ["经销商"],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "result-count-reference"
    await bridge.handle(request(
        question="查询绝对计数管产品的经销商名单。",
        message_id="count-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="一共有多少家经销商？",
        message_id="count-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert executions[-1].question == "查询绝对计数管产品的经销商数量。"
    assert executions[-1].history == []


@pytest.mark.asyncio
async def test_sort_and_ranked_item_followups_preserve_prior_object_context(
    context_catalog,
):
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="测试产品").model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "metrics": [MetricRef(input="销售额", canonical_name="销售额")],
            "dimensions": ["城市"],
        })

    bridge = handler(
        context_catalog, redis, v1, v1_context_reader=read_context
    )
    install_fallback_resolution(bridge, context_catalog)
    conversation_id = "sort-and-rank-followup"
    await bridge.handle(request(
        question="查询测试产品的城市名单。",
        message_id="sort-rank-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    sort_result = await bridge.handle(request(
        question="按销售额从高到低排序。",
        message_id="sort-rank-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    rank_result = await bridge.handle(request(
        question="第一名的销售额是多少？",
        message_id="sort-rank-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert sort_result.status == rank_result.status == "COMPLETED"
    assert executions[-2].question == (
        "查询测试产品的城市名单，按销售额从高到低排序。"
    )
    assert executions[-1].question == (
        "查询测试产品的销售额排名第一的城市及其销售额。"
    )
    assert executions[-2].history == executions[-1].history == []


@pytest.mark.asyncio
async def test_generic_object_reference_requires_one_prior_object_family(provider):
    redis = DeploymentRedis()
    calls = 0
    execution_responses = []

    async def v1(chat, _identity):
        nonlocal calls
        calls += 1
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return CanonicalAnalysisRequest(
            request_id=execution_responses[-1].request_id,
            conversation_id=chat.conversation_id,
            application_id=chat.application_id,
            tenant_id=IDENTITY.tenant_id,
            user_id=IDENTITY.user_id,
            original_question=chat.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=list(chat.business_domain_ids),
            source_dataset_id=execution_responses[-1].dataset_id,
            filters=[
                {"field": "商品名称", "operator": "EQ", "value": "测试产品"},
                {"field": "医院名称", "operator": "EQ", "value": "测试医院"},
            ],
            semantic_filter_bindings=[
                SemanticFilterBinding(
                    filter_index=0,
                    input_value="测试产品",
                    canonical_value="测试产品",
                    canonical_name="商品名称",
                    attribute_code="product_name",
                    score=1.0,
                ),
                SemanticFilterBinding(
                    filter_index=1,
                    input_value="测试医院",
                    canonical_value="测试医院",
                    canonical_name="医院名称",
                    attribute_code="hospital_name",
                    score=1.0,
                ),
            ],
        )

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "ambiguous-object-reference"
    await bridge.handle(request(
        question="查询测试医院采购测试产品的情况。",
        message_id="ambiguous-object-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve

    result = await bridge.handle(request(
        question="它在哪个省卖得最好？",
        message_id="ambiguous-object-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert "对象无法唯一确定" in result.answer
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_direct_completion_keeps_current_semantic_context(context_catalog):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if len(executions) == 1:
            return response(chat)
        failed = response(chat)
        failed.status = "SAFE_FALLBACK"
        failed.error_code = "DEPENDENCY_UNAVAILABLE"
        return failed

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:failed-continuation",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
    )
    conversation_id = "failed-direct-completion"
    first = request(
        question=first_step[0],
        message_id="failed-direct-first",
        conversation_id=conversation_id,
    )
    await bridge.handle(first, IDENTITY)
    before = await bridge.store.load(first, IDENTITY)
    before_state = ConversationState.model_validate(before.state.payload)
    prior_topic = before_state.active_topic_id
    prior_task_id = before_state.topics[prior_topic].active_task_id
    prior_version = before_state.tasks[prior_task_id].active_version

    followup = request(
        question="按月看销售额。",
        message_id="failed-direct-second",
        conversation_id=conversation_id,
    )
    result = await bridge.handle(followup, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    after = await bridge.store.load(followup, IDENTITY)
    after_state = ConversationState.model_validate(after.state.payload)
    assert after_state.active_topic_id == prior_topic
    assert set(after_state.tasks) == set(before_state.tasks)
    current_task = after_state.tasks[prior_task_id]
    assert current_task.active_version == prior_version + 1
    current = next(
        item for item in current_task.versions
        if item.version == current_task.active_version
    )
    assert current.context_question is not None
    assert current.context_question.execution_question == (
        "按月统计2025年上海的销售额。"
    )
    assert current.context_question.metrics == ["销售额"]
    assert current.context_question.dimensions == ["月"]
    assert current.context_question.provenance == "V2_CONTEXT_RESOLUTION"
    assert current.semantics.metrics == []
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_demo_result_availability_retries_one_simpler_real_v1_query(
    context_catalog,
):
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if len(executions) == 2:
            failed = response(chat)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
            return failed
        return query_response(chat, answer="real scalar retry result")

    bridge = V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:result-availability",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=context_catalog[0],
        model=scripted.model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
        demo_mode=True,
    )
    conversation_id = "result-availability"
    await bridge.handle(request(
        question=first_step[0],
        message_id="result-availability-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    result = await bridge.handle(request(
        question="按月看销售额。",
        message_id="result-availability-second",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert result.error_code is None
    assert result.answer == "real scalar retry result"
    assert [item.question for item in executions] == [
        first_step[0],
        "按月统计2025年上海的销售额。",
        "查询2025年上海销售额。",
    ]
    assert executions[-1].history == []
    assert executions[-1].semantic_model_id == executions[-2].semantic_model_id
    assert executions[-1].business_domain_ids == executions[-2].business_domain_ids
    rewrite = next(
        item for item in result.analysis_process if item.stage == "QUESTION_REWRITE"
    )
    assert "按月统计2025年上海的销售额。" in rewrite.summary
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_demo_result_retry_removes_only_universal_entity_scope(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if len(executions) == 1:
            failed = response(chat)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
            failed._upstream_error_code = "ASL_ENTITY_MENTION_UNRESOLVED"
            return failed
        return query_response(chat, answer="real universal-scope result")

    bridge = handler(provider, redis, v1)
    bridge.demo_mode = True
    install_fallback_resolution(bridge, provider)
    chat = request(
        question="统计全部产品的含税销售总额。",
        message_id="universal-entity-scope",
        conversation_id="universal-entity-scope",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert result.error_code is None
    assert result.answer == "real universal-scope result"
    assert [item.question for item in executions] == [
        "统计全部产品的含税销售总额。",
        "统计含税销售总额。",
    ]
    assert all(item.history == [] for item in executions)
    rewrite = next(
        item for item in result.analysis_process if item.stage == "QUESTION_REWRITE"
    )
    assert "统计全部产品的含税销售总额。" in rewrite.summary


@pytest.mark.asyncio
async def test_demo_result_retry_never_removes_concrete_entity_value(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        failed = response(chat)
        failed.status = "SAFE_FALLBACK"
        failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
        failed._upstream_error_code = "ASL_ENTITY_MENTION_UNRESOLVED"
        return failed

    bridge = handler(provider, redis, v1)
    bridge.demo_mode = True
    install_fallback_resolution(bridge, provider)
    chat = request(
        question="统计可吸收外科缝线的含税销售总额。",
        message_id="concrete-entity-scope",
        conversation_id="concrete-entity-scope",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    assert result.error_code == "DEPENDENCY_CONTRACT_REJECTED"
    assert [item.question for item in executions] == [chat.question]


@pytest.mark.asyncio
async def test_demo_result_retry_does_not_change_named_value_boundary(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        failed = response(chat)
        failed.status = "SAFE_FALLBACK"
        failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
        failed._upstream_error_code = "ASL_ENTITY_MENTION_UNRESOLVED"
        return failed

    bridge = handler(provider, redis, v1)
    bridge.demo_mode = True
    install_fallback_resolution(bridge, provider)
    chat = request(
        question="分析上海市费森尤斯产品最近一年的销售趋势。",
        message_id="generic-type-after-name",
        conversation_id="generic-type-after-name",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    assert [item.question for item in executions] == [chat.question]


@pytest.mark.asyncio
async def test_demo_result_retry_keeps_generic_result_target(provider):
    redis = DeploymentRedis()
    executions = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        failed = response(chat)
        failed.status = "SAFE_FALLBACK"
        failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
        failed._upstream_error_code = "ASL_ENTITY_MENTION_UNRESOLVED"
        return failed

    bridge = handler(provider, redis, v1)
    bridge.demo_mode = True
    install_fallback_resolution(bridge, provider)
    chat = request(
        question="查询哪些产品的销售额最高？",
        message_id="generic-result-target",
        conversation_id="generic-result-target",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    assert [item.question for item in executions] == [chat.question]


@pytest.mark.asyncio
async def test_demo_result_retry_uses_prior_governed_metric_for_result_set_alias(
    provider,
):
    publish_province_dimension(
        provider, publication_id="result-set-alias-retry-release"
    )
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if "采购金额最高的医院" in chat.question:
            failed = response(chat)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "DEPENDENCY_CONTRACT_REJECTED"
            execution_responses.append(failed)
            return failed
        result = query_response(chat, answer="real availability result")
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(chat, filter_value="测试产品").model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "primary_intent": PrimaryIntent.METRIC_QUERY,
            "entity": None,
            "fields": [],
            "metrics": [MetricRef(input="销售额", canonical_name="销售额")],
            "dimensions": ["省份"],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    bridge.demo_mode = True
    install_fallback_resolution(bridge, provider)
    conversation_id = "result-set-alias-availability-retry"
    await bridge.handle(request(
        question="统计测试产品在各省份的销售额。",
        message_id="alias-retry-first",
        conversation_id=conversation_id,
    ), IDENTITY)
    del bridge._resolve
    await bridge.handle(request(
        question="排第一的省份里，有哪些医院在采购？",
        message_id="alias-retry-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    result = await bridge.handle(request(
        question="这些医院里采购金额最高的是哪家？",
        message_id="alias-retry-third",
        conversation_id=conversation_id,
    ), IDENTITY)

    assert result.status == "COMPLETED"
    assert result.error_code is None
    assert executions[-2].question == (
        "查询测试产品的销售额排名第一的省份中采购金额最高的医院。"
    )
    assert executions[-1].question == "查询测试产品的销售额。"
    rewrite = next(
        item for item in result.analysis_process if item.stage == "QUESTION_REWRITE"
    )
    assert "采购金额最高的医院" in rewrite.summary


@pytest.mark.asyncio
async def test_later_reference_uses_failed_turns_published_context(provider):
    publish_province_dimension(
        provider, publication_id="failed-turn-province-release"
    )
    redis = DeploymentRedis()
    executions = []
    execution_responses = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        if len(executions) == 2:
            failed = response(chat)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "ASL_DIMENSION_INVALID"
            return failed
        result = query_response(chat)
        execution_responses.append(result)
        return result

    async def read_context(chat, _identity):
        return v1_context(
            chat, filter_value="一次性脑电传感器"
        ).model_copy(update={
            "request_id": execution_responses[-1].request_id,
            "source_dataset_id": execution_responses[-1].dataset_id,
            "metrics": [MetricRef(input="销售额", canonical_name="销售额")],
        })

    bridge = handler(provider, redis, v1, v1_context_reader=read_context)
    install_fallback_resolution(bridge, provider)
    conversation_id = "failed-turn-later-reference"
    first = request(
        question="查询一次性脑电传感器的销售额。",
        message_id="failed-chain-first",
        conversation_id=conversation_id,
    )
    await bridge.handle(first, IDENTITY)
    del bridge._resolve

    second = await bridge.handle(request(
        question="按月看销售额。",
        message_id="failed-chain-second",
        conversation_id=conversation_id,
    ), IDENTITY)
    third_request = request(
        question="它在哪个省卖得最好？",
        message_id="failed-chain-third",
        conversation_id=conversation_id,
    )
    third = await bridge.handle(third_request, IDENTITY)

    assert second.status == "SAFE_FALLBACK"
    assert third.status == "COMPLETED"
    assert executions[-1].question == (
        "查询一次性脑电传感器的销售额最高的省份。"
    )
    snapshot = await bridge.store.load(third_request, IDENTITY)
    state = ConversationState.model_validate(snapshot.state.payload)
    task = state.tasks[state.topics[state.active_topic_id].active_task_id]
    assert task.active_version == 3
    current = next(
        item for item in task.versions if item.version == task.active_version
    )
    assert current.context_question.metrics == ["销售额"]
    assert current.context_question.dimensions == ["省份"]
    assert current.context_question.filters[0].surface == "一次性脑电传感器"


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


@pytest.mark.asyncio
async def test_v1_pending_option_answer_bypasses_v2_and_resumes_original_v1(provider):
    redis = DeploymentRedis()
    probes = []
    pending_calls = []
    normal_calls = []

    async def normal_v1(chat, _identity):
        normal_calls.append(chat.model_copy(deep=True))
        raise AssertionError("V2 completed-question execution must not handle Pending")

    async def probe(chat, identity):
        probes.append((chat.model_copy(deep=True), identity))
        return chat.question == "1"

    async def resume(chat, identity):
        pending_calls.append((chat.model_copy(deep=True), identity))
        return query_response(chat, "clarification completed")

    bridge = handler(
        provider,
        redis,
        normal_v1,
        v1_pending_answer_probe=probe,
        v1_pending_executor=resume,
    )
    chat = request(
        question="1",
        message_id="v1-pending-option",
        conversation_id="v1-pending-option",
        history=[{"role": "assistant", "content": "请选择一个业务含义"}],
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert normal_calls == []
    assert probes[0][0].question == "1"
    assert pending_calls[0][0].question == "1"
    assert pending_calls[0][0].history == []
    assert pending_calls[0][1] == IDENTITY
    snapshot = await bridge.store.load(chat, IDENTITY)
    record = snapshot.message(chat.message_id)
    assert record["bridge_route"] == "V1_PENDING_CLARIFICATION_CONTINUATION"
    assert record["v1_execution_called"] is True


@pytest.mark.asyncio
async def test_non_pending_turn_keeps_normal_v2_resolution(provider):
    redis = DeploymentRedis()
    executions = []
    pending_calls = []

    async def v1(chat, _identity):
        executions.append(chat.model_copy(deep=True))
        return response(chat)

    async def probe(_chat, _identity):
        return False

    async def resume(chat, _identity):
        pending_calls.append(chat)
        raise AssertionError("an unrelated turn must not enter V1 Pending")

    bridge = handler(
        provider,
        redis,
        v1,
        v1_pending_answer_probe=probe,
        v1_pending_executor=resume,
    )
    install_resolution(bridge, provider)
    chat = request(
        question="查询今年销售额",
        message_id="not-a-pending-answer",
        conversation_id="not-a-pending-answer",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert pending_calls == []
    assert executions[0].question == chat.question


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
