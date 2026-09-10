"""Opt-in, bounded native V2 observation using frozen sources and real task receipts.

No SQL, service route, production state or model decision interception. A restored
prefix is versioned historical evidence, never a newly executed fresh chain.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from tools.cutover import live_followup_round5_1 as n
from tools.cutover.round53_live_recheck import sources, SERVICE_ROOT
from tools.cutover.round54_healthy_fixture import catalog_health
from tools.cutover.round55_live_chains import freeze_sources


def definitions():
    reference = '返回刚才江苏订单笔数那个任务'
    return [
        dict(case_id='R58-FRESH', question='查询去年江苏省订单笔数', origin='EMPTY'),
        dict(case_id='R58-C3', question=reference, origin='C3_ORIGINAL_BEFORE'),
        dict(case_id='R58-C3-TIME', question=reference+'，改成今年', origin='C3_ORIGINAL_BEFORE'),
        dict(case_id='R58-C3-METRIC', question=reference+'，再加销售总数量', origin='C3_ORIGINAL_BEFORE'),
        dict(case_id='R58-C3-GROUP', question=reference+'，按城市分组', origin='C3_ORIGINAL_BEFORE'),
        dict(case_id='R58-NEW', question='查询今年江苏省销售总数量', origin='R58-FRESH'),
        dict(case_id='R58-FOLLOW', question='换今年', origin='R58-FRESH'),
        dict(case_id='R58-CLEAR', question='不限地区', origin='R58-FRESH'),
        dict(case_id='R58-CLEAR-NEXT', question='换今年', origin='R58-CLEAR'),
        dict(case_id='R58-REMOVE', question='不要订单笔数', origin='B3_ORIGINAL_BEFORE'),
    ]


def following(capture):
    value = capture['result']; before = deepcopy(capture['before'])
    assert not capture['error'] and value and value['plan'], 'ACCEPTED_PREFIX_REQUIRED'
    before['state'] = value['next_state']; before['pending'] = None
    plans = {p['payload']['task_id']: p for p in before['plans']}
    plans[value['plan_state']['payload']['task_id']] = value['plan_state']
    before['plans'] = list(plans.values()); before['history'].append(value)
    return before


async def run(args):
    # Snapshot settings before importing Oagnet fixture modules.
    settings = n.Settings().model_copy(update={'intent_model_max_retries': 0, 'intent_model_timeout_seconds': 60})
    assert settings.intent_model_name == 'qwen3.7-max' and settings.intent_model_enable_thinking is False
    assert 1 <= args.max_calls <= 24
    assert datetime.now(timezone.utc) < datetime.fromisoformat(args.deadline)
    catalog = n.read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw = (ROOT/'.eval_private/harness-20260909T083000Z/catalog_snapshot.json').read_bytes()
    assert sha256(raw).hexdigest() == catalog['source_snapshot_sha256']
    snapshot = n.read(args.evidence/'live_catalog_snapshot.json')
    assert n.digest(snapshot) == n.digest(n.read(ROOT/'.eval_private/harness-20260909T083000Z/catalog_snapshot.json'))
    observations = n.read(args.evidence/'source_observations.json')
    health = catalog_health(snapshot, observations)
    assert 'city' in next(m for d in snapshot['documents'] for m in d['metrics'] if m['metric_code']=='order_count')['bind_dimensions']
    seeds = {name: n.read(args.recordings/(key+'.json')) for name, key in (
        ('C3_ORIGINAL_BEFORE', '81820fdbe3c3024e3ddb7356'), ('B3_ORIGINAL_BEFORE', '05485e11352852ae03b7690c'))}
    # C3 was rejected but its unmodified input contains actually accepted A/B
    # receipts. Restore that input, never its failed output or an invented state.
    for cap in seeds.values():
        assert cap['state_unchanged'] is True and cap['before']['state']
        assert cap['before']['plans'] and all(v['plan'] for v in cap['before']['history'])
    runtime_hashes = n.hashes(); source_hashes = freeze_sources(args.evidence)
    args.output.mkdir(parents=True, exist_ok=False)
    n.write(args.output/'input_freeze.json', dict(cases=definitions(), runtime_hashes=runtime_hashes,
        source_hashes=source_hashes, catalog_health=health, model=settings.intent_model_name,
        thinking=False, temperature=0, retry=0, scope=n.SCOPE, clock=n.CLOCK, max_calls=args.max_calls,
        deadline=args.deadline, seed_hashes={k:n.digest(v) for k,v in seeds.items()},
        evaluation_kind='PUBLIC_DEV_NATIVE_RUNTIME; FROZEN_SOURCES; VERSIONED_ACTUAL_PREFIX_RESTORE; NOT_E2E'))
    model = n.ModelRecorder(settings, max_calls=args.max_calls, capture_exchanges=True)
    captures = {}; rows = []; denied = []; provider_blocked = False
    try:
        with n.network_guard(), sources(args.evidence), ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'path', [*sys.path, str(SERVICE_ROOT), str(SERVICE_ROOT/'tests')]))
            observer = stack.enter_context(n.RuntimeObserver())
            traces = []
            class Trace(n.logging.Handler):
                def emit(self, event):
                    if hasattr(event, 'context_arbitration'): traces.append(deepcopy(event.context_arbitration))
            logger = n.logging.getLogger('app.semantic_v2.context_proposal'); level = logger.level; handler = Trace()
            logger.setLevel(n.logging.INFO); logger.addHandler(handler)
            stack.callback(logger.removeHandler, handler); stack.callback(logger.setLevel, level)
            for case in definitions():
                origin = case['origin']; cid = case['case_id']
                if provider_blocked or len(model.calls)+2 > args.max_calls or datetime.now(timezone.utc) >= datetime.fromisoformat(args.deadline):
                    rows.append(dict(case_id=cid, status='NOT_RUN_PROVIDER_OR_BUDGET')); continue
                if origin == 'EMPTY':
                    before = dict(state=None, pending=None, plans=[], history=[])
                    conversation = 'round58-fresh-'+args.output.name
                elif origin in seeds:
                    before = deepcopy(seeds[origin]['before']); conversation = seeds[origin]['conversation_id']
                else:
                    parent = captures.get(origin)
                    if not parent or parent['error'] or not parent['result'] or not parent['result']['plan']:
                        rows.append(dict(case_id=cid, status='BLOCKED_HISTORY', blocked_by=origin)); continue
                    before = following(parent); conversation = parent['conversation_id']
                activation = before['state']['context']['catalog_pin']['activation_id'] if before['state'] else None
                publication = n.frozen_publication(raw, catalog, denied, native_source_values=True, recorded_activation_id=activation)
                source = n.FrozenSourceValues(observations, catalog=catalog, snapshot=snapshot,
                    expected_hash=observations['artifact_hash'], allow_synthetic=False)
                import catalog_value_sources
                with patch.object(catalog_value_sources, 'observe', source.observe), patch.object(catalog_value_sources, 'observe_probe', source.observe_probe):
                    engine = n.RawTurnPlanner(model, publication, clock=lambda: datetime.fromisoformat(n.CLOCK))
                    request = n.ChatRequest(**n.SCOPE, application_id='isolated-evaluation', conversation_id=conversation,
                        message_id='turn-'+cid, question=case['question'])
                    state = n.ScopedArtifact.model_validate(before['state']) if before['state'] else None
                    model.begin_turn(cid, 0); observer.begin_turn(cid, 0); traces.clear()
                    start_calls = len(model.calls); started = monotonic(); timings = []; original = model.complete
                    async def timed(**kw):
                        began = monotonic()
                        try: return await original(**kw)
                        finally: timings.append(dict(stage=kw['stage'], seconds=monotonic()-began))
                    model.complete = timed; result = None; error = None
                    try:
                        result = await asyncio.wait_for(engine.run(request, n.TrustedIdentity(tenant_id='evaluation', user_id='evaluation'),
                            state=state, plans=tuple(n.ScopedArtifact.model_validate(p) for p in before['plans'])), timeout=125)
                    except Exception as exc:
                        error = dict(type=type(exc).__name__, reason=n.failure_reason(exc), context_trace=getattr(exc, 'context_trace', None))
                    finally: model.complete = original
                    assert (state.model_dump(mode='json') if state else None) == before['state']
                    item = dict(**case, turn_id=cid, conversation_id=conversation, before=before,
                        result=result.model_dump(mode='json') if result else None, error=error,
                        outputs=deepcopy(model.outputs), exchanges=deepcopy(model.exchanges),
                        events=deepcopy(observer.active['events']), traces=deepcopy(traces),
                        model_calls=deepcopy(model.calls[start_calls:]), source_calls=deepcopy(source.calls),
                        stage_timings=timings, total_seconds=monotonic()-started, state_unchanged=True)
                    captures[cid] = item; n.write(args.output/(cid+'.json'), item)
                    provider_blocked = any(c.get('status') in {401, 403} for c in item['model_calls'])
                    rows.append(dict(case_id=cid, status='ERROR' if error else 'PLAN' if result.plan else 'CLARIFICATION',
                        reason=error['reason'] if error else None, model_calls=len(item['model_calls'])))
                    n.write(args.output/'progress.json', dict(cases=rows, calls=model.calls))
                    print(n.json.dumps(rows[-1]), flush=True)
            assert n.hashes() == runtime_hashes and freeze_sources(args.evidence) == source_hashes and not denied
            n.write(args.output/'result.json', dict(cases=rows, calls=model.calls, runtime_hashes=runtime_hashes,
                model_calls=len(model.calls), embedding_calls=0, sql_execute=0, source_sql=0,
                production_state_writes=0, complete=True))
    finally: await model.upstream.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'evidence', 'recordings'): parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=20)
    parser.add_argument('--deadline', required=True)
    parser.add_argument('--allow-model-calls', action='store_true', required=True)
    asyncio.run(run(parser.parse_args()))
