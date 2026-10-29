"""Display actual SQL defaults separately; missing metadata is not 'none'."""
import copy
import json

import pytest

from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import TrustedIdentity
from app.presentation.execution_trace import render_sql_execution_defaults
from app.presentation.intent_recognition import render_asl_extraction_json
from app.services.progress import progress_scope
from test_executed_metric_display import request
from test_http_adapters import StubClient


@pytest.mark.parametrize('limit,source,phrase', [
    (10000, 'DETAIL_DEFAULT', '系统明细查询默认上限'),
    (10, 'ASL', '来自 ASL.limit'),
    (None, 'NONE', '未添加 LIMIT'),
])
def test_explicit_limit_source(limit, source, phrase):
    text = render_sql_execution_defaults({'effective_limit': limit, 'limit_source': source, 'system_filters': []})
    assert phrase in text
    assert '系统补充筛选（不属于用户 ASL.filters）：无。' in text


def test_missing_metadata_does_not_invent_an_unlimited_query():
    assert '未提供' in render_sql_execution_defaults({})
    assert '未添加 LIMIT' not in render_sql_execution_defaults({})


@pytest.mark.parametrize('surface', [False, True])
@pytest.mark.asyncio
async def test_shared_sql_boundary_reports_rules_without_changing_asl_or_sql(monkeypatch, surface):
    ast = {'version': '2.0', 'subject': {'entity': 'hospital'},
           'metrics': [{'name': 'count'}], 'dimensions': [{'name': 'hospital.hospital_name'}],
           'filters': [], 'time_context': None, 'limit': None, 'ambiguity': []}
    original = copy.deepcopy(ast)
    sql = 'SELECT hospital_name, COUNT(*) AS count FROM hospital WHERE hospital_name IS NOT NULL GROUP BY hospital_name'
    summary = {'effective_limit': None, 'limit_source': 'NONE', 'system_filters': [
        {'condition': 'hospital_name IS NOT NULL', 'source': 'ENTITY_IDENTITY_DEFAULT', 'reason': '排除空名称组'}]}
    client = StubClient([
        {'success': True, 'sql': sql, 'effective_filter_summary': summary},
        {'success': True, 'columns': ['hospital_name', 'count'], 'data': [{'hospital_name': 'A', 'count': 1}], 'row_count': 1},
    ])
    adapter = HttpDataRetrievalAdapter(Settings(), client)
    identity = TrustedIdentity(tenant_id='t', user_id='u')
    events = []
    async def generate(*args, **kwargs):
        return {'asl': ast}
    monkeypatch.setattr('app.adapters.surface_asl.generate_surface_asl', generate)
    with progress_scope(events.append):
        if surface:
            await adapter.query_surface(request(), identity, mentions=[])
        else:
            await adapter._execute_validated_asl(request(), identity, asl=ast, semantic_model_id=81,
                business_domain_id=None, analysis_contract=None, metric_definitions=[], metric_definition_fingerprints=[])
    message = next(e['message'] for e in events if e['stage'] == 'SEMANTIC_QUERY_PLANNING')
    assert 'hospital_name IS NOT NULL' in message and '排除空名称组' in message
    assert '不属于用户 ASL.filters' in message
    assert '未添加 LIMIT' in message
    assert ast == original == json.loads(client.calls[0][2]['asl'])
    assert client.calls[1][2]['sql'] == sql


def test_asl_preserves_null_limit_without_claiming_an_unlimited_execution():
    ast = {'metrics': [], 'dimensions': [{'name': 'dealer.dealer_name'}], 'limit': None}
    text = render_asl_extraction_json(ast)
    # Execution defaults are explained by the SQL execution trace (tested above),
    # not necessarily repeated in the ASL display.
    assert '未添加 LIMIT' not in text
    assert '"limit": null' in text
    assert ast['limit'] is None
