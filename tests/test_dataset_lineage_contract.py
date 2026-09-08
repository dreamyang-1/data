"""Offline dataset reuse regressions with immutable persisted synthetic rows."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models import ChatRequest, ConversationControl, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.services.authorized_scope import bind_authorized_scope
from app.services.dataset_followup import scope_for_request
from minio_followup_store import MinioFollowupStore, dataset_source_complete
from test_minio_followup_store import FakeMinio
from test_phase0c_scope_contract import service


IDENTITY = TrustedIdentity(tenant_id='lineage-tenant', user_id='lineage-user')
ROWS = [{'地区': '甲', '销售额': 10}, {'地区': '甲', '销售额': 20},
        {'地区': '乙', '销售额': 999}, {'地区': '乙', '销售额': 40},
        {'地区': '乙', '销售额': 50}, {'地区': '乙', '销售额': 60}]


def request(question, **scope):
    chat = ChatRequest(application_id='lineage-app', conversation_id='lineage-conversation',
        message_id='lineage-message', question=question, semantic_model_id=81, **scope)
    result = RuleBasedIntentClassifier().classify(question, IDENTITY, chat.conversation_id)
    result.application_id = chat.application_id
    result.conversation_control = ConversationControl.FOLLOW_UP
    bind_authorized_scope(result, chat.authorized_semantic_scope)
    return result


def setup(storage='json'):
    agent = service()
    client = FakeMinio()
    agent.dataset_store = MinioFollowupStore(client, bucket='lineage-fixture')
    if storage == 'parquet':
        from minio_followup_store import HybridMinioFollowupStore, ParquetMinioFollowupStore
        agent.dataset_store = HybridMinioFollowupStore(agent.dataset_store,
            ParquetMinioFollowupStore(client, bucket='lineage-fixture', part_rows=2, max_dataset_bytes=1024*1024),
            small_max_rows=1, small_max_bytes=1024, medium_max_rows=100, medium_max_bytes=1024*1024)
    return agent


def save(agent, rows=ROWS, *, truncated=False, **kwargs):
    return agent.dataset_store.save_dataset(scope=scope_for_request(request('查询销售额')),
        columns=list(rows[0]), rows=rows, snapshot_id='synthetic-snapshot',
        data_as_of=datetime.now(timezone.utc), source_type='DATABASE_QUERY', source_ref='fixture',
        semantic_model_id=81, business_domain_ids=[],
        transformation_log=({'type': 'query_provenance', 'source_truncated': truncated},), **kwargs)


async def remember(agent, ref):
    await agent.sessions.put_dataset_reference(ref.to_dict(), recent_limit=20)


def derive(agent, ref, operation):
    return agent.dataset_store.execute_followup(ref, current_scope=ref.scope, operation=operation).reference


@pytest.mark.asyncio
@pytest.mark.parametrize('storage', ['json', 'parquet'])
async def test_global_rank_after_display_slice_uses_complete_original_rows(storage):
    agent = setup(storage)
    base = save(agent)
    await remember(agent, base)
    view = request('只看前2条')
    view.source_dataset_id = base.dataset_id
    preview, preview_id = await agent._try_dataset_followup(view)
    assert preview.dataset.rows == ROWS[:2]
    current = request('全局销售额最高1名')
    current.source_dataset_id = preview_id
    result, _ = await agent._try_dataset_followup(current)
    assert result is not None
    assert result.dataset.rows == [ROWS[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize('storage', ['json', 'parquet'])
async def test_display_expansion_never_removes_a_materialized_filter(storage):
    agent = setup(storage)
    base = save(agent)
    filtered = derive(agent, base, {'type': 'filter', 'field': '地区', 'operator': 'eq', 'value': '甲'})
    for ref in [base, filtered]:
        await remember(agent, ref)
    current = request('只看前5条')
    current.source_dataset_id = filtered.dataset_id
    result, _ = await agent._try_dataset_followup(current)
    assert result is not None
    assert result.dataset.rows == ROWS[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize('storage', ['json', 'parquet'])
async def test_join_does_not_turn_incomplete_input_into_global_ranking_evidence(storage):
    agent = setup(storage)
    left = save(agent, [{'code': 'a', '销售额': 10}, {'code': 'b', '销售额': 20}], truncated=True)
    right = save(agent, [{'code': 'a', '名称': '甲'}, {'code': 'b', '名称': '乙'}])
    joined = agent.dataset_store.join_datasets([left, right], current_scope=left.scope, join_keys=['code']).reference
    await remember(agent, joined)
    current = request('全局销售额最高1名')
    current.source_dataset_id = joined.dataset_id
    assert await agent._try_dataset_followup(current) == (None, None)
    assert current.execution_mode == 'QUERY_DATABASE'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', [
    {'type': 'limit', 'count': 2},
    {'type': 'sort_limit', 'field': '销售额', 'descending': False, 'count': 2},
])
async def test_global_rank_recovers_slice_but_explicit_dataset_remains_fixed(operation):
    from app.services.orchestrator import ExplicitDatasetUnavailableError
    agent = setup()
    base = save(agent)
    view = derive(agent, base, operation)
    for ref in [base, view]:
        await remember(agent, ref)
    current = request('全局销售额最高1名')
    current.source_dataset_id = view.dataset_id
    current.assumptions.append('EXPLICIT_SOURCE_DATASET_SELECTION')
    with pytest.raises(ExplicitDatasetUnavailableError, match='不完整'):
        await agent._try_dataset_followup(current)
    automatic = request('全局销售额最高1名')
    automatic.source_dataset_id = view.dataset_id
    result, _ = await agent._try_dataset_followup(automatic)
    assert result.dataset.rows == [ROWS[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize('question,expected', [('只看前5条', ROWS[:5]), ('只看前10条', ROWS)])
async def test_chained_display_limits_recover_available_original_rows(question, expected):
    agent = setup()
    base = save(agent)
    first = derive(agent, base, {'type': 'limit', 'count': 3})
    second = derive(agent, first, {'type': 'limit', 'count': 1})
    for ref in [base, first, second]:
        await remember(agent, ref)
    current = request(question)
    current.source_dataset_id = second.dataset_id
    result, _ = await agent._try_dataset_followup(current)
    assert result.dataset.rows == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('question,expected', [('只看前5条', ROWS[:2]), ('全局销售额最高1名', [ROWS[1]])])
async def test_limit_can_restore_filtered_parent_but_never_unfiltered_grandparent(question, expected):
    agent = setup()
    base = save(agent)
    filtered = derive(agent, base, {'type': 'filter', 'field': '地区', 'operator': 'eq', 'value': '甲'})
    limited = derive(agent, filtered, {'type': 'limit', 'count': 1})
    for ref in [base, filtered, limited]:
        await remember(agent, ref)
    current = request(question)
    current.source_dataset_id = limited.dataset_id
    result, _ = await agent._try_dataset_followup(current)
    assert result.dataset.rows == expected
    assert result.asl['source_dataset_id'] == filtered.dataset_id


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', [
    {'type': 'filter', 'field': '地区', 'operator': 'eq', 'value': '甲'},
    {'type': 'select', 'columns': ['销售额']},
    {'type': 'sort', 'field': '销售额', 'descending': True},
    {'type': 'pipeline', 'operations': [{'type': 'filter', 'field': '地区', 'operator': 'eq', 'value': '甲'}, {'type': 'limit', 'count': 1}]},
])
async def test_intervening_transformations_cannot_erase_prior_slice_incompleteness(operation):
    agent = setup()
    base = save(agent)
    limited = derive(agent, base, {'type': 'limit', 'count': 2})
    transformed = derive(agent, limited, operation)
    for ref in [base, limited, transformed]:
        await remember(agent, ref)
    current = request('全局销售额最高1名')
    current.source_dataset_id = transformed.dataset_id
    assert await agent._try_dataset_followup(current) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['expired', 'missing', 'foreign_model', 'foreign_scope'])
async def test_unusable_parent_never_supplies_global_evidence(kind):
    from app.domain.semantic_scope import AuthorizedSemanticScope
    agent = setup()
    base = save(agent)
    view = derive(agent, base, {'type': 'limit', 'count': 2})
    candidate = base
    if kind == 'expired':
        candidate = replace(base, expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    elif kind == 'missing':
        agent.dataset_store.client.objects.pop((base.bucket, base.object_name))
    elif kind == 'foreign_model':
        candidate = replace(base, semantic_model_id=82)
    elif kind == 'foreign_scope':
        candidate = replace(base, scope=replace(base.scope,
            authorized_semantic_scope_fingerprint=AuthorizedSemanticScope(semantic_model_id=82, scope_mode='MODEL_WIDE').fingerprint()))
    for ref in [candidate, view]:
        await remember(agent, ref)
    agent.dataset_store.client.get_calls.clear()
    current = request('全局销售额最高1名')
    current.source_dataset_id = view.dataset_id
    assert await agent._try_dataset_followup(current) == (None, None)
    assert current.execution_mode == 'QUERY_DATABASE'
    if kind.startswith('foreign'):
        assert (base.bucket, base.object_name) not in agent.dataset_store.client.get_calls


@pytest.mark.parametrize('truncated', [False, True])
def test_join_coverage_survives_json_reload_and_further_projection(truncated):
    agent = setup()
    left = save(agent, [{'code': 'a', '销售额': 10}], truncated=truncated)
    right = save(agent, [{'code': 'a', '名称': '甲'}])
    joined = agent.dataset_store.join_datasets([left, right], current_scope=left.scope, join_keys=['code']).reference
    loaded = agent.dataset_store.load_dataset(joined, current_scope=joined.scope)
    assert dataset_source_complete(loaded.reference, len(loaded.rows)) is not truncated
    projection = derive(agent, joined, {'type': 'select', 'columns': ['销售额']})
    assert dataset_source_complete(projection, projection.row_count) is not truncated


@pytest.mark.parametrize('mutation', ['missing_join_proof', 'unknown_transform', 'partial_read', 'missing_lineage'])
def test_unknown_or_partial_coverage_fails_closed(mutation):
    agent = setup()
    base = save(agent)
    rows = base.row_count
    if mutation == 'missing_join_proof':
        base = replace(base, source_type='JOINED_DATASET', transformation_log=({'type': 'INNER_JOIN'},))
    elif mutation == 'unknown_transform':
        base = replace(base, transformation_log=({'type': 'future_transform'},))
    elif mutation == 'partial_read':
        rows -= 1
    else:
        base = replace(base, parent_dataset_ids=('unknown-parent',), transformation_log=())
    assert not dataset_source_complete(base, rows)


@pytest.mark.asyncio
async def test_normal_three_turn_workflow_reuses_complete_result_without_querying_again():
    from app.domain.models import DataQueryResult, Dataset
    from test_conversation_result_followup import _list_agent, IDENTITY as WORKFLOW_IDENTITY
    agent, retrieval, _, store = _list_agent()
    async def query(canonical, identity, **kwargs):
        retrieval.requests.append(canonical.model_copy(deep=True))
        return DataQueryResult(asl={'version': '2.0', 'intent': 'query', 'ambiguity': []},
            sql='SELECT region, amount FROM synthetic_sales', data_source_id='fixture',
            dataset=Dataset(columns=list(ROWS[0]), rows=ROWS, row_count=len(ROWS),
                snapshot_id='workflow-fixture', data_as_of=datetime.now(timezone.utc)))
    retrieval.query = query
    responses = []
    for index, question in enumerate(['查询本月销售额', '只看前2条', '刚才结果的全局销售额最高1名'], 1):
        responses.append(await agent.handle(ChatRequest(application_id='app', conversation_id='workflow-lineage',
            message_id=str(index), semantic_model_id=81, question=question), WORKFLOW_IDENTITY))
    assert [response.status for response in responses] == ['COMPLETED'] * 3
    assert len(retrieval.requests) == 1
    assert store.items[responses[-1].dataset_id].rows == (ROWS[2],)


@pytest.mark.parametrize('change', ['columns', 'snapshot_id', 'scope', 'lineage', 'count', 'cycle', 'multiple_parents'])
def test_parent_recovery_requires_exact_presentation_lineage(change):
    from app.services.dataset_followup import presentation_ancestors
    agent = setup()
    base = save(agent)
    limited = derive(agent, base, {'type': 'limit', 'count': 2})
    parent, child = base.to_dict(), limited.to_dict()
    if change == 'columns':
        parent['columns'] = ['unrelated']
    elif change == 'snapshot_id':
        parent['snapshot_id'] = 'other-snapshot'
    elif change == 'scope':
        parent['scope']['user_id'] = 'other-user'
    elif change == 'lineage':
        child['transformation_log'][0] = {'type': 'query_provenance', 'source_truncated': True}
    elif change == 'count':
        child['transformation_log'][-1]['count'] = 3
    elif change == 'cycle':
        child['parent_dataset_ids'] = [child['dataset_id']]
    else:
        child['parent_dataset_ids'].append('another-parent')
    assert presentation_ancestors(child, [parent, child], allow_rank_slice=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('truncated', [False, True])
async def test_cross_branch_join_reports_limited_reliability_for_incomplete_input(truncated):
    from uuid import uuid4
    from app.domain.models import AgentResponse, AtomicTask, PrimaryIntent, ReliabilityReport
    agent = setup()
    left = save(agent, [{'code_id': 'a', '销售额': 10}], truncated=truncated)
    right = save(agent, [{'code_id': 'a', '名称': '甲'}])
    for ref in [left, right]:
        await remember(agent, ref)
    responses = {key: AgentResponse(request_id=uuid4(), conversation_id=ref.scope.conversation_id,
        status='COMPLETED', intent=PrimaryIntent.DETAIL_QUERY, answer='fixture', dataset_id=ref.dataset_id,
        reliability=ReliabilityReport(level='HIGH', score=1, gates={})) for key, ref in [('left', left), ('right', right)]}
    current = ChatRequest(application_id='lineage-app', conversation_id='lineage-conversation',
        message_id='join', question='按code_id关联结果', semantic_model_id=81)
    result = await agent._execute_cross_branch_join(
        AtomicTask(task_id='join', question=current.question, depends_on=['left', 'right']), current, IDENTITY,
        responses, {'left': left.scope.conversation_id, 'right': right.scope.conversation_id})
    assert result.status == 'COMPLETED'
    assert result.reliability.level == ('LIMITED' if truncated else 'HIGH')
    assert any('来源数据不完整' in warning for warning in result.reliability.warnings) is truncated
