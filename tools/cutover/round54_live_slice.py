"""Bounded, opt-in live observation; production model and runtime are unchanged.

Only actual accepted prefix receipts can be reused. Reuse is counted separately
from newly executed turns. Frozen source evidence is never filled from a model.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from tools.cutover import live_followup_round5_1 as native
from tools.cutover.round53_live_recheck import sources, SERVICE_ROOT
from tools.cutover.round54_healthy_fixture import cases, preflight, BASE
from tools.cutover.evaluation_contract import digest


def freeze_sources(evidence):
    files = [evidence / n for n in ('live_catalog_snapshot.json', 'source_observations.json', 'targeted_observations.json')]
    files += [SERVICE_ROOT / n for n in ('catalog_publication.py', 'catalog_value_candidates.py')]
    return {str(p): sha256(p.read_bytes()).hexdigest() for p in files}


async def run(args):
    # Capture Agent settings before Oagnet's legacy module import loads the
    # workspace-root .env API_KEY. Never re-read provider settings after
    # fixture construction; the evaluation must use the Agent's configured key.
    settings = native.Settings().model_copy(update={'intent_model_max_retries': 0, 'intent_model_timeout_seconds': 60})
    raw = args.fixture.read_bytes()
    catalog = native.read(ROOT / 'docs/cutover/evaluation_gates/frozen_catalog.json')
    snapshot = native.read(args.current_catalog)
    observations = native.read(args.evidence / 'source_observations.json')
    receipt = preflight(raw, snapshot, observations, catalog)
    frozen_catalog_path = ROOT / '.eval_private/harness-20260909T083000Z/catalog_snapshot.json'
    frozen_catalog_raw = frozen_catalog_path.read_bytes()
    if sha256(frozen_catalog_raw).hexdigest() != catalog['source_snapshot_sha256']:
        raise ValueError('INVALID_TEST_INPUT:CATALOG_PIN_SNAPSHOT')
    if digest(snapshot) != digest(json.loads(frozen_catalog_raw)):
        raise ValueError('INVALID_TEST_INPUT:CURRENT_CATALOG_DRIFT')
    source_hashes = freeze_sources(args.evidence); runtime_hashes = native.hashes()
    # The current read-only audit must agree with the certified frozen source.
    assert snapshot['catalog_version'] == native.read(args.evidence / 'live_catalog_snapshot.json')['catalog_version']
    seed = native.read(args.seed)
    parent = native.read(args.seed.parent / 'result.json')
    assert seed['turn_id'] in parent['turns'] and parent['calls']
    assert seed['question'] == BASE and not seed['error'] and seed['result']['plan']
    seed_state = native.ScopedArtifact.model_validate(seed['result']['next_state'])
    assert seed_state.context.authorized_scope.semantic_model_id == 81
    assert list(seed_state.context.authorized_scope.business_domain_ids) == [205]
    with native.network_guard(), patch.object(sys, 'path', [*sys.path, str(SERVICE_ROOT), str(SERVICE_ROOT / 'tests')]), ExitStack() as precheck:
        source_check = native.FrozenSourceValues(observations, catalog=catalog, snapshot=snapshot,
            expected_hash=observations['artifact_hash'], allow_synthetic=False)
        import catalog_value_sources
        precheck.enter_context(patch.object(catalog_value_sources, 'observe', source_check.observe))
        precheck.enter_context(patch.object(catalog_value_sources, 'observe_probe', source_check.observe_probe))
        checked = native.frozen_publication(frozen_catalog_raw, catalog, [], native_source_values=True,
            recorded_activation_id=seed_state.context.catalog_pin.activation_id)
        from app.semantic_v2.catalog_bridge import ScopedPlanSession
        request = native.ChatRequest(**native.SCOPE, application_id='isolated-evaluation',
            conversation_id=native.CONVERSATION, message_id='preflight', question=BASE)
        session = ScopedPlanSession(request, native.TrustedIdentity(tenant_id='evaluation', user_id='evaluation'), checked)
        session.restore(seed_state, kind='CONVERSATION')
        session.accept_catalog()
    receipt['catalog_pin_restore'] = 'PASS'
    if getattr(args, 'preflight_only', False):
        print(json.dumps(receipt, ensure_ascii=False))
        return
    selected = [c for c in cases() if not args.case or c['case_id'] in args.case]
    if args.case and len(selected) != len(args.case): raise ValueError('INVALID_TEST_INPUT:CASE_ID')
    prior_calls = sum(len(native.read(p / 'result.json')['calls']) for p in args.prior_output)
    if args.max_model_calls < 1 or prior_calls + args.max_model_calls > args.total_budget:
        raise ValueError('LIVE_MODEL_BUDGET_EXCEEDED')
    args.output.mkdir(parents=True, exist_ok=False)
    native.write(args.output / 'preflight.json', receipt)
    native.write(args.output / 'input_freeze.json', dict(cases=selected, fixture_sha256=sha256(raw).hexdigest(),
        runtime_hashes=runtime_hashes, source_hashes=source_hashes, catalog_read_hash=sha256(args.current_catalog.read_bytes()).hexdigest(),
        seed_capture_hash=digest(seed), seed_runtime_hashes=parent['runtime_hashes'],
        model='qwen3.7-max', thinking=False, temperature=0, retry=0,
        current_budget=args.max_model_calls, prior_calls=prior_calls, total_budget=args.total_budget,
        scope=native.SCOPE, clock=native.CLOCK, prefix_reuse='ACTUAL_EXECUTED_RECEIPT; NOT_NEW_LIVE_TURN'))
    denied = []; rows = []; records = []; cache = {}; reused = []
    # Only the exact original prefix is shared; no hand-created state is accepted.
    cache[digest([False, [BASE]])[:24]] = seed
    for folder in args.prior_output:
        for key in native.read(folder / 'result.json')['turns']:
            item = native.read(folder / (key + '.json'))
            if item['error'] is None and item['result'] is not None:
                cache[key] = item
    assert settings.intent_model_name == 'qwen3.7-max' and settings.intent_model_enable_thinking is False
    # Provider construction occurs only after the complete fixture preflight.
    model = native.ModelRecorder(settings, max_calls=args.max_model_calls, capture_exchanges=True)
    try:
        with native.network_guard(), sources(args.evidence) as targeted, ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'path', [*sys.path, str(SERVICE_ROOT), str(SERVICE_ROOT / 'tests')]))
            publication = native.frozen_publication(frozen_catalog_raw, catalog,
                denied, native_source_values=True, recorded_activation_id=seed_state.context.catalog_pin.activation_id)
            source = native.FrozenSourceValues(observations, catalog=catalog, snapshot=snapshot,
                expected_hash=observations['artifact_hash'], allow_synthetic=False)
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources, 'observe', source.observe))
            stack.enter_context(patch.object(catalog_value_sources, 'observe_probe', source.observe_probe))
            initial, pending_receipt = native.declared_pending(publication)
            native.write(args.output / 'pending_precondition.json', dict(receipt=pending_receipt, before=initial))
            observer = stack.enter_context(native.RuntimeObserver())
            traces = []
            class Trace(native.logging.Handler):
                def emit(self, event):
                    if hasattr(event, 'context_arbitration'): traces.append(deepcopy(event.context_arbitration))
            handler = Trace(); logger = native.logging.getLogger('app.semantic_v2.context_proposal'); level = logger.level
            logger.setLevel(native.logging.INFO); logger.addHandler(handler)
            stack.callback(logger.removeHandler, handler); stack.callback(logger.setLevel, level)
            engine = native.RawTurnPlanner(model, publication, clock=lambda: datetime.fromisoformat(native.CLOCK))
            provider_blocked = False
            for case in selected:
                before = deepcopy(initial) if case['initial_pending'] else dict(state=None, pending=None, plans=[], history=[])
                chain = []; status = 'NOT_RUN'; blocked = None
                for i, text in enumerate(case['utterances']):
                    key = digest([case['initial_pending'], case['utterances'][:i + 1]])[:24]
                    if key not in cache:
                        if provider_blocked:
                            status = 'NOT_RUN_PROVIDER'; blocked = 'PROVIDER_AUTHENTICATION_REJECTED'; break
                        if args.max_model_calls - len(model.calls) < 2:
                            status = 'NOT_RUN_BUDGET'; blocked = 'MODEL_BUDGET_EXHAUSTED'; break
                        # Recheck the exact frozen text and JSON serialization before each request.
                        assert sha256(args.fixture.read_bytes()).hexdigest() == receipt['fixture_sha256']
                        assert text == next(c for c in cases() if c['case_id'] == case['case_id'])['utterances'][i]
                        req = native.ChatRequest(**case['scope'], application_id='isolated-evaluation',
                            conversation_id=native.CONVERSATION, message_id='turn-' + key, question=text)
                        req.model_dump_json()
                        model.begin_turn(case['case_id'], i); observer.begin_turn(case['case_id'], i)
                        traces.clear(); call_start = len(model.calls); source_start = len(source.calls); timings = []
                        original = model.complete; started = monotonic()
                        async def timed(**kw):
                            began = monotonic()
                            try: return await original(**kw)
                            finally: timings.append(dict(stage=kw['stage'], seconds=monotonic() - began))
                        model.complete = timed; result = None; error = None
                        state = native.ScopedArtifact.model_validate(before['state']) if before['state'] else None
                        try:
                            result = await engine.run(req, native.TrustedIdentity(tenant_id='evaluation', user_id='evaluation'),
                                state=state, pending=native.ScopedArtifact.model_validate(before['pending']) if before['pending'] else None,
                                plans=tuple(native.ScopedArtifact.model_validate(p) for p in before['plans']))
                        except Exception as exc:
                            error = dict(type=type(exc).__name__, reason=native.failure_reason(exc),
                                context_trace=getattr(exc, 'context_trace', None))
                        finally: model.complete = original
                        assert (state.model_dump(mode='json') if state else None) == before['state']
                        item = dict(case_id=case['case_id'], turn_id=key, turn_index=i, question=text, before=deepcopy(before),
                            result=result.model_dump(mode='json') if result else None, error=error,
                            events=deepcopy(observer.active['events']), traces=deepcopy(traces),
                            outputs=deepcopy(model.outputs), exchanges=deepcopy(model.exchanges),
                            model_calls=deepcopy(model.calls[call_start:]), source_calls=deepcopy(source.calls[source_start:]),
                            stage_timings=timings, total_seconds=monotonic() - started, state_unchanged=True)
                        native.write(args.output / (key + '.json'), item); cache[key] = item; records.append(key)
                        if any(c.get('status') in {401, 403} for c in item['model_calls']):
                            provider_blocked = True
                        print(json.dumps(dict(case=case['case_id'], turn=i, error=error, plan=bool(result and result.plan), calls=len(model.calls))), flush=True)
                    else: reused.append(dict(case_id=case['case_id'], turn_index=i, turn_id=key, capture_hash=digest(cache[key])))
                    item = cache[key]; chain.append(key)
                    if item['error'] or item['result'] is None:
                        status = 'TERMINAL' if i == len(case['utterances']) - 1 else 'BLOCKED_HISTORY'; blocked = key; break
                    value = item['result']; before['state'] = value['next_state']
                    if value['plan']:
                        by_task = {p['payload']['task_id']: p for p in before['plans']}
                        by_task[value['plan_state']['payload']['task_id']] = value['plan_state']
                        before['plans'] = list(by_task.values()); before['pending'] = None
                    else: before['pending'] = value['pending_state']
                    before['history'].append(value)
                    if not value['plan'] and i < len(case['utterances']) - 1:
                        status = 'BLOCKED_HISTORY'; blocked = key; break
                    status = 'PLAN' if value['plan'] else 'CLARIFICATION'
                rows.append(dict(case_id=case['case_id'], status=status, turns=chain, blocked_by=blocked,
                    declared_turns=len(case['utterances']), reached_turns=len(chain)))
                native.write(args.output / 'progress.json', dict(cases=rows, turns=records, calls=model.calls))
            assert native.hashes() == runtime_hashes and freeze_sources(args.evidence) == source_hashes and not denied
            native.write(args.output / 'result.json', dict(cases=rows, turns=records, reused=reused, calls=model.calls,
                runtime_hashes=runtime_hashes, source_reads=source.calls, targeted_reads=targeted.calls,
                complete=True, model_calls=len(model.calls), prior_model_calls=prior_calls,
                sql_calls=0, production_writes=0, blind_access=0, invalid_requests=0))
    finally: await model.upstream.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fixture', 'evidence', 'current-catalog', 'seed', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--prior-output', type=Path, action='append', default=[])
    parser.add_argument('--max-model-calls', type=int, default=12)
    parser.add_argument('--total-budget', type=int, default=12)
    parser.add_argument('--allow-model-calls', action='store_true', required=True)
    parser.add_argument('--preflight-only', action='store_true')
    asyncio.run(run(parser.parse_args()))
