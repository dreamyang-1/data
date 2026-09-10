"""Reviewed public evaluation inputs and fail-before-provider preflight."""
from copy import deepcopy
import json
from hashlib import sha256

from tools.cutover.live_followup_round5_1 import CLOCK, SCOPE
from tools.cutover.evaluation_contract import digest

BASE = '查询去年江苏订单笔数'
NEW = '查询今年北京市销售总数量'
ROWS = [
    ('A', 'REGION_REPLACE', [BASE, '换成北京市']),
    ('B', 'TIME_REPLACE', [BASE, '换今年']),
    ('C', 'ADD', [BASE, '再加销售总数量']),
    ('D', 'REMOVE', [BASE, '再加销售总数量', '不要销售总数量']),
    ('E', 'CLEAR_NO_RESURRECTION', [BASE, '不限地区', '换今年']),
    ('F', 'CORRECTION', [BASE, '不是江苏，是北京市']),
    ('G', 'NEW_TASK', [BASE, NEW]),
    ('H', 'HISTORICAL_RETURN', [BASE, NEW, '返回刚才江苏订单笔数那个任务']),
    ('I', 'PENDING_NEW_TASK', ['查询今年北京市订单笔数']),
    ('J', 'TRUE_AMBIGUITY', [BASE, NEW, '前面两个任务选一个继续，具体哪个我还没确定']),
    ('K', 'SELF_CONTAINED_NEW_TASK', [BASE, NEW]),
]


def cases():
    return [dict(case_id='HC54-'+key, capability=kind, utterances=deepcopy(text),
        initial_pending=key == 'I', scope=deepcopy(SCOPE), clock=CLOCK, split='PUBLIC_DEV')
        for key, kind, text in ROWS]


def decode_fixture(raw):
    try:
        fixture = json.loads(raw.decode('utf-8'))
        if fixture != cases(): raise ValueError('REVIEWED_SOURCE_MISMATCH')
        for case in fixture:
            for question in case['utterances']:
                if not question or '\ufffd' in question or not any(c.isalnum() for c in question):
                    raise ValueError('TEXT_ENCODING_INVALID')
        return fixture
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError('INVALID_TEST_INPUT') from exc


def catalog_health(snapshot, observations):
    """Inspect the current certified metadata; never infer a missing time anchor."""
    if snapshot['scope']['semantic_model_id'] != 81 or snapshot['scope']['business_domain_ids'] != [205]:
        raise ValueError('INVALID_TEST_INPUT:CATALOG_SCOPE')
    docs = snapshot['documents']
    metrics = {m['metric_code']: m for d in docs for m in d['metrics']}
    entities = {e['entity_code']: e for d in docs for e in d['entities']}
    dimensions = {d['dim_code']: d for doc in docs for d in doc['dimensions']}
    proven = {}
    for code in ('order_count', 'sales_total_quantity'):
        metric = metrics[code]
        anchor = metric['time_caliber']['time_anchor']
        assert anchor and metric['calculation_rule']['calc_formula'] and metric['business_definition']['description']
        entity, attribute = anchor.split('.')
        time_field = next(a for a in entities[entity]['attributes'] if a['attr_code'] == attribute)
        assert time_field['field_mapping']
        assert metric['source_dependency']['bind_entity'] == ['sales_order']
        proven[code] = dict(name=metric['metric_name'], time_anchor=anchor,
            formula=metric['calculation_rule']['calc_formula'], time_field=time_field['field_mapping'],
            business_definition=metric['business_definition']['description'])
    fields = {}
    for entity, attr in [('province', 'province_name'), ('city', 'city_name')]:
        field = next(a for a in entities[entity]['attributes'] if a['attr_code'] == attr)
        assert field['field_mapping']
        fields[entity] = field['field_mapping']
    assert 'transaction_date' in dimensions and 'city' in dimensions
    for entity, value in [('province', '江苏省'), ('province', '北京市'), ('city', '北京市')]:
        assert any(e['query'] == value and e['observation']['complete']
            and e['observation']['field']['entity_code'] == entity
            and e['observation']['values'] == [value] for e in observations['entries'])
    assert snapshot['catalog_version'] == observations['catalog_version']
    return dict(status='CATALOG_HEALTHY_FIXTURE', metrics=proven, fields=fields,
        source_values='CERTIFIED_FROZEN_EXACT_OBSERVATIONS', scope=SCOPE,
        catalog_version=snapshot['catalog_version'], snapshot_hash=digest(snapshot),
        sales_amount_time_gap=metrics['sales_total_including_tax']['time_caliber']['time_anchor'] is None,
        boundary='Planning metadata and source proof; no executed SQL or entity-count identity certification')


def preflight(raw, snapshot, observations, catalog):
    from app.domain.models import ChatRequest
    from app.semantic_v2.recognition import current_turn_schema, semantic_task_schema
    from tools.cutover.frozen_source_values import FrozenSourceValues
    fixture = decode_fixture(raw)
    try:
        health = catalog_health(snapshot, observations)
        from pathlib import Path
        from unittest.mock import patch
        import sys
        root = Path(__file__).resolve().parents[2]
        service = root / 'Oagnet' if (root / 'Oagnet').is_dir() else root.parent / 'Oagnet'
        with patch.object(sys, 'path', [*sys.path, str(service)]):
            FrozenSourceValues(observations, catalog=catalog, snapshot=snapshot,
                expected_hash=observations['artifact_hash'], allow_synthetic=False)
        for case in fixture:
            for question in case['utterances']:
                req = ChatRequest(**case['scope'], question=question, application_id='isolated-evaluation',
                    conversation_id='preflight-only', message_id='preflight-only')
                req.model_dump_json()
        from types import SimpleNamespace
        for schema in [current_turn_schema(), semantic_task_schema(
                SimpleNamespace(reference_signals=[], topic_shift_signals=[]), {})]:
            from jsonschema import Draft202012Validator
            Draft202012Validator.check_schema(schema)
            json.dumps(schema, ensure_ascii=False).encode('utf-8').decode('utf-8')
    except Exception as exc:
        raise ValueError('INVALID_TEST_INPUT:CATALOG_OR_SCHEMA') from exc
    return dict(status='PASS', fixture_sha256=sha256(raw).hexdigest(), case_hash=digest(fixture),
        case_count=len(fixture), declared_turns=sum(len(c['utterances']) for c in fixture),
        catalog_health=health, model_calls=0)
