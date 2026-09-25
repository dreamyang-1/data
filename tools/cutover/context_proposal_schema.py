"""Round 4 diagnostic extension of the existing parse, NOT a production schema.

There is no second parser or authority object: surface facts are inherited from
CurrentTurnSemanticParse and the proposal names only offered task candidates.
"""
from copy import deepcopy
from typing import Literal

from pydantic import Field, model_validator

from app.semantic_v2.models import StrictModel, Identifier
from app.semantic_v2.pipeline import CurrentTurnSemanticParse
from app.semantic_v2.recognition import current_turn_schema

CONTRACT_VERSION = 'context-proposal-experiment-v1.1'
RELATIONS = ('NEW_TASK', 'CONTINUE', 'MODIFY', 'ADD', 'REPLACE', 'REMOVE',
             'CLEAR', 'CORRECT', 'DRILL_DOWN', 'RETURN_TO_TOPIC', 'ANSWER_CLARIFICATION')


class ContextProposal(StrictModel):
    status: Literal['ACCEPTED', 'UNRESOLVED', 'AMBIGUOUS']
    relation: Literal[*RELATIONS] | None
    target_task_id: Identifier | None
    state_version: int = Field(strict=True, ge=0)
    task_version: int | None = Field(strict=True, ge=1)
    pending_id: Identifier | None

    @model_validator(mode='after')
    def consistent_proposal(self):
        if self.status != 'ACCEPTED':
            if any(v is not None for v in (self.relation, self.target_task_id,
                                           self.task_version, self.pending_id)):
                raise ValueError('UNRESOLVED_MUST_NOT_SELECT_TARGET')
        elif self.relation is None:
            raise ValueError('ACCEPTED_RELATION_REQUIRED')
        elif self.relation == 'NEW_TASK':
            if any(v is not None for v in (self.target_task_id, self.task_version, self.pending_id)):
                raise ValueError('NEW_TASK_MUST_NOT_INHERIT_TARGET')
        elif self.target_task_id is None or self.task_version is None:
            raise ValueError('RELATION_REQUIRES_TARGET_VERSION')
        elif (self.relation == 'ANSWER_CLARIFICATION') != (self.pending_id is not None):
            raise ValueError('PENDING_RELATION_CONSISTENCY')
        return self


class ContextAwareParse(CurrentTurnSemanticParse):
    context_proposal: ContextProposal


def proposal_schema(context):
    schema = ContextAwareParse.model_json_schema()
    existing = current_turn_schema()
    schema['properties'].update(deepcopy(existing['properties']))
    schema['$defs'].update(deepcopy(existing['$defs']))
    definition = schema['$defs']['ContextProposal']['properties']
    ids = [row['task_id'] for row in context['candidate_tasks']]
    definition['target_task_id'] = ({'anyOf': [{'type': 'string', 'enum': ids}, {'type': 'null'}]}
                                     if ids else {'type': 'null'})
    pending = context['pending']
    definition['pending_id'] = ({'anyOf': [{'const': pending['pending_id'], 'type': 'string'}, {'type': 'null'}]}
                               if pending else {'type': 'null'})
    # Pydantic model_validator predicates are not automatically exported. The
    # model must see the same relation/target/status consistency as validation.
    # v1 captures remain v1: exporting these constraints does not rescore them.
    null_target = {key: {'type': 'null'} for key in ('target_task_id', 'task_version', 'pending_id')}
    schema['$defs']['ContextProposal']['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['UNRESOLVED', 'AMBIGUOUS']}}},
         'then': {'properties': {**null_target, 'relation': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}, 'relation': {'const': 'NEW_TASK'}}},
         'then': {'properties': null_target}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}}},
         'then': {'properties': {'relation': {'type': 'string'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}, 'relation': {'enum': list(RELATIONS[1:])}}},
         'then': {'properties': {'target_task_id': {'type': 'string'}, 'task_version': {'type': 'integer'}}}},
        {'if': {'properties': {'relation': {'const': 'ANSWER_CLARIFICATION'}}},
         'then': {'properties': {'pending_id': {'type': 'string'}}},
         'else': {'properties': {'pending_id': {'type': 'null'}}}},
    ]
    return schema
