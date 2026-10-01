"""Grounded ambiguity and exact Pending-answer handling for the raw V2 path."""
from __future__ import annotations

from math import log2
from typing import Literal

from pydantic import Field

from app.domain.models import ClarificationDecisionTrace
from . import models as m
from .authorized_contract import ScopedArtifact, contract_digest
from .recognition_client import RecognitionFailure
from .state_machine import PendingBlocker


class AmbiguityDraft(m.StrictModel):
    mention_id: m.Identifier
    slot_path: Literal['metrics','dimensions','subject']
    operation: Literal['SET','ADD','REPLACE']
    candidate_handles: list[m.Identifier] = Field(min_length=2,max_length=10)


class PendingResume(m.StrictModel):
    pending_id: m.Identifier
    task_id: m.Identifier
    task_version: int = Field(ge=1)
    payload_type: m.Identifier
    operations: dict[str, Literal['SET','ADD','REPLACE']]


class RecognizedClarification(m.StrictModel):
    decision: m.ClarificationDecision
    trace: ClarificationDecisionTrace
    next_state: ScopedArtifact
    pending_state: ScopedArtifact
    question: str
    plan: None = None


def governed_aliases(metadata):
    """Read governed alias terms using the catalog's stored-list convention."""
    raw=metadata.get('entity_alias' if metadata.get('type')=='entity' else 'synonyms',[])
    if isinstance(raw,str):
        values=[raw]
        for separator in (',', '，', ';', '；', '\n'):
            values=[part for value in values for part in value.split(separator)]
        raw=values
    return list(dict.fromkeys(x.strip() for x in raw
        if isinstance(x,str) and x.strip())) if isinstance(raw,list) else []


def selected_option(pending, text):
    if pending is None or pending.status!='ACTIVE':return None
    blocker=next((b for b in pending.blockers if b.blocker_id==pending.active_blocker_id and b.status=='OPEN'),None)
    if blocker is None:return None
    # Match the whole answer. The grammar is the existing V1 choice grammar,
    # implemented without a new regular expression or substring fallback.
    compact=''.join(text.split()).strip('，,。.!！?？;；：:')
    exact=[o for o in blocker.options if compact in {''.join(o.display_label.split()),o.option_id}]
    if len(exact)==1:return exact[0]
    if exact:return None
    token=compact.removeprefix('我')
    for prefix in ('选择','选','用','采用'):
        if token.startswith(prefix):token=token[len(prefix):];break
    token=token.removeprefix('第')
    if token.endswith(('个','项','条')):token=token[:-1]
    numbers={str(i):i for i in range(1,11)}|dict(zip('一二三四五六七八九十',range(1,11)))
    index=numbers.get(token)
    return blocker.options[index-1] if index and index<=len(blocker.options) else None


def pending_identity(task_id,payload_type,operations,blockers):
    def option_material(option):
        value = option.model_dump(mode='json')
        if value.get('filter_choice') is None:
            value.pop('filter_choice', None)
        return value
    immutable=[dict(blocker_id=b.blocker_id,slot=b.plan_path,
        options=[option_material(o) for o in b.options]) for b in blockers]
    return 'pending:'+contract_digest([task_id,payload_type,operations,immutable])[:32]


def prepare_ambiguities(session,parse,draft,handles,candidates,edit_type):
    """Return verified blockers/deferred evidence or deterministic unique repairs."""
    mentions={x.mention_id:x for x in parse.mentions}
    role_for={'metrics':'MEASURE','dimensions':'GROUP_BY','subject':'SUBJECT_ENTITY'}
    edits=list(draft.edits);blockers=[];operations={};deferred=set();seen=set()
    for proposed in draft.ambiguities:
        mention=mentions.get(proposed.mention_id)
        key=(proposed.slot_path,proposed.operation,proposed.mention_id)
        marker={(o.slot_name,o.operation_hint,o.mention_id) for o in parse.operation_markers}
        explicit=proposed.mention_id in parse.explicit_slot_mentions.get(proposed.slot_path,[])
        if mention is None or (key not in marker and not(proposed.operation=='SET' and explicit)):
            raise RecognitionFailure('V2_AMBIGUITY_EVIDENCE_NOT_CURRENT')
        if proposed.slot_path in seen or any(e.slot_path==proposed.slot_path for e in draft.edits):
            raise RecognitionFailure('V2_CONFLICTING_AMBIGUITY_SLOT')
        seen.add(proposed.slot_path)
        role=role_for[proposed.slot_path]
        offered=[c for c in candidates if c['mention_id']==mention.mention_id and c['role']==role]
        by_handle={c['binding_handle']:c for c in offered}
        if any(h not in by_handle for h in proposed.candidate_handles):
            raise RecognitionFailure('V2_AMBIGUITY_HANDLE_NOT_OFFERED')
        exact=[c for c in offered if mention.surface in {c['name'],c['code']}]
        aliases=[c for c in offered if mention.surface in c['aliases']]
        matches=exact or aliases
        if len(matches)==1:
            value={'binding_handle':matches[0]['binding_handle']}
            edits.append(edit_type(slot_path=proposed.slot_path,operation=proposed.operation,
                evidence_mention_ids=[mention.mention_id],value=value if proposed.slot_path=='subject' else [value]))
            continue
        if len(matches)<2 or {c['binding_handle'] for c in matches}!=set(proposed.candidate_handles):
            raise RecognitionFailure('V2_CATALOG_AMBIGUITY_NOT_PROVEN')
        if len({c['name'] for c in matches})!=len(matches):
            raise RecognitionFailure('V2_CATALOG_OPTION_LABELS_NOT_DISTINCT')
        options=[]
        for candidate in sorted(matches,key=lambda c:c['binding_handle']):
            rid,role,mid=handles[candidate['binding_handle']]
            ref=session.bind(rid,role,(parse.turn_id+':'+mid,))
            options.append(m.ClarificationOption(option_id='option:'+contract_digest(ref.model_dump(mode='json'))[:24],
                display_label=ref.display_name,canonical_ref=ref,evidence=['PINNED_GOVERNED_SYNONYM_COLLISION']))
        identifier='blocker:'+contract_digest([parse.turn_id,proposed.slot_path,mention.mention_id])[:24]
        blockers.append(PendingBlocker(blocker_id=identifier,plan_path=proposed.slot_path,expected_answer_type='OPTION_ID',
            candidate_ids=[o.option_id for o in options],information_gain=log2(len(options)),options=options))
        operations[identifier]=proposed.operation;deferred.add(key)
    remaining=set(draft.unresolved_mention_ids)-{a.mention_id for a in draft.ambiguities}
    if remaining:raise RecognitionFailure('V2_RECOGNITION_UNRESOLVED')
    data=draft.model_dump();data.update(edits=[e.model_dump() for e in edits],unresolved_mention_ids=[])
    return type(draft).model_validate(data),blockers,operations,deferred


def clarification_result(session,state,pending,resume,request,*,initial,previous_state):
    blocker=next(b for b in pending.blockers if b.blocker_id==pending.active_blocker_id)
    if not blocker.already_asked or blocker.status!='OPEN':
        raise RecognitionFailure('V2_PENDING_QUESTION_NOT_SCHEDULED')
    previous=previous_state.pending_records.get(pending.pending_id)
    already_asked=bool(previous and any(b.blocker_id==blocker.blocker_id and b.already_asked for b in previous.blockers))
    if already_asked or (initial and previous is not None):
        raise RecognitionFailure('V2_REPEATED_CLARIFICATION')
    question='请选择：'+'；'.join(f'{i}. {o.display_label}' for i,o in enumerate(blocker.options,1))
    if len(question)>1000:
        question='你想看哪一项？请从选项中选择。'
    # Keep the actually emitted question in the task's existing Pending fields.
    from .state_machine import ConversationState
    data=state.model_dump(mode='json')
    data['pending_records'][pending.pending_id].update(question=question,last_question=question)
    state=ConversationState.model_validate(data)
    trace=ClarificationDecisionTrace(conversation_id=request.conversation_id,message_id=request.message_id,
        source_stage='SEMANTIC_GROUNDING',reason_type='USER_SEMANTIC_AMBIGUITY',blocking_slot=blocker.plan_path,
        expected_answer_type='OPTION_ID',candidate_ids=blocker.candidate_ids,already_asked=already_asked,
        base_task_reference=pending.task_id+':'+str(pending.task_version),pending_reference=pending.pending_id,
        evidence_codes=(['VERIFIED_SOURCE_VALUE_COLLISION','NO_UNIQUE_SOURCE_VALUE','FIRST_ASK_FOR_BLOCKER']
            if any(o.filter_choice is not None for o in blocker.options) else
            ['PINNED_GOVERNED_SYNONYM_COLLISION','NO_UNIQUE_CATALOG_MATCH','FIRST_ASK_FOR_BLOCKER']),
        is_user_ambiguity=True,system_repair_possible=False,safe_default_available=False,decision='ASK')
    decision=m.ClarificationDecision(blocking=True,options=blocker.options,affected_plan_paths=[blocker.plan_path],
        candidate_answers=[o.display_label for o in blocker.options],
        information_gain=blocker.information_gain,base_task_version=pending.task_version,state_version=state.state_version,
        create_pending=initial,already_asked=already_asked)
    session._require_refs(session._decoded_refs([o.model_dump(mode='json') for o in blocker.options]))
    session.accept_catalog()
    return RecognizedClarification(decision=decision,trace=trace,question=question,
        next_state=session.seal(kind='CONVERSATION',payload=state),pending_state=session.seal(kind='PENDING',payload=resume))
