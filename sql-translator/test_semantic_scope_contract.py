import io
import json
import fnmatch

import pytest

import api_server_prod as api
from sql_translator_prod import RedisDSLLoader


@pytest.mark.parametrize('domains', [[], [205], [205, 206]])
def test_translator_declares_actual_scope_capability(domains):
    error = api._scope_contract_error({'modelId': '81', 'business_domain_ids': domains})
    if len(domains)<=1:
        assert error is None
    else:
        assert error[0] == 400
        assert error[1]['code'] == ('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED' if len(domains) > 1
                                    else 'EXPLICIT_DOMAIN_NOT_SUPPORTED')


@pytest.mark.parametrize('payload', [
    {'business_domain_id': 205},
    {'business_domain_ids': [205]},
    {'business_domain_ids': [205, 205]},
    {'authorized_semantic_scope': {'semantic_model_id': 81, 'business_domain_ids': [205], 'scope_mode': 'EXPLICIT_DOMAINS'}},
])
@pytest.mark.parametrize('method,arguments', [
    ('_handle_translate', ()), ('_handle_execute', ()), ('_handle_ast_to_sql', ()),
    ('_handle_metric_resolve', ()), ('_handle_relationship_resolve', ()),
    ('_handle_metric_lineage', ('81:sales',)),
])
def test_all_request_handlers_reject_explicit_scope_before_access(payload, method, arguments, monkeypatch):
    monkeypatch.setattr(api, 'get_translator', lambda: pytest.fail('no catalog, Redis, SQL or export access allowed'))
    monkeypatch.setattr(api, 'log', lambda _: None)
    # The former single-domain capability blocker is closed. Keep each alias
    # contrast: valid single-domain admission, then unsupported multi-domain
    # rejection at the same public handler before any service access.
    assert api._scope_contract_error({'modelId': '81', **payload}) is None
    blocked = json.loads(json.dumps(payload))
    if 'authorized_semantic_scope' in blocked:
        blocked['authorized_semantic_scope']['business_domain_ids'] = [205, 206]
    else:
        blocked.pop('business_domain_id', None)
        blocked['business_domain_ids'] = [205, 206]
    content = json.dumps({'modelId': '81', 'asl': '{}', 'sql': 'SELECT 1', **blocked}).encode()
    handler = object.__new__(api.APIHandler)
    handler.path = '/offline-contract-test'
    handler.headers = {'Content-Length': str(len(content))}
    handler.rfile = io.BytesIO(content)
    responses = []
    handler._send_response = lambda *args: responses.append(args)
    getattr(handler, method)(*arguments)
    assert len(responses) == 1
    assert responses[0][0] == 400
    assert responses[0][1]['code'] == 'EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'


@pytest.mark.parametrize('payload', [
    {'business_domain_ids': None}, {'business_domain_ids': [True]},
    {'business_domain_ids': [205.0]}, {'business_domain_ids': ['205']},
    {'business_domain_ids': [-1]}, {'business_domain_ids': [] , 'business_domain_id': 205},
    {'business_domain_ids': [206], 'business_domain_id': 205},
    {'business_domain_id': False},
    {'authorized_semantic_scope': {'semantic_model_id': 82, 'business_domain_ids': [], 'scope_mode': 'MODEL_WIDE'}},
    {'authorized_semantic_scope': {'semantic_model_id': 81, 'business_domain_ids': [205], 'scope_mode': 'MODEL_WIDE'}},
    {'business_domain_ids': [], 'authorized_semantic_scope': {'semantic_model_id': 81, 'business_domain_ids': [205], 'scope_mode': 'EXPLICIT_DOMAINS'}},
])
def test_scope_inconsistency_is_not_treated_as_model_wide(payload):
    error = api._scope_contract_error({'modelId': '81', **payload})
    assert error[0] == 400
    assert error[1]['code'] == 'REQUEST_SCOPE_INVALID'


def test_model_wide_authorized_scope_matches_current_request():
    assert api._scope_contract_error({'modelId': '81', 'business_domain_ids': [],
        'authorized_semantic_scope': {'semantic_model_id': 81, 'business_domain_ids': [],
                                      'scope_mode': 'MODEL_WIDE'}}) is None


class ScopeRedis:
    def __init__(self, payloads):
        self.payloads, self.reads, self.scans = payloads, [], []

    def get(self, key):
        self.reads.append(key)
        value = self.payloads.get(key)
        return json.dumps(value) if value is not None else None

    def scan_iter(self, match, count):
        self.scans.append(match)
        return [key for key in self.payloads if fnmatch.fnmatchcase(key, match)]


def loader_for(kind, payloads):
    loader = RedisDSLLoader()
    loader._redis = ScopeRedis(payloads)
    # Test the real lookup/cache/scan boundaries; adaptation is independent.
    setattr(loader, '_adapt_' + kind, lambda data, model: data)
    return loader


@pytest.mark.parametrize('kind', ['metric', 'entity', 'dimension'])
def test_model_qualified_lookup_does_not_read_global_or_other_model_keys(kind):
    legacy = f'semantic_model:{kind}:revenue'
    foreign = f'semantic_model:82:{kind}:revenue'
    loader = loader_for(kind, {legacy: {'semantic_model_id': 82}, foreign: {'semantic_model_id': 82}})
    assert getattr(loader, 'get_' + kind)('revenue', '81') is None
    assert loader.redis.reads == [f'semantic_model:81:{kind}:revenue']


@pytest.mark.parametrize('kind', ['metric', 'entity', 'dimension'])
def test_model_qualified_scan_has_no_legacy_fallback(kind):
    loader = loader_for(kind, {
        f'semantic_model:81:{kind}:good': {'semantic_model_id': 81},
        f'semantic_model:82:{kind}:bad': {'semantic_model_id': 82},
        f'semantic_model:{kind}:legacy': {'semantic_model_id': 82},
    })
    assert list(loader._iter_kind(kind, '81')) == [{'semantic_model_id': 81}]
    assert loader.redis.scans == [f'semantic_model:81:{kind}:*']
    assert loader.redis.reads == [f'semantic_model:81:{kind}:good']


@pytest.mark.parametrize('kind', ['metric', 'entity', 'dimension'])
@pytest.mark.parametrize('declared_key', ['semantic_model_id', 'model_id'])
def test_model_payload_conflict_cannot_enter_the_cache(kind, declared_key):
    loader = loader_for(kind, {f'semantic_model:81:{kind}:bad': {declared_key: 82}})
    assert getattr(loader, 'get_' + kind)('bad', '81') is None
    assert getattr(loader, '_' + kind + '_cache') == {}


@pytest.mark.parametrize('kind', ['metric', 'entity', 'dimension'])
def test_switching_models_does_not_reuse_another_models_cached_record(kind):
    loader = loader_for(kind, {
        f'semantic_model:81:{kind}:same': {'semantic_model_id': 81},
        f'semantic_model:82:{kind}:same': {'semantic_model_id': 82},
    })
    get = getattr(loader, 'get_' + kind)
    assert get('same', '81')['semantic_model_id'] == 81
    assert get('same', '82')['semantic_model_id'] == 82
    assert get('same', '81')['semantic_model_id'] == 81
    assert loader.redis.reads == [f'semantic_model:81:{kind}:same', f'semantic_model:82:{kind}:same']
