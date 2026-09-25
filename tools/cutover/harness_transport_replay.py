"""Replay captured untyped responses through the actual recognition client.

Only a verified MockTransport is allowed; sockets, databases and other HTTP
clients remain denied. Missing transport evidence is a gap, never a fake pass.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import TrustedIdentity
from app.semantic_v2.authorized_contract import ScopedArtifact
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.recognition_client import RecognitionModelClient
from tools.cutover.evaluation_contract import digest
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.harness_fixtures import request_for
from tools.cutover.harness_replay import deny_external_calls,compare_results
from tools.cutover.run_raw_transition_benchmark import frozen_publication,failure_reason


async def replay_transport_verified(capture,case,catalog,raw_snapshot,*,expected_capture_hash,
        request_receipts,source_observations=None,source_observations_hash=None):
    if digest(capture)!=expected_capture_hash or capture['format']!='PRIVATE_RAW_TRANSITION_V1':
        raise ValueError('RECORDED_CAPTURE_HASH_OR_FORMAT_INVALID')
    index=capture['turn_index'];utterances=[*case['history'],case['current_utterance']]
    if (type(index) is not int or not 0<=index<len(utterances) or capture['case_id']!=case['case_id']
            or capture['question']!=utterances[index] or capture['clock']!=case['clock']
            or capture['scope']!=catalog['scope'] or capture['catalog_ref']!=catalog['artifact_hash']
            or capture['source_observations_hash']!=source_observations_hash):
        raise ValueError('RECORDED_CAPTURE_CASE_OR_SCOPE_MISMATCH')
    exchanges=capture['exchanges']
    if not exchanges or len(exchanges)!=len(request_receipts):
        raise ValueError('FROZEN_TRANSPORT_RECEIPTS_REQUIRED')
    first=exchanges[0]['request_body'];position=0;checks=[];evidence_errors=[]
    def gap(reason):
        evidence_errors.append(reason)
        raise ValueError(reason)
    def recorded(request):
        nonlocal position
        if position>=len(exchanges):gap('FROZEN_TRANSPORT_EXHAUSTED')
        exchange=exchanges[position];receipt=request_receipts[position]
        body=json.loads(request.content)
        if body!=exchange['request_body']:gap('RECORDED_MODEL_INPUT_MISMATCH')
        checks.append({'stage':exchange['stage'],'request_body_equal':True});position+=1
        if receipt.get('transport_error_type')=='MODEL_TIMEOUT':
            raise httpx.ReadTimeout('recorded timeout',request=request)
        if receipt.get('transport_error_type')=='MODEL_HTTP_ERROR':
            raise httpx.ConnectError('recorded transport error',request=request)
        status=exchange.get('status')
        if type(status) is not int:gap('FROZEN_HTTP_STATUS_MISSING')
        if status!=200:return httpx.Response(status,json={'recorded_http_error':True})
        if 'finish_reason' not in receipt:gap('FROZEN_FINISH_REASON_MISSING')
        # Refusal itself was not captured by the old recorder. Such a response
        # cannot be reconstructed by silently replacing refusal with false.
        if capture['outcome'].get('reason')=='V2_MODEL_OUTPUT_INCOMPLETE' and receipt['finish_reason'] in (None,'stop'):
            gap('FROZEN_REFUSAL_EVIDENCE_MISSING')
        return httpx.Response(status,json={'choices':[{'finish_reason':receipt['finish_reason'],
            'message':{'content':exchange['raw_content']}}]})
    transport=httpx.MockTransport(recorded)
    settings=Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('isolated-replay-token'),
        intent_model_base_url='https://replay.invalid',intent_model_name=first['model'],
        intent_model_enable_thinking=first['enable_thinking'],intent_model_max_retries=0,
        intent_model_response_format=first['response_format']['type'])
    before=capture['before'];artifacts=[v for v in (before['state'],before['pending'],*before['plans']) if v]
    if capture.get('result'):artifacts.append(capture['result']['next_state'])
    activations={v['context']['catalog_pin']['activation_id'] for v in artifacts}
    if len(activations)>1:raise ValueError('RECORDED_STATE_PUBLICATION_MISMATCH')
    root=Path(__file__).resolve().parents[2]
    oagnet=root/'Oagnet' if (root/'Oagnet').is_dir() else root.parent/'Oagnet'
    result=None;failure=None
    with deny_external_calls(allowed_mock_transport=transport) as counters,ExitStack() as stack:
        stack.enter_context(patch.object(sys,'path',[*sys.path,str(oagnet),str(oagnet/'tests')]))
        publication=frozen_publication(raw_snapshot,catalog,[],native_source_values=source_observations is not None,
            recorded_activation_id=next(iter(activations),None))
        if source_observations is not None:
            values=FrozenSourceValues(source_observations,catalog=catalog,snapshot=json.loads(raw_snapshot),
                expected_hash=source_observations_hash,allow_synthetic=capture['mode']=='SCRIPTED_MODEL_PIPELINE')
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',values.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',values.observe_probe))
        model=RecognitionModelClient(settings,transport=transport)
        engine=RawTurnPlanner(model,publication,clock=lambda:datetime.fromisoformat(case['clock']))
        request=request_for({**case,'current_utterance':capture['question']},message_id='turn-'+str(index))
        try:
            result=await engine.run(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
                state=ScopedArtifact.model_validate(before['state']) if before['state'] else None,
                pending=ScopedArtifact.model_validate(before['pending']) if before['pending'] else None,
                plans=tuple(ScopedArtifact.model_validate(v) for v in before['plans']))
        except Exception as exc:
            failure={'type':type(exc).__name__,'reason':failure_reason(exc)}
    if evidence_errors:raise ValueError(evidence_errors[0])
    if position!=len(exchanges):raise ValueError('FROZEN_RESPONSE_REPLAY_STOPPED_EARLY')
    return {'case_id':case['case_id'],'turn_index':index,'mode':'RECORDED_TRANSPORT_PIPELINE',
        'source_capture_hash':expected_capture_hash,'failure':failure,'model_stages':checks,
        'external_call_attempts':counters,'real_model_calls':0,'source_SQL_executions':0,
        'production_writes':0,'production_state_mutations':0,
        'equality':compare_results(capture.get('result'),result.model_dump(mode='json') if result else None)},result
