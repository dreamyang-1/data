import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.cutover import round55_healthy_fixture as fixture


@pytest.mark.parametrize('base', [fixture.BASE, fixture.RECORDED_BASE])
def test_three_deep_chains_preserve_reviewed_inputs(base):
    rows = fixture.cases(base)
    with patch.object(fixture.prior, 'cases', lambda: fixture.cases(base)):
        assert fixture.prior.decode_fixture(json.dumps(rows, ensure_ascii=False).encode('utf8')) == rows
    assert len(rows) == 3 and [len(c['utterances']) for c in rows] == [4, 4, 3]
    assert rows[1]['utterances'][2] == '不要订单笔数'
    assert rows[1]['utterances'][3] == '换今年'  # Observe a turn after REMOVE.


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['encoding', 'scope', 'empty'])
async def test_invalid_round55_inputs_stop_before_provider(tmp_path, monkeypatch, fault):
    from tools.cutover import round55_live_chains as live
    data = fixture.cases()
    if fault == 'scope': data[0]['scope']['business_domain_ids'] = []
    if fault == 'empty': data[0]['utterances'][0] = ''
    path = tmp_path / 'fixture.json'
    path.write_bytes(b'\xff' if fault == 'encoding' else json.dumps(data).encode('utf8'))
    monkeypatch.setattr(live.native, 'read', lambda _: {})
    def forbidden(*args, **kw): raise AssertionError('PROVIDER_CONSTRUCTION_FORBIDDEN')
    monkeypatch.setattr(live.native, 'ModelRecorder', forbidden)
    with pytest.raises((ValueError, UnicodeError)):
        await live.run(SimpleNamespace(fixture=path, current_catalog=path, evidence=tmp_path))


@pytest.mark.asyncio
async def test_settings_freeze_precedes_foreign_fixture_imports(tmp_path, monkeypatch):
    from tools.cutover import round55_live_chains as live
    path = tmp_path / 'fixture.json'; path.write_bytes(b'[]'); events = []
    class Settings:
        def __init__(self): events.append('AGENT_SETTINGS')
        def model_copy(self, **kw): return self
    def preflight(*args):
        events.append('FOREIGN_FIXTURE_IMPORT')
        raise ValueError('STOP_BEFORE_MODEL')
    monkeypatch.setattr(live.native, 'Settings', Settings)
    monkeypatch.setattr(live.native, 'read', lambda _: {})
    monkeypatch.setattr(live, 'preflight', preflight)
    with pytest.raises(ValueError, match='STOP_BEFORE_MODEL'):
        await live.run(SimpleNamespace(fixture=path, current_catalog=path, evidence=tmp_path))
    assert events == ['AGENT_SETTINGS', 'FOREIGN_FIXTURE_IMPORT']
