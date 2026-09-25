from copy import deepcopy
import pytest

from tools.cutover.round53_healthy_cases import ROWS, validate_case
from tools.cutover.live_followup_round5_1 import CLOCK, SCOPE, cases


@pytest.mark.parametrize('row', ROWS)
def test_reviewed_utf8_healthy_cases_roundtrip(row):
    import json
    case = dict(case_id='HC53-'+row[0],scope=SCOPE,clock=CLOCK,
        utterances=([] if row[0]=='H' else ['查询去年江苏订单笔数'])+row[2])
    validate_case(json.loads(json.dumps(case,ensure_ascii=False).encode('utf-8').decode('utf-8')))


@pytest.mark.parametrize('damage', ['question_marks','replacement_character','valid_but_wrong_question','scope','clock'])
def test_input_corruption_is_rejected_before_model_construction(damage):
    case = deepcopy(next(c for c in cases() if c['case_id']=='LF51-F'))
    if damage=='question_marks': case['utterances'][1]='????????'
    if damage=='replacement_character': case['utterances'][1]='\ufffd不是上海'
    if damage=='valid_but_wrong_question': case['utterances'][1]='换江苏'
    if damage=='scope': case['scope']['business_domain_ids']=[206]
    if damage=='clock': case['clock']='2026-09-10T09:00:00+08:00'
    with pytest.raises(ValueError,match='LIVE_FIXTURE'): validate_case(case)


@pytest.mark.asyncio
async def test_actual_live_entry_rejects_damaged_fixture_before_source_or_model(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from tools.cutover import round53_live_recheck as live
    case = dict(case_id='HC53-B',scope=SCOPE,clock=CLOCK,utterances=['查询去年江苏订单笔数','???'])
    path=tmp_path/'case.json';path.write_text(json.dumps(case,ensure_ascii=False),encoding='utf-8')
    def forbidden(*args): raise AssertionError('source/model must not initialize')
    monkeypatch.setattr(live,'sources',forbidden)
    with pytest.raises(ValueError,match='TEXT_ENCODING_INVALID'):
        await live.main(SimpleNamespace(prior_output=[],max_model_calls=2,seed=tmp_path/'seed.json',case=path))


@pytest.mark.asyncio
async def test_live_entry_freezes_actual_source_versions_before_any_provider(monkeypatch, tmp_path):
    import json
    from hashlib import sha256
    from types import SimpleNamespace
    from tools.cutover import round53_live_recheck as live
    case=dict(case_id='HC53-B',scope=SCOPE,clock=CLOCK,utterances=['查询去年江苏订单笔数','换今年'])
    case_path=tmp_path/'case.json';case_path.write_text(json.dumps(case,ensure_ascii=False),encoding='utf-8')
    names=('source_observations.json','targeted_observations.json','live_catalog_snapshot.json')
    for name in names:(tmp_path/name).write_text('{}',encoding='utf-8')
    def stop(*args): raise RuntimeError('STOP_BEFORE_PROVIDER')
    monkeypatch.setattr(live,'sources',stop)
    args=SimpleNamespace(prior_output=[],max_model_calls=2,seed=tmp_path/'seed.json',case=case_path,
        evidence=tmp_path,output=tmp_path/'future-live')
    with pytest.raises(RuntimeError,match='STOP_BEFORE_PROVIDER'):await live.main(args)
    receipt=json.loads((tmp_path/'future-live.source-freeze.json').read_text(encoding='utf-8'))
    assert receipt['source_files']=={name:sha256(b'{}').hexdigest() for name in names}
    assert receipt['prior_model_calls']==0 and len(receipt['contract_sources'])==4
