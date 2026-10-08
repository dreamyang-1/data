from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tools.cutover.round54_healthy_fixture import cases, decode_fixture


@pytest.mark.parametrize('fault', ['utf8', 'empty', 'question_marks', 'replacement', 'different_text', 'missing_model', 'wider_scope'])
def test_bad_input_never_becomes_a_live_request(fault):
    payload = cases()
    if fault == 'empty': payload[0]['utterances'][1] = ''
    if fault == 'question_marks': payload[0]['utterances'][1] = '????'
    if fault == 'replacement': payload[0]['utterances'][1] = '\ufffd'
    if fault == 'different_text': payload[0]['utterances'][1] = '另一个有效但未经审核的问题'
    if fault == 'missing_model': payload[0]['scope'].pop('semantic_model_id')
    if fault == 'wider_scope': payload[0]['scope']['business_domain_ids'] = []
    raw = b'\xff' if fault == 'utf8' else json.dumps(payload, ensure_ascii=False).encode('utf8')
    with pytest.raises(ValueError, match='INVALID_TEST_INPUT'): decode_fixture(raw)


def test_reviewed_fixture_roundtrip_and_counts():
    payload = cases(); raw = json.dumps(payload, ensure_ascii=False).encode('utf8')
    assert decode_fixture(raw) == payload
    assert len(payload) == 11 and sum(len(c['utterances']) for c in payload) == 25
    payload[0]['utterances'][0] = 'mutated'
    assert cases()[0]['utterances'][0] != 'mutated'


@pytest.mark.asyncio
async def test_actual_entry_preflight_failure_precedes_provider_construction(tmp_path, monkeypatch):
    from tools.cutover import round54_live_slice as live
    path = tmp_path / 'fixture.json'; path.write_bytes(b'\xff')
    monkeypatch.setattr(live.native, 'read', lambda _: {})
    def forbidden(*a, **kw): raise AssertionError('PROVIDER_CONSTRUCTION_FORBIDDEN')
    monkeypatch.setattr(live.native, 'ModelRecorder', forbidden)
    with pytest.raises(ValueError, match='INVALID_TEST_INPUT'):
        await live.run(SimpleNamespace(fixture=path, current_catalog=path, evidence=tmp_path))


@pytest.mark.asyncio
async def test_agent_settings_are_captured_before_foreign_fixture_environment(tmp_path, monkeypatch):
    from tools.cutover import round54_live_slice as live
    path = tmp_path / 'fixture.json'; path.write_bytes(b'[]')
    events = []
    class Settings:
        def __init__(self): events.append('AGENT_SETTINGS')
        def model_copy(self, **kw): return self
    def fixture(*args):
        events.append('FOREIGN_FIXTURE_IMPORT')
        raise ValueError('STOP_BEFORE_MODEL')
    monkeypatch.setattr(live.native, 'Settings', Settings)
    monkeypatch.setattr(live.native, 'read', lambda _: {})
    monkeypatch.setattr(live, 'preflight', fixture)
    with pytest.raises(ValueError, match='STOP_BEFORE_MODEL'):
        await live.run(SimpleNamespace(fixture=path, current_catalog=path, evidence=tmp_path))
    assert events == ['AGENT_SETTINGS', 'FOREIGN_FIXTURE_IMPORT']
