"""Synthetic, offline acceptance of the trusted conversation compatibility contract."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.models import TrustedIdentity
from app.stores.long_memory import MemoryScope
from test_phase0c_scope_contract import app, chat, service


AUTH = {'Authorization': 'Bearer phase0c-fixture-token'}
ROUTES = ['/agent_chat', '/agent_chat/stream', '/agent_chat/refresh', '/agent_chat/refresh/stream']


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('production', [False, True])
def test_trusted_conversation_does_not_require_personal_identity(route, production):
    headers = {**AUTH, 'X-Application-Id': 'scope-app'}
    with TestClient(app(), headers=headers) as client:
        # Exercise the production trust guard using already-created offline stores.
        client.app.state.container.settings.env = 'production' if production else 'test'
        response = client.post(route, json=chat(question='你好').model_dump(mode='json'))
    assert response.status_code == 200
    if 'stream' in route:
        # The existing protocol is data-only SSE, never named SSE events.
        assert 'event:' not in response.text
        events = [json.loads(block.removeprefix('data: ')) for block in response.text.strip().split('\n\n')]
        assert events[-1]['type'] == 'complete'
        assert events[-1]['status'] == 'COMPLETED'
        assert not any(event['type'] == 'error' for event in events)
    else:
        assert response.json()['status'] == 'COMPLETED'


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer invalid'}])
def test_conversation_id_never_substitutes_for_service_authentication(headers):
    with TestClient(app(), headers=headers) as client:
        response = client.post('/agent_chat', json=chat(question='你好').model_dump(mode='json'))
        assert not client.app.state.container.sessions._responses
    assert response.status_code == 401
    assert response.json()['detail']['code'] == 'UPSTREAM_SCOPE_TRUST_INVALID'


def test_retries_and_stream_reuse_only_the_same_conversation_response():
    application = app()
    with TestClient(application, headers=AUTH) as client:
        body = chat(question='你好').model_dump(mode='json')
        first = client.post('/agent_chat', json=body)
        assert first.status_code == 200
        retry = client.post('/agent_chat', json=body)
        assert retry.json()['request_id'] == first.json()['request_id']
        stream = client.post('/agent_chat/stream', json=body)
        assert first.json()['request_id'] in stream.text
        second = client.post('/agent_chat', json={**body, 'conversation_id': 'other-conversation'})
        assert second.json()['request_id'] != first.json()['request_id']
        keys = list(application.state.container.sessions._responses)
        # Tenant-scoped validated examples must also be separated without a principal.
        assert len({key[0] for key in keys}) == 2
        assert len({key[1] for key in keys}) == 2


@pytest.mark.parametrize('headers', [
    {'X-Tenant-Id': 'tenant-a'}, {'X-User-Id': 'user-a'},
    {'X-Tenant-Id': 'default-tenant', 'X-User-Id': 'default-user'},
])
def test_partial_or_placeholder_metadata_uses_the_conversation_namespace(headers):
    with TestClient(app(), headers=AUTH) as client:
        body = chat(question='你好').model_dump(mode='json')
        first = client.post('/agent_chat', json=body)
        assert first.status_code == 200
        second = client.post('/agent_chat', json=body, headers=headers)
        assert second.status_code == 200
        assert second.json()['request_id'] == first.json()['request_id']


@pytest.mark.parametrize('headers', [{'X-Tenant-Id': ''}, {'X-User-Id': ' '}, {'X-User-Id': 'x' * 129}])
def test_supplied_malformed_metadata_is_not_silently_ignored(headers):
    with TestClient(app(), headers={**AUTH, **headers}) as client:
        response = client.post('/agent_chat', json=chat(question='你好').model_dump(mode='json'))
    assert response.status_code == 401
    assert response.json()['detail']['code'] == 'STATE_NAMESPACE_REQUIRED'


@pytest.mark.parametrize('changes', [
    {'semantic_model_id': 82}, {'business_domain_ids': [205]},
    {'database_id': 2}, {'knowledge_base_names': ['other-kb']},
])
def test_conversation_cache_still_enforces_current_authorized_scope(changes):
    with TestClient(app(), headers=AUTH) as client:
        body = chat(question='你好').model_dump(mode='json')
        first = client.post('/agent_chat', json=body)
        assert first.status_code == 200
        conflict = client.post('/agent_chat/stream', json={**body, **changes})
        assert conflict.status_code == 409
        assert conflict.json()['detail']['code'] == 'MESSAGE_ID_REUSE_CONFLICT'
        fresh = client.post('/agent_chat', json={**body, **changes, 'message_id': 'new-message'})
        assert fresh.status_code == 200
        assert fresh.json()['request_id'] != first.json()['request_id']


@pytest.mark.parametrize('metadata', [{}, {'X-Tenant-Id': 'default-tenant', 'X-User-Id': 'default-user'}])
def test_conversation_without_personal_principal_never_reads_personal_memory(metadata):
    application = app()
    with TestClient(application, headers={**AUTH, **metadata}) as client:
        memories = application.state.container.memories
        memories.list_active = AsyncMock(side_effect=AssertionError('personal memory must not be read'))
        response = client.post('/agent_chat', json=chat(question='查询本月销售额', use_longterm_memory=True).model_dump(mode='json'))
        assert response.status_code == 200
        assert response.json()['status'] == 'COMPLETED'
        memories.list_active.assert_not_called()


@pytest.mark.parametrize('tenant,user', [('default-tenant', 'default-user'), ('tenant', 'default-user'),
    ('conversation:synthetic', 'conversation:synthetic')])
def test_personal_memory_scope_rejects_shared_or_synthetic_principals(tenant, user):
    with pytest.raises(ValidationError, match='STABLE_USER_PRINCIPAL_REQUIRED'):
        MemoryScope(tenant_id=tenant, user_id=user, application_id='scope-app')


@pytest.mark.asyncio
async def test_real_principal_retains_cross_conversation_personal_memory_recall():
    agent = service()
    agent.memories = SimpleNamespace(list_active=AsyncMock(return_value=[]))
    identity = TrustedIdentity(tenant_id='tenant-a', user_id='user-a')
    for conversation in ['first', 'second']:
        await agent.handle(chat(question='查询本月销售额', conversation_id=conversation, use_longterm_memory=True), identity)
    assert agent.memories.list_active.await_count == 2
    assert all(call.args[0] == MemoryScope(tenant_id='tenant-a', user_id='user-a', application_id='scope-app')
               for call in agent.memories.list_active.await_args_list)


def test_spreadsheet_import_and_chat_use_identical_normalized_conversation_namespace():
    application = app()
    reference = SimpleNamespace(dataset_id='synthetic-dataset', source_object='synthetic.csv',
        columns=['amount'], row_count=1, expires_at='2099-01-01T00:00:00+00:00')
    importer = SimpleNamespace(import_object=AsyncMock(return_value=(reference, ['data'])))
    with TestClient(application, headers=AUTH) as client:
        object.__setattr__(application.state.container, 'file_importer', importer)
        response = client.post('/v1/data-analysis/datasets/import-spreadsheet', json={
            'application_id': ' scope-app ', 'conversation_id': ' scope-conversation ',
            'semantic_model_id': 81, 'object_name': 'synthetic.csv'})
        assert response.status_code == 201
        answer = client.post('/agent_chat', json=chat(question='你好').model_dump(mode='json'))
        assert answer.status_code == 200
        scope = importer.import_object.await_args.kwargs['scope']
        key = next(iter(application.state.container.sessions._responses))
        assert (scope.tenant_id, scope.user_id, scope.application_id, scope.conversation_id) == key[:4]
        assert scope.authorized_semantic_scope_fingerprint == chat().authorized_semantic_scope.fingerprint()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['pending', 'last_request', 'task_frame'])
@pytest.mark.parametrize('changes,reuse', [({}, True), ({'conversation_id': 'other'}, False),
    ({'semantic_model_id': 82}, False), ({'business_domain_ids': [205]}, False)])
async def test_conversation_state_restore_checks_namespace_and_current_scope(kind, changes, reuse):
    from app.domain.state_identity import conversation_namespace
    from app.intent import RuleBasedIntentClassifier
    from app.services.authorized_scope import bind_authorized_scope
    agent = service()
    previous = chat()
    tenant, user = conversation_namespace(previous.application_id, previous.conversation_id)
    identity = TrustedIdentity(tenant_id=tenant, user_id=user)
    old = RuleBasedIntentClassifier().classify('上海最近一年销售额', identity, previous.conversation_id)
    old.application_id = previous.application_id
    bind_authorized_scope(old, previous.authorized_semantic_scope)
    if kind == 'pending':
        from app.domain.models import PendingState
        await agent.sessions.put_pending(PendingState(request=old, clarification_rounds=1, state_version=1), expected_version=0)
    else:
        await getattr(agent.sessions, 'put_' + kind)(old)
    observed = []
    delegate = agent.turn_admission_gate.evaluate
    def capture(**kwargs):
        observed.append(kwargs.get('previous'))
        return delegate(**kwargs)
    agent.turn_admission_gate.evaluate = capture
    current = chat(**changes)
    tenant, user = conversation_namespace(current.application_id, current.conversation_id)
    await agent.handle(current, TrustedIdentity(tenant_id=tenant, user_id=user))
    assert observed
    assert any(item is not None for item in observed) is reuse


@pytest.mark.parametrize('same_conversation', [False, True])
def test_dataset_ownership_is_bound_to_the_trusted_conversation(same_conversation):
    from app.domain.state_identity import conversation_namespace
    from minio_followup_store import DatasetScope, DatasetScopeMismatch, MinioFollowupStore
    from test_minio_followup_store import FakeMinio
    client = FakeMinio()
    store = MinioFollowupStore(client, bucket='fixture')
    def scope(conversation):
        tenant, user = conversation_namespace('scope-app', conversation)
        return DatasetScope(tenant_id=tenant, user_id=user, application_id='scope-app',
            conversation_id=conversation, authorized_semantic_scope_fingerprint=chat().authorized_semantic_scope.fingerprint())
    ref = store.save_dataset(scope=scope('first'), columns=['amount'], rows=[{'amount': 1}],
        snapshot_id='synthetic', data_as_of=datetime.now(timezone.utc), source_type='DATABASE_QUERY',
        source_ref='fixture', semantic_model_id=81, business_domain_ids=[])
    if same_conversation:
        assert store.load_dataset(ref, current_scope=scope('first')).rows == ({'amount': 1},)
    else:
        with pytest.raises(DatasetScopeMismatch):
            store.load_dataset(ref, current_scope=scope('other'))
        assert client.get_calls == []


@pytest.mark.asyncio
async def test_redis_conversation_keys_preserve_same_conversation_and_separate_other_conversations():
    import fakeredis.aioredis
    from app.domain.models import PendingState
    from app.domain.state_identity import conversation_namespace
    from app.intent import RuleBasedIntentClassifier
    from app.stores.session import RedisSessionStore
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisSessionStore(redis, ttl_seconds=600, prefix='conversation-test')
    # Installed fakeredis lacks Lua. Exercise real key construction/JSON storage
    # with a write transport double, not a claim to test Redis CAS concurrency.
    async def write_transport(script, numkeys, key, expected_version, value, ttl):
        await redis.set(key, value, ex=ttl)
        return 1
    redis.eval = AsyncMock(side_effect=write_transport)
    def parts(conversation):
        tenant, user = conversation_namespace('scope-app', conversation)
        return tenant, user, 'scope-app', conversation
    key = parts('first')
    old = RuleBasedIntentClassifier().classify('上海销售额', TrustedIdentity(tenant_id=key[0], user_id=key[1]), key[3])
    old.application_id = key[2]
    try:
        await store.put_pending(PendingState(request=old, clarification_rounds=1, state_version=1), expected_version=0)
        await store.put_last_request(old)
        await store.put_task_frame(old)
        await store.put_dag_pending(*key, {'scope': 'synthetic', 'state_version': 1}, expected_version=0)
        for method in ['get_pending', 'get_last_request', 'get_task_frame', 'get_dag_pending']:
            assert await getattr(store, method)(*key) is not None
            assert await getattr(store, method)(*parts('other')) is None
    finally:
        await redis.aclose()
