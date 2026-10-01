"""Minimal no-op observer of the real V1 orchestrator entry.

This adapter neither reimplements V1 nor manufactures V2 internal contracts.
Unrecorded dependencies abort the isolated diagnostic, without triggering a
different rule fallback and calling that a production-comparable run.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from functools import wraps
import socket
from unittest.mock import patch

import httpx
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_observation import json_value

VERSION='v1-native-outcome-observer-v1'
COMMON_AXES=('task_relation','clarification_decision','metrics','dimensions','entity_types',
             'entity_values','filters','time_range','time_grain','query_shape',
             'authorized_scope','final_semantic_request','asl_semantics')

class FrozenDependencyMissing(BaseException):
    """Diagnostic control boundary; never a semantic or provider failure."""

@contextmanager
def frozen_dependencies_only():
    attempts=[]
    def stop(kind):
        def fail(*args,**kwargs):
            attempts.append(kind)
            raise FrozenDependencyMissing('FROZEN_V1_DEPENDENCY_RECORD_MISSING:'+kind)
        return fail
    async def async_stop(*args,**kwargs):return stop('HTTP')()
    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.AsyncClient,'send',async_stop))
        stack.enter_context(patch.object(httpx.Client,'send',stop('HTTP')))
        stack.enter_context(patch.object(socket.socket,'connect',stop('NETWORK')))
        stack.enter_context(patch.object(socket.socket,'connect_ex',stop('NETWORK')))
        import pymysql,redis
        stack.enter_context(patch.object(pymysql,'connect',stop('DATABASE')))
        stack.enter_context(patch.object(redis.Redis,'execute_command',stop('REDIS')))
        yield attempts

class V1OutcomeObserver:
    def __init__(self,orchestrator):
        from app.services.orchestrator import DataAnalysisOrchestrator
        if type(orchestrator) is not DataAnalysisOrchestrator:
            raise ValueError('REAL_V1_ORCHESTRATOR_REQUIRED')
        self.runtime=orchestrator;self.events=[];self.stack=ExitStack()

    def __enter__(self):
        def observe(obj,name,stage):
            original=getattr(obj,name)
            @wraps(original)
            async def wrapped(*args,**kwargs):
                event={'stage':stage,'inputs':json_value(args),'kwargs':json_value(kwargs)}
                self.events.append(event)
                try:
                    result=await original(*args,**kwargs)
                except BaseException as exc:
                    event['error_type']=type(exc).__name__;raise
                event['output']=json_value(result)
                return result
            self.stack.enter_context(patch.object(obj,name,wrapped))
        observe(self.runtime,'_request_clarification','CLARIFICATION_TERMINAL')
        observe(self.runtime.adapters.query,'query','FINAL_SEMANTIC_REQUEST')
        observe(self.runtime.sessions,'put_task_frame','TASK_FRAME_BEFORE_EXECUTION')
        observe(self.runtime,'_finish_terminal','FINISH_TERMINAL')
        return self

    def __exit__(self,*exc):self.stack.close()

    async def run(self,chat,identity):
        response=await self.runtime.handle(chat,identity)
        self.events.append({'stage':'AGENT_RESPONSE','output':json_value(response)})
        return response

    def outcome(self):
        axes={};sources={}
        for event in self.events:
            if event['stage']=='AGENT_RESPONSE':
                response=event['output']
                if response['clarification_questions'] or response['clarification_items']:
                    axes['clarification_decision']='ASK';sources['clarification_decision']='AGENT_RESPONSE'
                elif response['status']=='COMPLETED':
                    axes['clarification_decision']='NO_ASK';sources['clarification_decision']='AGENT_RESPONSE'
            if event['stage']=='FINAL_SEMANTIC_REQUEST':
                request=event['inputs'][0]
                axes['final_semantic_request']=request;sources['final_semantic_request']=event['stage']
                # Exact V1 output fields, not an inferred V2 TaskSemanticState.
                for axis,key in [('metrics','metrics'),('dimensions','dimensions'),('filters','filters'),
                                 ('time_range','time_range'),('authorized_scope','authorized_semantic_scope'),
                                 ('task_relation','turn_relation')]:
                    if key in request and request[key] is not None:
                        axes[axis]=deepcopy(request[key]);sources[axis]=event['stage']
        return {'adapter_version':VERSION,'runtime_entry':'DataAnalysisOrchestrator.handle',
                'axes':axes,'observation_sources':sources,
                'NOT_COMPARABLE_AXES':{k:'NO_RELIABLE_NATIVE_OBSERVATION' for k in COMMON_AXES if k not in axes},
                'events_hash':digest(self.events),
                'terminal_response_observed':any(e['stage']=='AGENT_RESPONSE' for e in self.events),
                'final_query_executed':False}

def compare_common(v1,v2,expected,*,same_input_manifest):
    """Only intersect independently labeled, observed outcome axes.

    Caller must supply the same semantic representation; native metric IDs or
    display strings are not auto-reinterpreted as canonical V2 bindings.
    """
    if not same_input_manifest:raise ValueError('COMMON_INPUT_MANIFEST_MISMATCH')
    if any(k not in COMMON_AXES for k in expected):raise ValueError('V2_INTERNAL_AXIS_NOT_COMMON')
    axes=sorted(set(expected)&set(v1)&set(v2))
    return {'comparable_axes':axes,'V1':{k:digest(v1[k])==digest(expected[k]) for k in axes},
            'V2':{k:digest(v2[k])==digest(expected[k]) for k in axes},
            'not_comparable_axes':sorted(set(expected)-set(axes)),
            'whole_outcome_comparable':bool(expected) and len(axes)==len(expected)}
