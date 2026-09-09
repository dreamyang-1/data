"""Read-only isolated V1-entry coverage probe; no fabricated dependency replies."""
from contextlib import ExitStack
from datetime import datetime,date
import json
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.adapters import build_http_adapters
from app.domain.models import ChatRequest,TrustedIdentity
from app.intent import HybridIntentClassifier
from app.planning import MultiQuestionPlanner
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.question_rewriter import HttpEntityAttributeSearcher,QuestionRewriter
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore
from tools.cutover.calibration_v1 import V1OutcomeObserver,FrozenDependencyMissing,frozen_dependencies_only
from tools.cutover.evaluation_contract import digest

class FixedDate(date):
    @classmethod
    def today(cls):return cls(2026,9,9)

class FixedDatetime(datetime):
    @classmethod
    def now(cls,tz=None):
        value=cls(2026,9,9,9,tzinfo=ZoneInfo('Asia/Shanghai'))
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

async def probe(cases,settings,*,private_directory):
    if settings.adapter_mode!='http':raise ValueError('CURRENT_V1_HTTP_CONFIGURATION_REQUIRED')
    if any(c.get('split')=='BLIND_HOLDOUT' for c in cases):raise ValueError('HOLDOUT_FORBIDDEN')
    directory=Path(private_directory);directory.mkdir(parents=True,exist_ok=False)
    receipts=[]
    for case in cases:
        if case['entry_group']!='TRUE_SINGLE_TURN':
            receipts.append({'case_id':case['case_id'],'status':'NOT_COMPARABLE',
                'reason':'FROZEN_V1_NATIVE_HISTORY_OR_PENDING_DATASET_RECEIPT_REQUIRED',
                'runtime_called':False,'observed_axes':[]});continue
        rewriter=QuestionRewriter(HttpEntityAttributeSearcher(
            base_url=settings.asl_generator_base_url,path=settings.entity_attribute_search_path,
            timeout_seconds=settings.question_rewrite_timeout_seconds,top_k=settings.question_rewrite_top_k,
            score_threshold=settings.question_rewrite_search_threshold,
            display_resolve_path=settings.semantic_display_resolve_path),
            auto_replace_threshold=settings.question_rewrite_auto_threshold,candidate_gap=settings.question_rewrite_candidate_gap,
            typo_similarity_threshold=settings.question_rewrite_typo_threshold) if settings.question_rewrite_enabled else QuestionRewriter(None)
        runtime=DataAnalysisOrchestrator(settings,HybridIntentClassifier(settings),build_http_adapters(settings),
            InMemorySessionStore(),event_store=InMemorySessionEventStore(),question_rewriter=rewriter,
            task_planner=MultiQuestionPlanner(settings))
        observer=V1OutcomeObserver(runtime)
        chat=ChatRequest(question=case['current_utterance'],conversation_id='calibration-'+case['case_id'],application_id='cutover-evaluation',
            message_id='turn-0',semantic_model_id=81,business_domain_ids=[205])
        status='OBSERVED';reason=None
        with ExitStack() as stack:
            stack.enter_context(patch('app.intent.classifier.date',FixedDate))
            stack.enter_context(patch('app.intent.classifier.datetime',FixedDatetime))
            stack.enter_context(patch('app.intent.structured.datetime',FixedDatetime))
            attempts=stack.enter_context(frozen_dependencies_only())
            stack.enter_context(observer)
            try:await observer.run(chat,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'))
            except FrozenDependencyMissing as exc:status='BLOCKED';reason=str(exc)
            except Exception as exc:status='DIAGNOSTIC_ERROR';reason=type(exc).__name__
        outcome=observer.outcome()
        (directory/(case['case_id']+'.json')).write_text(json.dumps({'outcome':outcome,'events':observer.events},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        receipts.append({'case_id':case['case_id'],'status':status,'reason':reason,'runtime_called':True,
            'observed_axes':list(outcome['axes']),'terminal_response_observed':outcome['terminal_response_observed'],
            'blocked_dependency_attempts':attempts,'events_hash':digest(observer.events),
            'scope_request_fingerprint':chat.authorized_semantic_scope.fingerprint(),
            'as_of':'2026-09-09T09:00:00+08:00','external_calls':0,'production_state_writes':0})
    return receipts
