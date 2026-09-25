from copy import deepcopy

import httpx
import pytest

from tools.cutover.harness_manifest import freeze_candidates,freeze_versions,verify_frozen
from tools.cutover.harness_safety import state_safety,removal_history_safety
from tools.cutover.run_raw_transition_benchmark import run,frozen_publication,network_guard
from test_cutover_raw_transition_benchmark import inputs
from test_v2_raw_turn_recognition import ScriptedTransport
from tools.cutover.harness_dryplan import observe_dry_plan


@pytest.mark.asyncio
async def test_frozen_candidate_and_evaluator_identity_are_checked(inputs):
    rows,catalog,raw,settings,_=inputs
    with network_guard():
        publication=frozen_publication(raw,catalog,[])
        candidates=freeze_candidates(publication,rows[0])
    manifest=freeze_versions(catalog,commit='fixture-commit',settings=settings,candidate_snapshot=candidates)
    verify_frozen(manifest)
    assert manifest['prompts']['PARSE']['hash'] and manifest['prompts']['PROBE']['hash']
    assert manifest['temperature']==0 and manifest['semantic_candidate_baseline_id'] is None
    assert 'private-test-token' not in str(manifest)
    changed=deepcopy(manifest);changed['evaluator_hash']='wrong'
    with pytest.raises(ValueError,match='EVALUATOR_CHANGED'):verify_frozen(changed)
    changed=deepcopy(manifest);changed['runtime_source_hashes']['app/semantic_v2/recognition.py']='wrong'
    with pytest.raises(ValueError,match='RUNTIME_OR_PROMPT_CHANGED'):verify_frozen(changed)


@pytest.mark.asyncio
async def test_hard_safety_observes_native_proofs_and_rejects_tampered_acceptance(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    capture=captures[-1]
    clean=state_safety(capture['result'],capture['before'])
    assert clean['scope_expansion'] is False and clean['catalog_id_fabrication'] is False
    altered=deepcopy(capture['result']);ref=altered['plan']['logical_plan']['payload']['measures'][0]
    ref['canonical_id']='invented';ref['business_domain_ids']=[206]
    wrong=state_safety(altered,capture['before'])
    assert wrong['scope_expansion'] and wrong['catalog_id_fabrication']


@pytest.mark.asyncio
async def test_existing_dry_plan_seam_observes_same_accepted_plan_without_execution(inputs):
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    output=observe_dry_plan(captures[0],rows[0],catalog,raw)
    assert output['status']=='OBSERVED_SUPPLEMENTAL_NATIVE_SEAM'
    assert output['same_raw_runtime_entry'] is False and output['SQL_executed'] is False
    assert not any(output['external_call_attempts'].values())
    assert output['public_receipt']['logical_input_hash']


@pytest.mark.asyncio
async def test_dry_plan_respects_native_translator_pin_finish_ownership(inputs,monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    import sys
    from tools.cutover import harness_dryplan
    root=Path(__file__).resolve().parents[1]
    sql=root/'sql-translator' if (root/'sql-translator').is_dir() else root.parent/'sql-translator'
    monkeypatch.syspath_prepend(str(sql))
    import pinned_catalog
    rows,catalog,raw,settings,steps=inputs;captures=[]
    await run(rows,catalog,raw,settings,transport=httpx.MockTransport(ScriptedTransport(steps)),private_capture=captures.append)
    original=harness_dryplan.lower_asl2;finishes=[]
    def lower(*a,**kw):return replace(original(*a,**kw),status='SUPPORTED_PLAN_ONLY',asl={'fixture':'pin-lifecycle-only'})
    def translate(pin,*a,**kw):
        receipt=pin.finish();finishes.append(receipt)
        return {'success':True,'catalog_pin':receipt}
    monkeypatch.setattr(harness_dryplan,'lower_asl2',lower)
    monkeypatch.setattr(pinned_catalog,'translate_pinned_catalog',translate)
    output=observe_dry_plan(captures[0],rows[0],catalog,raw)
    assert len(finishes)==1 and output['public_receipt']['native_success'] is True
    assert not any(output['external_call_attempts'].values())
    def wrong_identity(pin,*a,**kw):
        receipt=pin.finish();receipt['catalog_version']='different'
        return {'success':True,'catalog_pin':receipt}
    monkeypatch.setattr(pinned_catalog,'translate_pinned_catalog',wrong_identity)
    with pytest.raises(ValueError,match='CATALOG_ACCEPTANCE_IDENTITY_MISMATCH'):
        observe_dry_plan(captures[0],rows[0],catalog,raw)


def _state_result(values,patch):
    return {'plan':{'logical_plan':{'task_id':'task'}},'resolution':{'task_patch':patch},
        'next_state':{'payload':{'tasks':{'task':{'active_version':1,'versions':[{'version':1,'semantics':{'metrics':values}}]}}}}}


def test_remove_last_observation_does_not_require_a_clear_barrier_to_exist():
    removed=_state_result([],{'removes':[{'slot_path':'metrics'}]})
    stable=_state_result([],{});revived=_state_result(['old-metric'],{})
    assert removal_history_safety([removed,stable])=={'remove_last_resurrection':False}
    assert removal_history_safety([removed,revived])=={'remove_last_resurrection':True}
    explicit=_state_result(['new-metric'],{'adds':[{'slot_path':'metrics'}]})
    assert removal_history_safety([removed,explicit])=={}
