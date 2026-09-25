"""Opt-in model evaluation of RawTurnPlanner on a verified frozen catalog.

Uses actual publication/pin/planner/reducer code with existing in-memory test
stores. No native publication, source SQL, production state or V1 response path.
Pending/Dataset fixtures not implemented here stay NOT_RUN with fixed denominators.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout, redirect_stderr, ExitStack
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.semantic_v2.recognition import RawTurnPlanner, PROMPT_VERSION, PARSE_PROMPT, DRAFT_PROMPT, current_turn_schema
from app.semantic_v2.recognition_client import RecognitionModelClient, RecognitionFailure
from app.semantic_v2.authorized_contract import ScopedArtifact
from tools.cutover.build_evaluation_gold import freeze
from tools.cutover.evaluation_contract import digest
from tools.cutover.semantic_evaluator import read_jsonl
from tools.cutover.transition_evaluator import evaluate_transitions, validate_transitions
from tools.cutover.transition_observations import observe_v2_plan
from tools.cutover.frozen_source_values import FrozenSourceValues, SourceValuesUnavailable

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = 'V2_RAW_TURN_PLANNER'
MODEL_NETWORK = ContextVar('cutover_model_transport_network', default=False)


class ModelBudgetExhausted(RuntimeError):
    pass


@contextmanager
def network_guard():
    """Enter inside an initialized event loop (Windows owns a socketpair)."""
    def guarded(real):
        def call(*args, **kwargs):
            if not MODEL_NETWORK.get():
                raise RuntimeError('NON_MODEL_NETWORK_DENIED')
            return real(*args, **kwargs)
        return call
    with patch.object(socket.socket, 'connect', guarded(socket.socket.connect)), \
            patch.object(socket.socket, 'connect_ex', guarded(socket.socket.connect_ex)), \
            patch.object(socket, 'create_connection', guarded(socket.create_connection)), \
            patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED':'1', 'LANGFUSE_TRACING_ENABLED':'false'}):
        yield


def frozen_publication(raw_snapshot, catalog, denied_source_reads, *, native_source_values=False, recorded_activation_id=None):
    snapshot = json.loads(raw_snapshot)
    if freeze(snapshot, hashlib.sha256(raw_snapshot).hexdigest(), catalog['observed_at']) != catalog:
        raise ValueError('FROZEN_SNAPSHOT_PROJECTION_MISMATCH')
    oagnet = ROOT/'Oagnet' if (ROOT/'Oagnet').is_dir() else ROOT.parent/'Oagnet'
    with patch.object(sys, 'path', [*sys.path, str(oagnet), str(oagnet/'tests')]), \
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        from test_catalog_publication import system
        publication, _, _, _, _, _ = system()
    scope = catalog['scope']
    def capture(model, domains):
        if model != scope['semantic_model_id'] or list(domains) != scope['business_domain_ids']:
            raise ValueError('FROZEN_SCOPE_MISMATCH')
        return deepcopy(snapshot)
    publication.capture = capture
    with ExitStack() as stack:
        if recorded_activation_id is not None:
            if not isinstance(recorded_activation_id, str) or UUID(recorded_activation_id).hex != recorded_activation_id:
                raise ValueError('RECORDED_ACTIVATION_ID_INVALID')
            import catalog_registry
            # Only the fresh MemoryStore/FakeRedis above. Reproduce the captured
            # evaluation identity; never rewrite restored state or bypass scope.
            stack.enter_context(patch.object(catalog_registry, 'uuid',
                SimpleNamespace(uuid4=lambda:SimpleNamespace(hex=recorded_activation_id))))
        publication.publish(scope['semantic_model_id'], scope['business_domain_ids'],
            embed_fn=lambda texts: [[0.1, 0.2] for _ in texts],
            publication_id='isolated-frozen-evaluation-81-205', producer_revision='offline-evaluation',
            embedding_contract='offline-static-vector-2', expected_catalog_version=catalog['catalog_version'])
    native_pin = publication.pin
    def deny_source(*args, **kwargs):
        denied_source_reads.append(True)
        raise SourceValuesUnavailable('FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED')
    def pin(*args, **kwargs):
        result = native_pin(*args, **kwargs)
        result.lookup_entity_values = deny_source
        result.probe_entity_values = deny_source
        return result
    if not native_source_values:
        publication.pin = pin
    return publication


def schema_diagnostics(exc, output_model):
    """No model values, error messages or arbitrary dynamic field names."""
    known = set()
    def visit(value):
        if isinstance(value, dict):
            known.update(value.get('properties', {}))
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(output_model.model_json_schema())
    return [{'type':error['type'], 'location':[part if isinstance(part, int) or part in known else '<unknown-field>' for part in error['loc']]}
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:20]]


class ModelRecorder:
    def __init__(self, settings, *, transport=None, max_calls=100, capture_exchanges=False):
        self.settings = settings.model_copy(update={'intent_model_max_retries':0})
        self.calls = []
        self.active = {}
        self.outputs = []
        self.exchanges = []
        self.capture_exchanges = capture_exchanges
        self.last_content = None
        self.last_schema = None
        self.max_calls = max_calls
        self.live_transport = transport is None
        self.upstream = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0)
        owner = self
        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if len(owner.calls) >= owner.max_calls:
                    raise ModelBudgetExhausted('MODEL_REQUEST_BUDGET_EXHAUSTED')
                body = json.loads(request.content)
                if (str(request.url) != owner.settings.intent_model_base_url.rstrip('/')+'/chat/completions'
                        or body.get('model') != owner.settings.intent_model_name):
                    raise ValueError('MODEL_TRANSPORT_TARGET_MISMATCH')
                receipt = {**owner.active, 'input_hash':digest(body['messages']), 'status':None}
                owner.calls.append(receipt)
                exchange = None
                if owner.capture_exchanges:
                    # The HTTP body contains the exact prompt/candidates/schema;
                    # headers, credentials and service URLs are never captured.
                    exchange = {'stage':owner.active['stage'], 'request_body':deepcopy(body),
                        'status':None, 'raw_content':None}
                    owner.exchanges.append(exchange)
                token = MODEL_NETWORK.set(owner.live_transport)
                try:
                    response = await owner.upstream.handle_async_request(request)
                    await response.aread()
                    receipt['status'] = response.status_code
                    if exchange is not None: exchange['status'] = response.status_code
                    try:
                        payload = response.json()
                        if not isinstance(payload, dict): return response
                        receipt['returned_model'] = payload.get('model')
                        receipt['usage'] = {k:v for k,v in (payload.get('usage') or {}).items()
                            if k in {'prompt_tokens','completion_tokens','total_tokens'} and type(v) is int}
                        choices = payload.get('choices')
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            message = choices[0].get('message')
                            if isinstance(message, dict):
                                content = message.get('content')
                                if isinstance(content, str) and len(content) <= 128_000:
                                    owner.last_content = content
                                    if exchange is not None: exchange['raw_content'] = content
                            receipt['finish_reason'] = choices[0].get('finish_reason')
                    except (ValueError, TypeError, AttributeError):
                        receipt['receipt_decode_failed'] = True
                    return response
                except httpx.TimeoutException:
                    receipt['transport_error_type'] = 'MODEL_TIMEOUT'
                    raise
                except httpx.HTTPError:
                    receipt['transport_error_type'] = 'MODEL_HTTP_ERROR'
                    raise
                finally:
                    MODEL_NETWORK.reset(token)
            async def aclose(self): pass
        self.client = RecognitionModelClient(self.settings, transport=Transport())

    def begin_turn(self, case_id, index):
        self.active = {'case_id':case_id, 'turn_index':index, 'stage':'ENTRY'}
        self.outputs = []
        self.exchanges = []
        self.last_content = None
        self.last_schema = None

    async def complete(self, **kwargs):
        self.active['stage'] = kwargs['stage']
        self.active['schema_hash'] = digest(kwargs.get('schema') or kwargs['output_model'].model_json_schema())
        self.last_schema = kwargs['output_model']
        self.last_content = None
        try:
            result = await self.client.complete(**kwargs)
        except RecognitionFailure:
            if self.last_content is not None:
                try:
                    kwargs['output_model'].model_validate_json(self.last_content)
                except ValidationError as exc:
                    self.calls[-1]['schema_diagnostics'] = schema_diagnostics(exc, kwargs['output_model'])
            raise
        self.outputs.append({'stage':kwargs['stage'], 'output':result.model_dump(mode='json')})
        return result


def failure_reason(exc):
    if isinstance(exc, SourceValuesUnavailable): return 'FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED'
    if isinstance(exc, ModelBudgetExhausted): return 'MODEL_REQUEST_BUDGET_EXHAUSTED'
    if isinstance(exc, RecognitionFailure): return str(exc)
    return 'CONTRACT_OR_HARNESS_FAILURE'


async def replay_turn(capture, case, catalog, raw_snapshot, *, expected_capture_hash,
        oracle_outputs=None, source_observations=None, source_observations_hash=None, case_validator=None,
        runtime_entry=False):
    """Replay one recorded turn in the same isolated pipeline, with no model HTTP.

    An Oracle intervention replaces only supplied model outputs; subsequent
    outputs remain recorded, not regenerated under a changed model context. This
    diagnoses contract/binding/state boundaries, never model or cutover accuracy.
    The caller owns the trusted private capture/hash and independently reviewed
    Oracle outputs. Model output cannot supply either authority input.
    """
    (case_validator or validate_transitions)([case], catalog)
    if capture.get('format') != 'PRIVATE_RAW_TRANSITION_V1' or digest(capture) != expected_capture_hash:
        raise ValueError('RECORDED_CAPTURE_HASH_OR_FORMAT_INVALID')
    index = capture['turn_index'];utterances = [*case['history'], case['current_utterance']]
    if (type(index) is not int or not 0 <= index < len(utterances) or capture['case_id'] != case['case_id']
            or capture['question'] != utterances[index] or capture['clock'] != case['clock']
            or capture['scope'] != catalog['scope'] or capture['catalog_ref'] != catalog['artifact_hash']
            or capture['source_observations_hash'] != source_observations_hash):
        raise ValueError('RECORDED_CAPTURE_CASE_OR_SCOPE_MISMATCH')
    if (source_observations is None) != (source_observations_hash is None):
        raise ValueError('FROZEN_VALUES_AND_EXPECTED_HASH_REQUIRED_TOGETHER')
    outputs = deepcopy(capture['outputs'] if oracle_outputs is None else oracle_outputs)
    if not isinstance(outputs, list) or len(outputs) != len(capture['outputs']):
        raise ValueError('ORACLE_OUTPUT_STAGE_COVERAGE_INVALID')
    # Incomplete/untyped model responses need transport replay, not this typed seam.
    if len(capture['outputs']) != len(capture['exchanges']):
        raise ValueError('RECORDED_TYPED_OUTPUTS_INCOMPLETE')
    mode = 'RECORDED_MODEL_PIPELINE' if oracle_outputs is None else 'RECORDED_OUTPUT_ORACLE_INTERVENTION'
    checks, denied = [], []
    class Recorded:
        async def complete(self, **kwargs):
            position = len(checks)
            if position >= len(outputs): raise ValueError('RECORDED_OUTPUT_STAGE_EXHAUSTED')
            original = capture['exchanges'][position]
            messages = original['request_body']['messages']
            context = json.loads(messages[1]['content'])
            matches = context == kwargs['context']
            if (outputs[position]['stage'] != kwargs['stage'] or original['stage'] != kwargs['stage']
                    or (not matches and oracle_outputs is None)):
                raise ValueError('RECORDED_MODEL_CONTEXT_OR_STAGE_MISMATCH')
            # Prompt/schema changes cannot hide inside an unmodified replay.
            expected_system = kwargs['instruction'] + '\nJSON Schema:\n' + json.dumps(
                kwargs.get('schema') or kwargs['output_model'].model_json_schema(), ensure_ascii=False)
            if oracle_outputs is None and messages[0]['content'] != expected_system:
                raise ValueError('RECORDED_PROMPT_OR_SCHEMA_MISMATCH')
            checks.append({'stage':kwargs['stage'], 'context_matches_recording':matches,
                'output_changed':outputs[position] != capture['outputs'][position]})
            return kwargs['output_model'].model_validate(outputs[position]['output'])
    before = capture['before']
    artifacts = [v for v in (before['state'], before['pending'], *before['plans']) if v is not None]
    if capture.get('result') is not None:
        artifacts.append(capture['result']['next_state'])
    activations = {v['context']['catalog_pin']['activation_id'] for v in artifacts}
    if len(activations) > 1: raise ValueError('RECORDED_STATE_PUBLICATION_MISMATCH')
    activation = next(iter(activations), None)
    result = None;failure = None;replay = None
    oagnet = ROOT/'Oagnet' if (ROOT/'Oagnet').is_dir() else ROOT.parent/'Oagnet'
    with network_guard(), patch.object(sys, 'path', [*sys.path, str(oagnet), str(oagnet/'tests')]), ExitStack() as stack:
        publication = frozen_publication(raw_snapshot, catalog, denied,
            native_source_values=source_observations is not None, recorded_activation_id=activation)
        if source_observations is not None:
            replay = FrozenSourceValues(source_observations, catalog=catalog, snapshot=json.loads(raw_snapshot),
                expected_hash=source_observations_hash, allow_synthetic=capture['mode']=='SCRIPTED_MODEL_PIPELINE')
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources, 'observe', replay.observe))
            stack.enter_context(patch.object(catalog_value_sources, 'observe_probe', replay.observe_probe))
        engine = RawTurnPlanner(Recorded(), publication, clock=lambda:datetime.fromisoformat(capture['clock']))
        request = ChatRequest(semantic_model_id=case['scope']['semantic_model_id'],business_domain_ids=case['scope']['business_domain_ids'],
            database_id=case.get('database_id'),knowledge_base_names=case.get('knowledge_base_names',[]),
            application_id='isolated-evaluation',conversation_id=case['case_id'],message_id=f'turn-{index}',question=capture['question'])
        try:
            # Same computation as run(), exposing the original ValidationError
            # before the normal bounded RecognitionFailure wrapper loses it.
            entry=engine.run if runtime_entry else engine._run
            result = await entry(request, TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
                state=ScopedArtifact.model_validate(before['state']) if before['state'] else None,
                pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None,
                plans=tuple(ScopedArtifact.model_validate(p) for p in before['plans']))
        except Exception as exc:
            failure = {'type':type(exc).__name__, 'reason':failure_reason(exc)}
            if isinstance(exc, ValidationError):
                from app.semantic_v2.models import PlanEnvelope
                failure['schema_diagnostics'] = schema_diagnostics(exc, PlanEnvelope)
            known = {'_patch':'TASK_PATCH','materialize_payload':'SEMANTIC_IR','_check_roles':'BINDING_ROLE',
                'restore':'STATE_RESTORE','lookup_values':'SOURCE_VALUE_BINDING',
                'resolve_requests':'SOURCE_VALUE_RESOLUTION','_hydrate':'CANONICAL_BINDING',
                'complete':'RECORDED_MODEL_INPUT'}
            frame = exc.__traceback__;boundary = 'RAW_TURN_CONTRACT'
            while frame:
                if frame.tb_frame.f_code.co_name in known: boundary = known[frame.tb_frame.f_code.co_name]
                frame = frame.tb_next
            failure['first_rejecting_boundary'] = boundary
    receipt = {'case_id':case['case_id'],'turn_index':index,'mode':mode,'source_capture_hash':expected_capture_hash,
        'oracle_outputs_hash':digest(oracle_outputs) if oracle_outputs is not None else None,
        'model_stages':checks,'failure':failure,'real_model_calls':0,'source_SQL_executions':0,'production_writes':0,
        'state_restore_policy':'REPRODUCE_ISOLATED_PUBLICATION_IDENTITY; ORIGINAL_SCOPE_AND_DIGEST_CHECKS',
        'source_observations_replayed':sum(c['status']=='REPLAYED' for c in replay.calls) if replay else 0,
        'model_accuracy_established':False,'production_cutover_pass':False}
    return receipt, result


async def run(rows, catalog, raw_snapshot, settings, *, allow_model_calls=False,
        transport=None, max_calls=100, private_capture=None, progress=None,
        source_observations=None, source_observations_hash=None, harness=None):
    (harness.validate_rows if harness is not None else validate_transitions)(rows, catalog)
    if not rows:
        raise ValueError('EMPTY_TRANSITION_CORPUS')
    if transport is not None and not isinstance(transport, httpx.MockTransport):
        raise ValueError('SCRIPTED_TRANSPORT_MUST_BE_MOCK')
    if transport is None and not allow_model_calls:
        raise ValueError('EXPLICIT_MODEL_CALL_OPT_IN_REQUIRED')
    if type(max_calls) is not int or not 1 <= max_calls <= 200:
        raise ValueError('MODEL_REQUEST_BUDGET_INVALID')
    if (source_observations is None) != (source_observations_hash is None):
        raise ValueError('FROZEN_VALUES_AND_EXPECTED_HASH_REQUIRED_TOGETHER')
    mode = 'LIVE_MODEL_PLAN_ONLY' if transport is None else 'SCRIPTED_MODEL_PIPELINE'
    predictions, records, denied = [], [], []
    captured_turns = 0
    replay = None
    model = ModelRecorder(settings, transport=transport, max_calls=max_calls, capture_exchanges=private_capture is not None)
    try:
        oagnet = ROOT/'Oagnet' if (ROOT/'Oagnet').is_dir() else ROOT.parent/'Oagnet'
        with network_guard(), patch.object(sys, 'path', [*sys.path, str(oagnet), str(oagnet/'tests')]), ExitStack() as stack:
            publication = frozen_publication(raw_snapshot, catalog, denied, native_source_values=source_observations is not None)
            if source_observations is not None:
                replay = FrozenSourceValues(source_observations, catalog=catalog, snapshot=json.loads(raw_snapshot),
                    expected_hash=source_observations_hash, allow_synthetic=transport is not None)
                import catalog_value_sources
                stack.enter_context(patch.object(catalog_value_sources, 'observe', replay.observe))
                stack.enter_context(patch.object(catalog_value_sources, 'observe_probe', replay.observe_probe))
            for row in rows:
                record_start, call_start = len(records), len(model.calls)
                # Allowlist execution inputs. New Gold metadata and truth must
                # never become a model context through a growing exclusion list.
                case = {k:v for k,v in row.items() if k in {'case_id','history','current_utterance',
                    'scope','catalog_ref','clock','database_id','knowledge_base_names','initial_pending','dataset'}}
                prediction = {'case_id':case['case_id'], 'scope':catalog['scope'], 'catalog_ref':catalog['artifact_hash'],
                    'component':COMPONENT, 'mode':mode, 'status':'NOT_RUN', 'axes':{}, 'safety':{}}
                predictions.append(prediction)
                def checkpoint():
                    if progress is not None:
                        progress(deepcopy({'prediction':prediction,'turns':records[record_start:],
                            'model_calls':model.calls[call_start:]}))
                if harness is None and (case.get('initial_pending') or case.get('dataset') is not None):
                    prediction['reason'] = 'FULL_ENTRY_PENDING_OR_DATASET_FIXTURE_REQUIRED'
                    checkpoint()
                    continue
                if len(model.calls) >= max_calls or any(c['status'] in {401,403} for c in model.calls):
                    prediction['reason'] = 'MODEL_BUDGET_OR_ACCESS_UNAVAILABLE'
                    checkpoint()
                    continue
                planner = RawTurnPlanner(model, publication, clock=lambda:datetime.fromisoformat(case['clock']))
                state, pending, plans, history = None, None, {}, []
                utterances = [*case['history'], case['current_utterance']]
                if harness is not None:
                    try:
                        initial = harness.initialize(case, publication)
                    except Exception as exc:
                        prediction.update(status='FAILED',reason=harness.fixture_failure(case,exc))
                        checkpoint()
                        continue
                    if initial is not None:
                        state,pending,plans,history = initial
                        utterances = [case['current_utterance']]
                for index, text in enumerate(utterances):
                    logical_index = index if len(utterances)>1 or not history else len(case['history'])
                    index = logical_index
                    model.begin_turn(case['case_id'], index)
                    if harness is not None:
                        harness.begin_turn(case['case_id'],index,state=state,pending=pending,plans=tuple(plans.values()))
                    record = {'case_id':case['case_id'], 'turn_index':index, 'phase':'HISTORY' if index < len(case['history']) else 'CURRENT'}
                    records.append(record)
                    result = None
                    before = None
                    if private_capture is not None:
                        before = {'state':state.model_dump(mode='json') if state else None,
                            'pending':pending.model_dump(mode='json') if pending else None,
                            'plans':[p.model_dump(mode='json') for p in plans.values()]}
                    try:
                        req = ChatRequest(**{k:case['scope'][k] for k in ('semantic_model_id','business_domain_ids')},
                            database_id=case.get('database_id'), knowledge_base_names=case.get('knowledge_base_names',[]),
                            application_id='isolated-evaluation', conversation_id=case['case_id'], message_id=f'turn-{index}', question=text)
                        result = await planner.run(req, TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
                            state=state, plans=tuple(plans.values()), pending=pending)
                        record['result_type'] = type(result).__name__
                        observation = observe_v2_plan(result, case, catalog=catalog, mode=mode,
                            previous=history[-1] if history else None, history=history)
                        record['observation'] = observation
                        if index < len(case['history']) and result.plan is None:
                            prediction.update(status='FAILED', reason='HISTORY_PRECONDITION_NOT_RESOLVED')
                            break
                        if index == len(case['history']): prediction.update(observation)
                        state = result.next_state
                        if result.plan is not None:
                            plans[result.plan['logical_plan']['task_id']] = result.plan_state
                            pending = None
                        else: pending = result.pending_state
                        history.append(result)
                    except Exception as exc:
                        record.update(error_type=type(exc).__name__, reason=failure_reason(exc), last_model_stage=model.active['stage'])
                        if isinstance(exc, ValidationError) and model.last_schema:
                            record['schema_diagnostics'] = schema_diagnostics(exc, model.last_schema)
                        prediction.update(status='FAILED', reason='HISTORY_PRECONDITION_FAILED' if record['phase']=='HISTORY' else record['reason'])
                        break
                    finally:
                        if private_capture is not None:
                            capture = {'format':'PRIVATE_RAW_TRANSITION_V1', 'case_id':case['case_id'], 'turn_index':index,
                                'stage':model.active['stage'], 'phase':record['phase'], 'mode':mode,
                                'scope':catalog['scope'], 'catalog_ref':catalog['artifact_hash'],
                                'source_observations_hash':source_observations_hash, 'clock':case['clock'],
                                'question':text, 'before':before, 'exchanges':deepcopy(model.exchanges),
                                'outputs':deepcopy(model.outputs), 'last_content':model.last_content,
                                'result':result.model_dump(mode='json') if result is not None else None,
                                'outcome':deepcopy(record)}
                            record['private_capture_hash'] = digest(capture)
                            private_capture(capture)
                            captured_turns += 1
                checkpoint()
    finally:
        await model.upstream.aclose()
    report = (harness.build_report(rows,predictions,catalog,records,model.calls,mode) if harness is not None else
              evaluate_transitions(rows, predictions, catalog, component=COMPONENT, mode=mode))
    report.update(model=settings.intent_model_name, thinking=settings.intent_model_enable_thinking,
        real_model_calls=len(model.calls) if transport is None else 0, model_transport_requests=len(model.calls),
        source_value_reads_denied=len(denied) + (sum(c['status']=='MISSING' for c in replay.calls) if replay else 0),
        source_value_observations_replayed=sum(c['status']=='REPLAYED' for c in replay.calls) if replay else 0,
        source_observations_hash=source_observations_hash,
        source_value_evidence_mode='FROZEN_READ_ONLY_CAPTURE' if replay and replay.capture_mode=='READ_ONLY_SOURCE'
            else 'SYNTHETIC_TEST' if replay else 'UNAVAILABLE',
        source_SQL_executions=0, production_external_writes=0,
        catalog_storage_mode='FROZEN_CATALOG_IN_MEMORY_PUBLICATION', query_vector_quality_evaluated=False,
        prompt_version=PROMPT_VERSION, current_turn_prompt_hash=digest(PARSE_PROMPT),
        current_turn_schema_hash=digest(current_turn_schema()), semantic_edit_prompt_hash=digest(DRAFT_PROMPT),
        history_policy='STOP_ON_UNRESOLVED_OR_FAILED_HISTORY; NO_SUCCESS_INJECTION',
        network_policy='ONLY_VALIDATED_MODEL_TRANSPORT_CAN_CONNECT', max_model_requests=max_calls,
        private_diagnostics_enabled=private_capture is not None,
        private_capture_turns=captured_turns, observed_turns=len(records),
        observation_counts=dict(__import__('collections').Counter(p['status'] for p in predictions)))
    return {'predictions':predictions, 'evaluation':report, 'turns':records, 'model_calls':model.calls}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('gold','catalog','snapshot','output-dir'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--case-ids')
    parser.add_argument('--max-model-requests',type=int,default=100)
    parser.add_argument('--private-diagnostics-dir',type=Path)
    parser.add_argument('--source-observations',type=Path)
    parser.add_argument('--source-observations-hash')
    parser.add_argument('--allow-model-calls',action='store_true',required=True)
    args = parser.parse_args()
    rows = read_jsonl(args.gold)
    if args.case_ids:
        selected = set(args.case_ids.split(','))
        rows = [r for r in rows if r['case_id'] in selected]
        if {r['case_id'] for r in rows} != selected: parser.error('Unknown case ID')
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    # Refuse output replacement before any model calls.
    args.output_dir.mkdir(parents=True,exist_ok=False)
    capture = None
    if args.private_diagnostics_dir:
        args.private_diagnostics_dir.mkdir(parents=True,exist_ok=False)
        def capture(value):
            # Filename never uses user-controlled IDs. Raw model output is private.
            path = args.private_diagnostics_dir/(digest([value['case_id'],value['turn_index']])+'.json')
            with path.open('x',encoding='utf-8') as f: json.dump(value,f,ensure_ascii=False,indent=2)
    def progress(value):
        with (args.output_dir/'progress.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(value,ensure_ascii=False)+'\n')
        print(json.dumps({'case_id':value['prediction']['case_id'],'status':value['prediction']['status'],
            'model_requests':len(value['model_calls'])}),flush=True)
    result = asyncio.run(run(rows,catalog,args.snapshot.read_bytes(),Settings(),allow_model_calls=args.allow_model_calls,
        max_calls=args.max_model_requests,private_capture=capture,progress=progress,
        source_observations=json.loads(args.source_observations.read_text(encoding='utf-8')) if args.source_observations else None,
        source_observations_hash=args.source_observations_hash))
    for name,value in result.items():
        with (args.output_dir/(name+'.json')).open('x',encoding='utf-8') as f: json.dump(value,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result['evaluation'].items() if k!='details'},ensure_ascii=False))
