"""Execution regressions use real persisted synthetic artifacts, never business IO."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    AgentResponse, AtomicTask, CanonicalAnalysisRequest,
    ChatRequest, MetricRef, PlannerExtraction, PrimaryIntent, TimeRange, TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.services.authorized_scope import bind_authorized_scope
from app.services.orchestrator import DataAnalysisOrchestrator, DependencyConstraintError
from app.stores import InMemorySessionStore
from minio_followup_store import DatasetScope, MinioFollowupStore, dataset_source_complete
from test_minio_followup_store import FakeMinio


IDENTITY = TrustedIdentity(tenant_id="execution-test", user_id="execution-test")


def context():
    chat = ChatRequest(semantic_model_id=81, application_id="execution-test",
                       conversation_id="root", message_id="message", question="查询结果")
    request = CanonicalAnalysisRequest(semantic_model_id=81, application_id=chat.application_id,
        conversation_id=chat.conversation_id, tenant_id=IDENTITY.tenant_id, user_id=IDENTITY.user_id,
        original_question="查询医院明细", primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院", fields=["医院名称"], metrics=[MetricRef(input="订单笔数")])
    bind_authorized_scope(request, chat.authorized_semantic_scope)
    chat._dag_root_conversation_id = chat.conversation_id
    return chat, request


def service():
    agent = DataAnalysisOrchestrator(
        settings=Settings(_env_file=None, env="test", adapter_mode="mock", runtime_mode="V1",
                          intent_model_enabled=False, analysis_synthesis_enabled=False),
        classifier=RuleBasedIntentClassifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        dataset_store=MinioFollowupStore(FakeMinio(), bucket="execution-fixtures"),
    )
    return agent


async def persist(agent, chat, columns, rows, *, conversation="root", request=None,
                  truncated=False, quality="PASS", **provenance):
    log = {"type": "query_provenance", "source_truncated": truncated,
           "source_quality_status": quality,
           "source_total_row_count": len(rows), **provenance}
    if request is not None:
        log["pre_asl_query_fingerprint"] = agent._query_fingerprint(request)
        log["pre_asl_fingerprint_components"] = agent._query_fingerprint_components(request)
    reference = agent.dataset_store.save_dataset(
        scope=DatasetScope(IDENTITY.tenant_id, IDENTITY.user_id, chat.application_id, conversation,
                          chat.authorized_semantic_scope.fingerprint()),
        columns=columns, rows=rows, snapshot_id="synthetic-snapshot",
        data_as_of=datetime.now(timezone.utc), source_type="DATABASE_QUERY", source_ref="synthetic",
        semantic_model_id=chat.semantic_model_id, business_domain_ids=chat.business_domain_ids,
        transformation_log=(log,),
    )
    await agent.sessions.put_dataset_reference(reference.to_dict(), recent_limit=20)
    return reference


@pytest.mark.parametrize("changes", [
    {"time_range": TimeRange(start="2025-01-01", end_exclusive="2026-01-01")},
    {"entity": "供应商"}, {"fields": ["医院编码"]}, {"database_id": 2},
    {"comparison_type": "YOY"}, {"resolved_periods": ["2025-09"]},
    {"metrics": [MetricRef(input="订单笔数", version="new-version")]},
    {"source_dataset_id": "explicit-dataset"},
    {"metric_subject_entity": "订单明细"},
])
def test_query_identity_changes_for_result_affecting_slots(changes):
    _, request = context()
    assert DataAnalysisOrchestrator._query_fingerprint(request) != (
        DataAnalysisOrchestrator._query_fingerprint(request.model_copy(update=changes))
    )


@pytest.mark.parametrize("before,after", [
    ({"排序": [{"field": "订单笔数", "order": "asc"}]},
     {"排序": [{"field": "订单笔数", "order": "desc"}]}),
    ({"时间粒度": {"unit": "month"}}, {"时间粒度": {"unit": "year"}}),
    ({"是否去重": "是"}, {"是否去重": "否"}),
    ({"输出要求": "计算环比增长率"}, {"输出要求": "计算同比增长率"}),
])
def test_query_identity_includes_effective_planner_query_shape(before, after):
    _, request = context()
    other = request.model_copy(deep=True)
    request._planner_extraction = PlannerExtraction(structured=before)
    other._planner_extraction = PlannerExtraction(structured=after)
    assert DataAnalysisOrchestrator._query_fingerprint(request) != DataAnalysisOrchestrator._query_fingerprint(other)


def test_query_identity_ignores_wording_and_and_filter_order():
    _, request = context()
    request.filters = [{"field": "城市", "value": "上海"}, {"field": "科室", "value": "消化内科"}]
    other = request.model_copy(deep=True, update={"request_id": uuid4(),
        "original_question": "换一种说法查同一医院明细", "filters": list(reversed(request.filters))})
    assert DataAnalysisOrchestrator._query_fingerprint(request) == DataAnalysisOrchestrator._query_fingerprint(other)


@pytest.mark.asyncio
async def test_equivalent_query_reuses_exact_persisted_rows():
    agent = service(); chat, request = context()
    reference = await persist(agent, chat, ["医院名称"], [{"医院名称": "合成医院"}], request=request)
    result = await agent._reuse_prior_dataset_for_task(request, chat)
    assert result is not None and result.dataset.rows == [{"医院名称": "合成医院"}]
    assert result.asl["source_dataset_id"] == reference.dataset_id
    assert request.execution_mode == "REUSE_PREVIOUS_RESULT"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wording", "refresh_endpoint"])
async def test_explicit_refresh_never_uses_automatic_history_cache(mode):
    agent = service(); chat, request = context()
    await persist(agent, chat, ["医院名称"], [{"医院名称": "旧结果"}], request=request)
    if mode == "wording":
        chat.question = "请重新查询最新数据，不要复用上次结果"
    else:
        chat._regeneration_mode = "REFRESH"
    assert await agent._reuse_prior_dataset_for_task(request, chat) is None
    assert request.execution_mode == "QUERY_DATABASE"


@pytest.mark.asyncio
async def test_source_truncation_cannot_be_lost_by_query_reuse():
    agent = service(); chat, request = context()
    reference = await persist(agent, chat, ["医院名称"], [{"医院名称": "预览医院"}],
                              request=request, truncated=True, source_total_row_count=20)
    assert not dataset_source_complete(reference, 1)
    assert await agent._reuse_prior_dataset_for_task(request, chat) is None


@pytest.mark.asyncio
async def test_recorded_total_cannot_be_bigger_than_persisted_source():
    agent = service(); chat, request = context()
    reference = await persist(agent, chat, ['医院名称'], [{'医院名称':'仅一行'}],
        request=request, source_total_row_count=20)
    assert not dataset_source_complete(reference, 1)
    assert await agent._reuse_prior_dataset_for_task(request, chat) is None


@pytest.mark.asyncio
async def test_complete_source_can_be_filtered_without_losing_coverage_evidence():
    agent = service(); chat, _ = context()
    source = await persist(agent, chat, ['医院名称'],
        [{'医院名称':'甲医院'},{'医院名称':'乙医院'}], source_row_count=2)
    derived = agent.dataset_store.execute_followup(source, current_scope=source.scope,
        operation={'type':'filter','field':'医院名称','operator':'eq','value':'甲医院'})
    loaded = agent.dataset_store.load_dataset(derived.reference, current_scope=source.scope)
    assert loaded.rows == ({'医院名称':'甲医院'},)
    assert dataset_source_complete(derived.reference, len(loaded.rows))


@pytest.mark.asyncio
async def test_source_quality_is_not_upgraded_by_query_reuse():
    agent = service(); chat, request = context()
    await persist(agent, chat, ["医院名称"], [{"医院名称": "合成医院"}], request=request, quality="WARN")
    result = await agent._reuse_prior_dataset_for_task(request, chat)
    assert result is not None and result.dataset.quality_status == "WARN"


@pytest.mark.asyncio
async def test_truncated_source_is_not_a_complete_dependency_constraint():
    agent = service(); chat, _ = context()
    reference = await persist(agent, chat, ["supplier_code"], [{"supplier_code": "S-001"}],
                              truncated=True, source_total_row_count=20)
    response = AgentResponse(request_id=uuid4(), conversation_id="root", status="PARTIAL_SUCCESS",
                             intent=PrimaryIntent.DETAIL_QUERY, answer="预览", dataset_id=reference.dataset_id)
    task = AtomicTask(task_id="task-2", question="根据上述供应商查询商品", depends_on=["task-1"],
        extraction={'依赖绑定':[{'source_task_id':'task-1','source_column':'supplier_code'}]})
    with pytest.raises(DependencyConstraintError, match="INCOMPLETE"):
        await agent._compile_dependency_constraints(task, chat, IDENTITY,
            {"task-1": response}, {"task-1": "root"})


async def ratio_input(*, reorder=False, truncated=False, question=None, missing=False):
    agent = service(); chat, _ = context()
    columns = ["经销商编码", "医院数", "销售额"]
    if reorder:
        columns = ["经销商编码", "销售额", "医院数"]
    rows = [{"经销商编码": "D-001", "医院数": 5, "销售额": 1000},
            {"经销商编码": "D-002", "医院数": 10, "销售额": 2000}]
    if missing:
        columns.remove("医院数")
        rows = [{key: value for key, value in row.items() if key != "医院数"} for row in rows]
    source = await persist(agent, chat, columns, rows, conversation="numerator", truncated=truncated)
    denominator = await persist(agent, chat, ["区域医院总数"], [{"区域医院总数": 100}], conversation="denominator")
    task = AtomicTask(task_id="task-3", question=question or
        "计算各经销商医院覆盖率，医院数除以区域医院总数，不再查询数据库", depends_on=["task-1", "task-2"])
    responses = {name: AgentResponse(request_id=uuid4(), conversation_id=ref.scope.conversation_id,
        status="COMPLETED", intent=PrimaryIntent.METRIC_QUERY, answer="查询已完成", dataset_id=ref.dataset_id)
        for name, ref in [("task-1", source), ("task-2", denominator)]}
    return agent, chat, task, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("reorder", [False, True])
async def test_ratio_uses_declared_columns_not_last_numeric_column(reorder):
    agent, chat, task, responses = await ratio_input(reorder=reorder)
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {"task-1": "numerator", "task-2": "denominator"}, responses)
    assert result.status == "COMPLETED"
    preview = result.evidence[0].payload["preview"]
    assert {row["经销商编码"]: row["覆盖率"] for row in preview} == {"D-001": "5.00%", "D-002": "10.00%"}
    assert "医院数" in result.evidence[0].payload["operation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"truncated": True}, {"missing": True}, {"question": "计算两个结果，不再查询数据库"},
])
async def test_ratio_does_not_guess_or_upgrade_incomplete_inputs(kwargs):
    agent, chat, task, responses = await ratio_input(**kwargs)
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {"task-1": "numerator", "task-2": "denominator"}, responses)
    assert result.status != "COMPLETED" and result.dataset_id is None


@pytest.mark.asyncio
async def test_ratio_loads_response_dataset_not_a_later_same_conversation_artifact():
    agent, chat, task, responses = await ratio_input()
    await persist(agent, chat, ["经销商编码", "医院数", "销售额"],
        [{"经销商编码": "D-001", "医院数": 80, "销售额": 8000},
         {"经销商编码": "D-002", "医院数": 90, "销售额": 9000}], conversation="numerator")
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {"task-1": "numerator", "task-2": "denominator"}, responses)
    assert result.status == "COMPLETED"
    assert {row["经销商编码"]: row["覆盖率"] for row in result.evidence[0].payload["preview"]} == {
        "D-001": "5.00%", "D-002": "10.00%"}


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, True, 'missing', 0, -1])
async def test_invalid_denominator_is_not_a_success(value):
    agent, chat, task, responses = await ratio_input()
    replacement = await persist(agent, chat, ['区域医院总数'], [{'区域医院总数': value}], conversation='denominator')
    responses['task-2'].dataset_id = replacement.dataset_id
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {'task-1':'numerator', 'task-2':'denominator'}, responses)
    assert result.status == 'FAILED' and result.dataset_id is None


@pytest.mark.asyncio
async def test_structured_operand_binding_supports_numeric_scalar_average():
    agent = service(); chat, _ = context()
    amount = await persist(agent, chat, ['金额', '备用金额'], [{'金额':1000, '备用金额':9000}], conversation='amount')
    orders = await persist(agent, chat, ['订单笔数'], [{'订单笔数':4}], conversation='orders')
    task = AtomicTask(task_id='average', question='计算平均每单金额', depends_on=['amount', 'orders'],
        extraction={'计算绑定': {'operation':'divide',
            'numerator': {'source_task_id':'amount', 'column':'金额'},
            'denominator': {'source_task_id':'orders', 'column':'订单笔数'},
            'result_field':'平均每单金额', 'format':'number'}})
    result = await agent._respond_pure_computation(chat, IDENTITY, task, {'amount':'amount','orders':'orders'})
    assert result.status == 'COMPLETED'
    assert result.evidence[0].payload['preview'][0]['平均每单金额'] == 250
    binding = result.evidence[0].payload['input_binding']
    assert binding['numerator']['dataset_id'] == amount.dataset_id
    assert binding['denominator']['dataset_id'] == orders.dataset_id


@pytest.mark.asyncio
async def test_dependency_response_cannot_load_another_scope():
    agent, chat, task, responses = await ratio_input()
    # References remain in the same session. A changed authorization must still
    # be checked by the real dataset store rather than trusting reference.scope.
    other_chat = ChatRequest(semantic_model_id=81, business_domain_id=205,
        application_id=chat.application_id, conversation_id=chat.conversation_id,
        message_id='changed-scope', question=chat.question)
    result = await agent._respond_pure_computation(other_chat, IDENTITY, task,
        {'task-1':'numerator','task-2':'denominator'}, responses)
    assert result.status == 'FAILED' and result.dataset_id is None


@pytest.mark.asyncio
async def test_warn_inputs_do_not_become_high_quality_computation():
    agent, chat, task, responses = await ratio_input()
    source = await persist(agent, chat, ['经销商编码','医院数'],
        [{'经销商编码':'D-001','医院数':5}], conversation='numerator', quality='WARN')
    responses['task-1'].dataset_id = source.dataset_id
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {'task-1':'numerator','task-2':'denominator'}, responses)
    assert result.status == 'COMPLETED' and result.reliability.level != 'HIGH'
    ref = await agent._latest_task_dataset_reference(IDENTITY, chat, chat.conversation_id, dataset_id=result.dataset_id)
    assert ref['transformation_log'][0]['source_quality_status'] == 'WARN'


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['period', 'unit'])
async def test_ratio_rejects_known_incompatible_inputs(mismatch):
    agent, chat, task, responses = await ratio_input()
    shape = {'time_range': {'start':'2025-01-01','end_exclusive':'2026-01-01'},
             'metrics':[{'name':'医院数','unit':'家'}]}
    source = await persist(agent, chat, ['经销商编码','医院数'],
        [{'经销商编码':'D-001','医院数':5}], conversation='numerator', fingerprint_components=shape)
    denominator_shape = {'time_range': shape['time_range'], 'metrics':[{'name':'区域医院总数','unit':'家'}]}
    if mismatch == 'period':
        denominator_shape['time_range'] = None
    else:
        denominator_shape['metrics'][0]['unit'] = '元'
    denominator = await persist(agent, chat, ['区域医院总数'], [{'区域医院总数':100}],
        conversation='denominator', fingerprint_components=denominator_shape)
    responses['task-1'].dataset_id = source.dataset_id
    responses['task-2'].dataset_id = denominator.dataset_id
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
        {'task-1':'numerator','task-2':'denominator'}, responses)
    assert result.status == 'FAILED' and result.dataset_id is None
