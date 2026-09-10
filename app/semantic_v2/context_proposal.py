"""Scope-bound candidate exposure and hard validation for raw V2 recognition."""
from copy import deepcopy
from dataclasses import dataclass, replace
import json

from .authorized_contract import contract_digest
from .pipeline import AuthorizedLogicalPlan, CurrentTurnSemanticParse
from .state_machine import ConversationState
from .structured_edits import _labels
from .pending_recognition import selected_option
from .context_contract import ContextProposal
from .recognition_client import RecognitionFailure

# Bounded exposure policy, not a calibrated semantic ranking.
MAX_CONTEXT_TASK_CANDIDATES = 4
MAX_CONTEXT_CHARACTERS = 24000


class ContextProposalFailure(RecognitionFailure):
    """Terminal context decision; no slot question, guessed target or mutation."""
    def __init__(self, trace):
        from .models import TerminalDecision
        self.context_trace = deepcopy(trace)
        self.context_status = trace['FINAL_STATUS']
        self.decision = TerminalDecision(reason_type='USER_AMBIGUITY'
            if self.context_status == 'AMBIGUOUS' else 'SEMANTIC_RESOLUTION_FAILURE')
        self.context_trace['FINAL_DECISION'] = self.decision.model_dump(mode='json')
        super().__init__('V2_CONTEXT_' + self.context_status)


def active_id(state):
    topic = state.topics.get(state.active_topic_id)
    return topic.active_task_id if topic else None


def restored_state(session, artifact):
    if artifact is None:
        return ConversationState(state_version=0, **session._state_identity())
    return ConversationState.model_validate(session.restore(artifact, kind='CONVERSATION', defer_source_values=True))


@dataclass(frozen=True)
class DiscoveredContext:
    model_context: dict
    context_digest: str
    state_digest: str
    authority_fingerprint: str
    pending_digest: str | None


def discover_context(session, *, state=None, plans=(), pending=None):
    """Scope restoration precedes exposure; no text/parse/relation argument."""
    current = restored_state(session, state)
    prior_plans = {}
    for artifact in plans:
        plan = AuthorizedLogicalPlan.model_validate(session.restore(artifact, kind='LAST_REQUEST', defer_source_values=True))
        task = current.tasks.get(plan.task_id)
        if task is None or plan.task_version != task.active_version:
            raise ValueError('CONTEXT_PRIOR_PLAN_VERSION_MISMATCH')
        version = next(v for v in task.versions if v.version == task.active_version)
        if version.plan_id != plan.plan_id or plan.permission_requirement != session.context:
            raise ValueError('CONTEXT_PRIOR_PLAN_IDENTITY_MISMATCH')
        if plan.task_id in prior_plans:
            raise ValueError('CONTEXT_DUPLICATE_PRIOR_PLAN')
        prior_plans[plan.task_id] = plan
    pending_summary = None
    native_pending = current.pending
    if native_pending is not None:
        # The scoped conversation proves the Pending summary. A resume receipt
        # is required to APPLY an answer, not to interpret an independent task.
        receipt = session.restore(pending, kind='PENDING') if pending is not None else None
        task = current.tasks.get(native_pending.task_id)
        if (native_pending.status != 'ACTIVE' or task is None or
                task.active_version != native_pending.task_version or
                receipt is not None and (receipt.get('task_id') != native_pending.task_id or
                receipt.get('task_version') != native_pending.task_version or
                receipt.get('pending_id') != native_pending.pending_id)):
            raise RecognitionFailure('V2_PENDING_RESUME_MISMATCH')
        pending_summary = dict(pending_id=native_pending.pending_id,
            task_id=native_pending.task_id, task_version=native_pending.task_version,
            slot_path=native_pending.slot_path, active_blocker_id=native_pending.active_blocker_id,
            blockers=[dict(blocker_id=b.blocker_id, expected_answer_type=b.expected_answer_type,
                status=b.status, options=[dict(option_id=o.option_id, label=o.display_label) for o in b.options])
                for b in native_pending.blockers], asked_slots=list(native_pending.asked_slots))
    eligible = [t for t in current.tasks.values() if t.status in {'RESOLVED', 'PROVISIONAL'}
                and current.topics[t.topic_id].status != 'CANCELLED']
    def updated(task):
        return next(v.created_at.timestamp() for v in task.versions if v.version == task.active_version)
    recent = sorted(eligible, key=lambda t: (-updated(t), t.task_id))
    priority = [active_id(current), native_pending.task_id if native_pending else None]
    selected = []
    for identifier in [*priority, *(t.task_id for t in recent)]:
        task = next((t for t in eligible if t.task_id == identifier), None)
        if task is not None and task not in selected:
            selected.append(task)
        if len(selected) == MAX_CONTEXT_TASK_CANDIDATES:
            break
    candidates = []
    scope = session.context.authorized_scope.model_dump(mode='json')
    for task in selected:
        version = next(v for v in task.versions if v.version == task.active_version)
        semantics = version.semantics
        sources = ['ACTIVE'] if task.task_id == active_id(current) else ['RECENT']
        if native_pending and task.task_id == native_pending.task_id:
            sources.append('PENDING')
        candidates.append(dict(task_id=task.task_id, task_version=task.active_version,
            task_status=task.status, candidate_source=sources, candidate_scope=scope,
            metrics=_labels(semantics.metrics), dimensions=_labels(semantics.dimensions),
            subject=_labels(semantics.subject), filters=_labels(semantics.filter_expression),
            time=_labels(semantics.time_spec), cleared_slots=list(task.clear_barriers),
            query_shape=str(prior_plans[task.task_id].query_shape) if task.task_id in prior_plans else None,
            created_order=min(v.created_at.timestamp() for v in task.versions),
            updated_order=updated(task), last_user_semantic_delta=None,
            last_delta_status='NOT_STORED_IN_TASK_VERSION; DO_NOT_INFER_FROM_QUESTION'))
    context = dict(state_version=current.state_version, active_task_id=active_id(current),
        candidate_tasks=candidates, candidate_count=len(candidates), pending=pending_summary,
        candidate_scope=scope, candidate_policy='ACTIVE_PENDING_THEN_RECENT_V1',
        omitted_task_count=len(eligible)-len(candidates))
    if len(json.dumps(context, ensure_ascii=False)) > MAX_CONTEXT_CHARACTERS:
        raise ValueError('CONTEXT_SUMMARY_BUDGET_EXCEEDED')
    return DiscoveredContext(context, contract_digest(context), contract_digest(current.model_dump(mode='json')),
        session.context.fingerprint(), contract_digest(native_pending.model_dump(mode='json'))
        if native_pending is not None and pending is not None else None)


def validate_proposal(proposal, discovered, *, session, state=None, question=''):
    """Reject illegal proposals; soft parse signals never override acceptance.

    Current delta, CLEAR/REMOVE and inherited fields are NOT decided here.
    Those remain native TaskPatch/Reducer responsibilities.
    """
    proposal = ContextProposal.model_validate(proposal)
    current = restored_state(session, state)
    context = discovered.model_context
    checks = []
    def require(condition, code):
        checks.append({'constraint': code, 'passed': bool(condition)})
        return bool(condition)
    valid = require(discovered.authority_fingerprint == session.context.fingerprint(), 'CURRENT_SCOPE_MATCH')
    valid &= require(discovered.context_digest == contract_digest(context), 'CANDIDATE_SNAPSHOT_UNCHANGED')
    valid &= require(discovered.state_digest == contract_digest(current.model_dump(mode='json')),
                     'STATE_SNAPSHOT_UNCHANGED')
    valid &= require(proposal.state_version == current.state_version, 'STATE_VERSION_MATCH')
    target = None
    if proposal.status == 'ACCEPTED' and proposal.relation != 'NEW_TASK':
        offered = {row['task_id']: row for row in context['candidate_tasks']}
        target = current.tasks.get(proposal.target_task_id)
        valid &= require(proposal.target_task_id in offered, 'CANDIDATE_MEMBERSHIP')
        valid &= require(target is not None, 'TASK_EXISTS')
        if target is not None:
            valid &= require(target.status in {'RESOLVED', 'PROVISIONAL'}, 'TASK_USABLE')
            valid &= require(proposal.task_version == target.active_version ==
                offered.get(target.task_id, {}).get('task_version'), 'TASK_VERSION_MATCH')
            if proposal.relation == 'RETURN_TO_TOPIC':
                valid &= require(target.task_id != active_id(current), 'HISTORICAL_TARGET_IS_NON_ACTIVE')
            elif proposal.relation != 'ANSWER_CLARIFICATION':
                valid &= require(target.task_id == active_id(current), 'CURRENT_RELATION_TARGET_IS_ACTIVE')
        if proposal.relation == 'ANSWER_CLARIFICATION':
            p = current.pending
            valid &= require(p is not None and p.status == 'ACTIVE' and p.pending_id == proposal.pending_id and
                p.task_id == proposal.target_task_id and p.task_version == proposal.task_version and
                discovered.pending_digest == contract_digest(p.model_dump(mode='json')), 'PENDING_VERSION_MATCH')
            valid &= require(p is not None and selected_option(p, question) is not None, 'PENDING_ANSWER_ADMISSIBLE')
    status = 'ACCEPTED' if valid and proposal.status == 'ACCEPTED' else proposal.status if valid else 'REJECTED'
    trace = dict(MODEL_RELATION_PROPOSAL=proposal.relation, MODEL_TARGET_PROPOSAL=proposal.target_task_id,
        MODEL_STATUS=proposal.status,
        CONTEXT_CANDIDATES=[{k: row[k] for k in ('task_id', 'task_version', 'candidate_source', 'candidate_scope')}
                            for row in context['candidate_tasks']],
        CONTEXT_SUMMARY_HASH=contract_digest(context), HARD_CONSTRAINT_RESULTS=checks, SOFT_EVIDENCE=[],
        FINAL_RELATION=proposal.relation if status == 'ACCEPTED' else None,
        FINAL_TARGET=proposal.target_task_id if status == 'ACCEPTED' else None,
        FINAL_STATUS=status, OVERRIDE_DIRECTION='MODEL_TO_FAIL_CLOSED' if not valid else 'NONE',
        OVERRIDE_REASON=[c['constraint'] for c in checks if not c['passed']],
        CURRENT_DELTA_AND_BARRIERS='DEFERRED_TO_NATIVE_PATCH_REDUCER; NOT_CHECKED_BY_RELATION_VALIDATOR')
    return trace


def proposal_resolution(trace, parse, state, task_patch, semantic_resolution):
    """Consume validated model semantics using the existing resolution/state contract."""
    from .pipeline import TurnResolutionResult, TurnReferentialCompleteness
    from .models import ProceedDecision, TerminalDecision
    if trace['FINAL_STATUS'] != 'ACCEPTED':
        return TurnResolutionResult(dialogue_act='CONTINUE', target_topic_id=None, target_task_id=None,
            referential_completeness=TurnReferentialCompleteness(relation='UNRESOLVED_REFERENCE', depends_on_history=True),
            task_patch=task_patch, semantic_resolution=semantic_resolution,
            decision=TerminalDecision(reason_type='SEMANTIC_RESOLUTION_FAILURE'))
    act = trace['FINAL_RELATION']
    if act == 'NEW_TASK':
        target, topic, relation = 'task:' + parse.turn_id, 'topic:' + parse.turn_id, 'SELF_CONTAINED'
    else:
        task = state.tasks[trace['FINAL_TARGET']]
        target, topic = task.task_id, task.topic_id
        relation = 'HISTORICAL_TASK' if act == 'RETURN_TO_TOPIC' else 'CURRENT_TASK'
    return TurnResolutionResult(dialogue_act=act, target_topic_id=topic, target_task_id=target,
        referential_completeness=TurnReferentialCompleteness(relation=relation, depends_on_history=act != 'NEW_TASK'),
        task_patch=task_patch, semantic_resolution=semantic_resolution, decision=ProceedDecision())


def accept_proposal(session, proposal, discovered, *, state=None, question=''):
    """Register a request-local proof before any edits; never persist model authority."""
    import logging
    trace = validate_proposal(proposal, discovered, session=session, state=state, question=question)
    logging.getLogger(__name__).info('V2 context arbitration', extra={'context_arbitration': trace})
    if trace['FINAL_STATUS'] != 'ACCEPTED':
        raise ContextProposalFailure(trace)
    from .models import freeze_contract
    session._context_proposal_proof = (freeze_contract(ContextProposal.model_validate(proposal)),
        replace(discovered, model_context=freeze_contract(discovered.model_context)), freeze_contract(trace))
    return trace


def validated_session_trace(session, state):
    """Recheck state/scope before the compiler records its resolution proof."""
    proof = getattr(session, '_context_proposal_proof', None)
    if proof is None:
        return None
    proposal, discovered, _ = proof
    return accept_proposal(session, proposal, discovered, state=state, question=session._request.question)
