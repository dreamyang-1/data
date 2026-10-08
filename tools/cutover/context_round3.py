"""Offline context arbitration diagnostics; never imported by production.

The A arm interprets EXISTING recorded model evidence, not an independent
context-aware model. The B arm uses the existing V1 rule gate for RELATION only;
canonical edits remain held fixed across arms. Neither is a replacement router.
No labels, case IDs, raw-history search, confidence weights or business words
are inputs to the model-evidence arm.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from unittest.mock import patch

from app.semantic_v2.pipeline import TurnResolver, TurnResolutionResult, TurnReferentialCompleteness
from app.semantic_v2.models import ProceedDecision, TerminalDecision
from app.semantic_v2.enums import DialogueAct


FOLLOW_ACTS = {'CONTINUE','MODIFY','ADD','REPLACE','REMOVE','CLEAR','CORRECT','DRILL_DOWN','REVISE','REFRESH'}


@dataclass(frozen=True)
class Attachment:
    relation: str
    target: str | None
    reason: str
    evidence: str


def active_task(state):
    topic = state.topics.get(state.active_topic_id)
    return topic.active_task_id if topic else None


def model_evidence(parse, state, historical_task_id=None):
    """Unweighted diagnostic reading of offered, recorded semantic evidence.

    Explicit conflicting NEW/follow-up propositions abstain. An active target
    selected through a historical handle is still the same attachment; act
    taxonomy is not used to invent a different task. This distinction is
    explicit in the diagnostics and never changes frozen Gold labels.
    """
    acts = set(parse.dialogue_act_candidates)
    new = bool(acts & {'NEW_TASK','SWITCH_TOPIC'} or parse.topic_shift_signals)
    follow = bool(acts & FOLLOW_ACTS or parse.followup_signals)
    historical = 'RETURN_TO_TOPIC' in acts or 'HISTORICAL' in parse.reference_signals
    referential = bool(parse.reference_signals)
    if new and (follow or historical or referential):
        return Attachment('UNRESOLVED',None,'CONFLICTING_MODEL_PROPOSITIONS','MODEL_UNRESOLVED')
    if new:
        return Attachment('NEW_TASK','task:'+parse.turn_id,'MODEL_NEW_TASK','LLM_SEMANTIC_EVIDENCE')
    if historical:
        if historical_task_id not in state.tasks:
            return Attachment('UNRESOLVED',None,'TARGET_NOT_OFFERED_OR_MISSING','HARD_CONSTRAINT')
        target = historical_task_id
        relation = 'FOLLOW_UP' if target == active_task(state) and follow and 'RETURN_TO_TOPIC' not in acts else 'RETURN_TO_TOPIC'
        return Attachment(relation,target,'MODEL_OFFERED_TARGET','LLM_SEMANTIC_EVIDENCE')
    if follow or referential:
        target = active_task(state)
        if target not in state.tasks:
            return Attachment('UNRESOLVED',None,'ACTIVE_TASK_MISSING','HARD_CONSTRAINT')
        return Attachment('FOLLOW_UP',target,'MODEL_FOLLOWUP_EVIDENCE','LLM_SEMANTIC_EVIDENCE')
    # Empty signals are not an explicit semantic decision. Do not credit a
    # rule default to the model. Scripted controls with empty acts exercise it.
    return Attachment('UNRESOLVED',None,'NO_MODEL_RELATION_PROPOSITION','MODEL_UNRESOLVED')


def rule_evidence(question, state, previous_question=None, *, as_of):
    """Existing deterministic V1 gate, no LLM relation signals or model score.

    A rule historical label does not identify a task. The gate itself has no
    multi-task selector, so this arm abstains instead of guessing recency.
    """
    from app.intent.classifier import RuleBasedIntentClassifier
    from app.domain.models import TrustedIdentity
    from app.services.turn_admission import TurnAdmissionGate
    from datetime import date, datetime
    clock=datetime.fromisoformat(as_of)
    if clock.tzinfo is None:raise ValueError('DIAGNOSTIC_CLOCK_MUST_BE_AWARE')
    class FrozenDate(date):
        @classmethod
        def today(cls):return cls(clock.year,clock.month,clock.day)
    identity=TrustedIdentity(tenant_id=state.tenant_id,user_id=state.user_id)
    classifier=RuleBasedIntentClassifier()
    with patch('app.intent.classifier.date',FrozenDate):
        current=classifier.classify(question,identity,state.conversation_id)
        previous=classifier.classify(previous_question,identity,state.conversation_id) if previous_question else None
    decision=TurnAdmissionGate().evaluate(question=question,current=current,previous=previous,
        message_id='diagnostic',pending=state.pending is not None)
    name=decision.relation.value
    reason='|'.join(decision.reason_codes)
    if name=='STANDALONE_NEW_TOPIC':return Attachment('NEW_TASK',None,reason,'LEGACY_RULE')
    if name=='HISTORICAL_TOPIC_RETURN':return Attachment('UNRESOLVED',None,'RULE_HAS_NO_TARGET_SELECTOR','LEGACY_RULE')
    if name=='AMBIGUOUS_RELATION':return Attachment('UNRESOLVED',None,reason,'LEGACY_RULE')
    target=state.pending.task_id if name=='CLARIFICATION_RESPONSE' and state.pending else active_task(state)
    if target not in state.tasks:return Attachment('UNRESOLVED',None,'RULE_TARGET_MISSING','HARD_CONSTRAINT')
    return Attachment('ANSWER_PENDING' if name=='CLARIFICATION_RESPONSE' else 'FOLLOW_UP',target,reason,'LEGACY_RULE')


def resolution_for(attachment, parse, state, task_patch, semantic_resolution):
    target=attachment.target
    if attachment.relation=='NEW_TASK':
        target='task:'+parse.turn_id;topic='topic:'+parse.turn_id;relation='SELF_CONTAINED';act='NEW_TASK'
    elif target in state.tasks:
        topic=state.tasks[target].topic_id
        relation='HISTORICAL_TASK' if attachment.relation=='RETURN_TO_TOPIC' else 'CURRENT_TASK'
        hints={m.operation_hint for m in parse.operation_markers}
        act=('RETURN_TO_TOPIC' if attachment.relation=='RETURN_TO_TOPIC' else
             'ANSWER_CLARIFICATION' if attachment.relation=='ANSWER_PENDING' else
             'ADD' if hints=={'ADD'} else 'REPLACE' if hints=={'REPLACE'} else 'MODIFY' if hints else 'CONTINUE')
    else:
        target=None;topic=None;relation='UNRESOLVED_REFERENCE';act='CONTINUE'
    return TurnResolutionResult(dialogue_act=act,target_topic_id=topic,target_task_id=target,
        referential_completeness=TurnReferentialCompleteness(relation=relation,depends_on_history=attachment.relation!='NEW_TASK'),
        task_patch=task_patch,semantic_resolution=semantic_resolution,
        decision=ProceedDecision() if target else TerminalDecision(reason_type='SEMANTIC_RESOLUTION_FAILURE'))


@contextmanager
def resolver_arm(arm, *, question='', previous_question=None, as_of=None):
    """Override ONLY the resolver seam. Scope/parse/patch/state validation stays.

    A/B are counterfactual diagnostics: changed context sent to a recorded
    semantic draft is recorded as such, never called a new model result.
    """
    if arm not in {'A_MODEL_HARD','B_RULE_ONLY','C_CURRENT_HYBRID','ORACLE_A_RELATION'}:
        raise ValueError('UNKNOWN_DIAGNOSTIC_ARM')
    original=TurnResolver.resolve;events=[]
    def resolve(parse, *, state, task_patch, semantic_resolution, historical_task_id=None):
        baseline=original(parse,state=state,task_patch=task_patch,semantic_resolution=semantic_resolution,
            historical_task_id=historical_task_id)
        evidence=model_evidence(parse,state,historical_task_id)
        if arm=='A_MODEL_HARD':
            final=resolution_for(evidence,parse,state,task_patch,semantic_resolution)
        elif arm=='B_RULE_ONLY':
            if as_of is None:raise ValueError('RULE_DIAGNOSTIC_CLOCK_REQUIRED')
            rule=rule_evidence(question,state,previous_question,as_of=as_of)
            final=resolution_for(rule,parse,state,task_patch,semantic_resolution)
        elif arm=='ORACLE_A_RELATION' and baseline.target_task_id==active_task(state):
            # Single output artifact; no target, patch, state or parse changes.
            final=baseline.model_copy(update={'dialogue_act':DialogueAct.MODIFY,
                'referential_completeness':baseline.referential_completeness.model_copy(update={'relation':'CURRENT_TASK'})})
        else:final=baseline
        events.append({'MODEL_RELATION':evidence.relation,'MODEL_TARGET_TASK':evidence.target,
            'MODEL_EVIDENCE_REASON':evidence.reason,'CURRENT_HYBRID_RELATION':str(baseline.dialogue_act),
            'CURRENT_HYBRID_TARGET_TASK':baseline.target_task_id,'FINAL_RELATION':str(final.dialogue_act),
            'FINAL_TARGET_TASK':final.target_task_id,'decision_source':evidence.evidence,
            'OVERRIDE_DIRECTION':'DIAGNOSTIC_ONLY' if arm!='C_CURRENT_HYBRID' else 'NONE',
            'OVERRIDE_REASON':arm,'DETERMINISTIC_CONSTRAINT':'NATIVE_SCOPED_RESTORE_AND_PATCH_VALIDATORS_RETAINED'})
        return final
    with patch.object(TurnResolver,'resolve',staticmethod(resolve)):yield events


def referential_slot_oracle(outputs, *, mention_id):
    """Reviewed parse-only experiment: a reference is not an edit obligation.

    The caller must certify that this mention is referential, not an omitted
    explicit request. No generic demotion heuristic is implemented here.
    """
    updated=deepcopy(outputs);parse=updated[0]['output']
    if any(m['mention_id']==mention_id for m in parse['operation_markers']):
        raise ValueError('ORACLE_CANNOT_DROP_EXPLICIT_OPERATION')
    touched=False
    for slot,ids in parse['explicit_slot_mentions'].items():
        if mention_id in ids:
            parse['explicit_slot_mentions'][slot]=[i for i in ids if i!=mention_id];touched=True
    if not touched:raise ValueError('ORACLE_REFERENCE_NOT_DECLARED')
    assert updated[1:]==outputs[1:]
    return updated
