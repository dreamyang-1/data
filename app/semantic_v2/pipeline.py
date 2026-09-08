"""Separated, offline pipeline artifacts and deterministic compilation boundaries."""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .enums import AnalysisGoal, DialogueAct, ErrorType, ExecutionBackend, QueryShape, ResolutionStatus
from .models import (
    BoundSemanticRef, DecisionOutcome, DeliverySpec, Identifier, Mention, PermissionContext,
    PlanPayload, ResultContract, ScopePolicyDecision, SemanticResolutionContract,
    ServiceRoute, SnapshotContext, StrictModel, VersionMetadata, AdapterReport, CompensationPlan,
)
from .registries import PayloadContractRegistry
from .slot_reducer import TaskPatch
from .authorized_contract import (AuthorizedScopeContext, AuthorizedVersionMetadata,
    CatalogBindingEvidence, SourceValueBindingEvidence, DatasetBindingEvidence, TaskBindingEvidence, validate_authorized_refs)


class OperationMarker(StrictModel):
    mention_id: Identifier
    operation_hint: Literal['ADD', 'REPLACE', 'REMOVE', 'CLEAR', 'SET', 'INHERIT']
    slot_name: Identifier


class CurrentTurnSemanticParse(StrictModel):
    """Allowed model output: surface facts only, never canonical IDs or execution choices."""
    mentions: list[Mention] = Field(default_factory=list)
    dialogue_act_candidates: list[DialogueAct] = Field(default_factory=list)
    operation_markers: list[OperationMarker] = Field(default_factory=list)
    reference_signals: list[Literal['PRONOUN', 'ELLIPSIS', 'HISTORICAL', 'ORDINAL']] = Field(default_factory=list)
    followup_signals: list[Literal['CONTINUE', 'MODIFY', 'ADD', 'DRILL_DOWN']] = Field(default_factory=list)
    topic_shift_signals: list[Literal['EXPLICIT_NEW_TASK']] = Field(default_factory=list)
    negations: list[Identifier] = Field(default_factory=list)
    coordination_groups: list[list[Identifier]] = Field(default_factory=list)
    temporal_expressions: list[Identifier] = Field(default_factory=list)
    query_shape_prediction: QueryShape | None = None
    explicit_slot_mentions: dict[str, list[Identifier]] = Field(default_factory=dict)


class CandidateSelectionDecision(StrictModel):
    candidate_id: Identifier | None = None
    status: ResolutionStatus

    def validate_candidates(self, available_ids: set[str]):
        if self.status == ResolutionStatus.ACCEPTED and self.candidate_id not in available_ids:
            raise ValueError('model may only select an existing candidate ID')
        if self.status != ResolutionStatus.ACCEPTED and self.candidate_id is not None:
            raise ValueError('unresolved model choice cannot select a candidate')


class CurrentTurnParseResult(CurrentTurnSemanticParse):
    turn_id: Identifier
    text_ref: Identifier
    text_digest: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def current_turn_only(self):
        from .registries import SlotDefinitionRegistry
        ids = {m.mention_id for m in self.mentions}
        if len(ids) != len(self.mentions) or any(m.source_turn_id != self.turn_id for m in self.mentions):
            raise ValueError('parse mentions must be unique and belong to current turn')
        referenced = [*self.negations, *self.temporal_expressions,
                      *(i for group in self.coordination_groups for i in group),
                      *(i for group in self.explicit_slot_mentions.values() for i in group),
                      *(m.mention_id for m in self.operation_markers)]
        if not set(referenced) <= ids:
            raise ValueError('parse refers to nonexistent mention')
        for slot in self.explicit_slot_mentions:
            SlotDefinitionRegistry.get(slot)
        for marker in self.operation_markers:
            SlotDefinitionRegistry.get(marker.slot_name)
        return self


class CurrentTurnParser:
    """Validates recorded stage output against one raw message. No model invocation."""
    @staticmethod
    def parse(*, text: str, turn_id: str, text_ref: str, parsed: CurrentTurnSemanticParse) -> CurrentTurnParseResult:
        for mention in parsed.mentions:
            if mention.end_char > len(text) or text[mention.start_char:mention.end_char] != mention.surface or mention.source_turn_id != turn_id:
                raise ValueError('SURFACE_PARSE_FAILURE: invalid current-turn mention span')
        return CurrentTurnParseResult(**parsed.model_dump(), turn_id=turn_id, text_ref=text_ref,
                                      text_digest=hashlib.sha256(text.encode()).hexdigest())


class TurnReferentialCompleteness(StrictModel):
    relation: Literal['SELF_CONTAINED', 'CURRENT_TASK', 'HISTORICAL_TASK', 'UNRESOLVED_REFERENCE']
    depends_on_history: bool
    evidence_mention_ids: list[Identifier] = Field(default_factory=list)


class TurnResolutionResult(StrictModel):
    dialogue_act: DialogueAct
    target_topic_id: Identifier | None
    target_task_id: Identifier | None
    referential_completeness: TurnReferentialCompleteness
    task_patch: TaskPatch
    semantic_resolution: SemanticResolutionContract
    decision: DecisionOutcome


class TurnResolver:
    @staticmethod
    def resolve(parse: CurrentTurnParseResult, *, state, task_patch: TaskPatch,
                semantic_resolution: SemanticResolutionContract, historical_task_id: str | None = None) -> TurnResolutionResult:
        from .models import ProceedDecision, TerminalDecision
        dependency = bool(parse.reference_signals or parse.followup_signals)
        if parse.topic_shift_signals:
            act, relation, topic, task, dependency = (DialogueAct.NEW_TASK, 'SELF_CONTAINED',
                'topic:' + parse.turn_id, 'task:' + parse.turn_id, False)
        elif 'HISTORICAL' in parse.reference_signals:
            target = state.tasks.get(historical_task_id) if historical_task_id else None
            act, relation = DialogueAct.RETURN_TO_TOPIC, 'HISTORICAL_TASK' if target else 'UNRESOLVED_REFERENCE'
            topic, task = (target.topic_id, target.task_id) if target else (None, None)
        elif dependency:
            topic = state.active_topic_id
            task = state.topics[topic].active_task_id if topic in state.topics else None
            relation = 'CURRENT_TASK' if task else 'UNRESOLVED_REFERENCE'
            hints = {m.operation_hint for m in parse.operation_markers}
            act = DialogueAct.ADD if hints == {'ADD'} else DialogueAct.REPLACE if hints == {'REPLACE'} else DialogueAct.MODIFY if hints else DialogueAct.CONTINUE
        else:
            act, relation, topic, task = DialogueAct.NEW_TASK, 'SELF_CONTAINED', 'topic:' + parse.turn_id, 'task:' + parse.turn_id
        return TurnResolutionResult(dialogue_act=act, target_topic_id=topic, target_task_id=task,
            referential_completeness=TurnReferentialCompleteness(relation=relation, depends_on_history=dependency),
            task_patch=task_patch, semantic_resolution=semantic_resolution,
            decision=ProceedDecision() if task else TerminalDecision(reason_type=ErrorType.SEMANTIC_RESOLUTION_FAILURE))


class ExecutionReadiness:
    @staticmethod
    def evaluate(blockers):
        from .models import ClarificationDecision, ProceedDecision, SystemRepairDecision, TerminalDecision
        blocking = [b for b in blockers if b.severity.value == 'BLOCKING']
        if not blocking:
            return ProceedDecision()
        system = [b for b in blocking if b.blocker_type != ErrorType.USER_AMBIGUITY]
        if system:
            return SystemRepairDecision(blockers=system)
        ambiguity = next((b for b in blocking if b.user_action_required), None)
        if ambiguity:
            return ClarificationDecision(blocker=ambiguity, blocking=True, affected_plan_paths=[ambiguity.plan_path],
                                         base_task_version=0, state_version=0, create_pending=True)
        return TerminalDecision(reason_type=ErrorType.USER_AMBIGUITY)


class BoundRefAuthorization(StrictModel):
    """Per-plan decision evidence, not an ACL copy."""
    canonical_id: Identifier
    catalog_type: Identifier
    catalog_version: Identifier
    semantic_model_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    application_id: Identifier
    authorization_decision_id: Identifier
    policy_snapshot_id: Identifier
    allowed: Literal[True]


def collect_bound_refs(value) -> tuple[BoundSemanticRef, ...]:
    if isinstance(value, BoundSemanticRef):
        return (value,)
    if isinstance(value, StrictModel):
        return tuple(ref for name in type(value).model_fields for ref in collect_bound_refs(getattr(value, name)))
    if isinstance(value, dict):
        return tuple(ref for item in value.values() for ref in collect_bound_refs(item))
    if isinstance(value, (list, tuple)):
        return tuple(ref for item in value for ref in collect_bound_refs(item))
    return ()


def validate_bound_ref_scope_and_permission(value, snapshot: SnapshotContext, permission: PermissionContext,
                                          authorizations: tuple[BoundRefAuthorization, ...]) -> None:
    if isinstance(permission, AuthorizedScopeContext):
        validate_authorized_refs(value, snapshot, permission, authorizations)
        return
    from .models import require_bounded_legacy_time
    require_bounded_legacy_time(value)
    refs = collect_bound_refs(value)
    if refs and not all((snapshot.catalog_publish_id, snapshot.vector_index_version, snapshot.semantic_model_id,
                         snapshot.database_id, snapshot.business_domain_ids, permission.authorization_decision_id,
                         permission.policy_snapshot_id, permission.row_scope_hash, permission.column_scope_hash,
                         permission.metric_scope_hash)):
        raise ValueError('PLAN_VALIDATION_FAILURE: missing snapshot or permission evidence')
    for ref in refs:
        if (not ref.business_domain_ids or ref.catalog_version != snapshot.catalog_version or ref.semantic_model_id != snapshot.semantic_model_id or
                not set(ref.business_domain_ids) <= set(snapshot.business_domain_ids) or
                not set(ref.business_domain_ids) <= set(permission.allowed_business_domain_ids)):
            raise ValueError('PLAN_VALIDATION_FAILURE: bound reference snapshot/scope mismatch')
        if not any(a.canonical_id == ref.canonical_id and a.catalog_type == ref.catalog_type and
                   a.catalog_version == ref.catalog_version and a.semantic_model_id == ref.semantic_model_id and
                   a.tenant_id == permission.tenant_id and a.user_id == permission.user_id and
                   a.application_id == permission.application_id and
                   a.authorization_decision_id == permission.authorization_decision_id and
                   a.policy_snapshot_id == permission.policy_snapshot_id for a in authorizations):
            raise ValueError('PERMISSION_DENIED: missing authorization for bound reference')


class LogicalPlan(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['0.2.1', '0.2.2'] = '0.2.1'
    plan_id: Identifier
    topic_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    service_route: ServiceRoute
    analysis_goals: list[AnalysisGoal]
    payload: PlanPayload
    delivery_spec: DeliverySpec
    permission_requirement: PermissionContext | AuthorizedScopeContext
    snapshot_requirement: SnapshotContext
    policy_decisions: list[ScopePolicyDecision] = Field(default_factory=list)
    version_metadata: VersionMetadata | AuthorizedVersionMetadata
    current_turn_ref: Identifier
    current_turn_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    sanitized_turn_text: str | None = None
    permission_proofs: tuple[BoundRefAuthorization | CatalogBindingEvidence | SourceValueBindingEvidence | DatasetBindingEvidence | TaskBindingEvidence, ...] = ()

    @property
    def query_shape(self):
        return PayloadContractRegistry.resolve_query_shape(self.payload.payload_type)

    @property
    def semantic_fingerprint(self):
        from .slot_reducer import semantic_fingerprint
        return semantic_fingerprint(self)

    @model_validator(mode='after')
    def final_plan_invariants(self):
        from .models import freeze_contract
        authorized = isinstance(self.permission_requirement, AuthorizedScopeContext)
        expected_version = '0.2.2' if authorized else '0.2.1'
        expected_proof = (CatalogBindingEvidence, SourceValueBindingEvidence, DatasetBindingEvidence, TaskBindingEvidence) if authorized else BoundRefAuthorization
        if (self.schema_version != expected_version or self.version_metadata.schema_version != expected_version
                or self.version_metadata.plan_schema_version != expected_version
                or any(not isinstance(p, expected_proof) for p in self.permission_proofs)):
            raise ValueError('PLAN_VALIDATION_FAILURE: incompatible scope contract version')
        PayloadContractRegistry.validate(self.payload, self.service_route, self.analysis_goals)
        if authorized:
            from .temporal_comparisons import validate_temporal_payload
            validate_temporal_payload(self.payload)
        validate_bound_ref_scope_and_permission(self.payload, self.snapshot_requirement,
                                              self.permission_requirement, self.permission_proofs)
        for name in type(self).model_fields:
            object.__setattr__(self, name, freeze_contract(getattr(self, name)))
        return self


class BackendContract(StrictModel):
    contract_id: Identifier
    contract_version: Identifier
    semantic_fingerprint: Identifier
    mode: Literal['SHADOW_ONLY'] = 'SHADOW_ONLY'


class AuthorizedLogicalPlan(LogicalPlan):
    """Explicit schema export for the current upstream scope contract."""
    schema_version: Literal['0.2.2'] = '0.2.2'
    permission_requirement: AuthorizedScopeContext
    version_metadata: AuthorizedVersionMetadata
    permission_proofs: tuple[CatalogBindingEvidence | SourceValueBindingEvidence | DatasetBindingEvidence | TaskBindingEvidence, ...] = ()


class ExecutablePlan(StrictModel):
    logical_plan: LogicalPlan
    execution_backend: ExecutionBackend
    result_contract: ResultContract
    backend_contract: BackendContract
    adapter_report: AdapterReport
    compensation_plan: CompensationPlan | None = None

    @model_validator(mode='after')
    def deterministic_outputs(self):
        definition = PayloadContractRegistry.get(self.logical_plan.payload.payload_type)
        if self.execution_backend != definition.execution_backend:
            raise ValueError('backend does not match registry')
        from .result_contract import ResultContractCompiler
        expected = ResultContractCompiler.compile(self.logical_plan)
        if self.result_contract != expected or self.backend_contract.semantic_fingerprint != self.logical_plan.semantic_fingerprint:
            raise ValueError('result/backend contract does not match logical plan')
        validate_bound_ref_scope_and_permission(self.result_contract, self.logical_plan.snapshot_requirement,
                                              self.logical_plan.permission_requirement, self.logical_plan.permission_proofs)
        return self


class LogicalPlanCompiler:
    @staticmethod
    def compile(*, parse: CurrentTurnParseResult, resolution, payload: PlanPayload,
                service_route: ServiceRoute, analysis_goals: list[AnalysisGoal],
                task_version: int, permission: PermissionContext, snapshot: SnapshotContext,
                authorizations: tuple[BoundRefAuthorization, ...], version_metadata: VersionMetadata,
                delivery_spec: DeliverySpec | None = None) -> LogicalPlan:
        if resolution.decision.decision_type != 'PROCEED':
            raise ValueError('PLAN_VALIDATION_FAILURE: turn resolution does not permit compilation')
        if not resolution.target_topic_id or not resolution.target_task_id:
            raise ValueError('PLAN_VALIDATION_FAILURE: unresolved task target')
        contract = resolution.semantic_resolution
        refs = collect_bound_refs(payload)
        if refs:
            if contract.status != ResolutionStatus.ACCEPTED:
                raise ValueError('SEMANTIC_RESOLUTION_FAILURE: accepted resolution required')
            selected = next(p for p in contract.plan_candidates if p.plan_candidate_id == contract.selected_plan_candidate_id)
            selected_ids = set(selected.semantic_candidate_ids)
            accepted = [c for s in contract.candidate_sets for c in s.candidates if c.candidate_id in selected_ids]
            for ref in refs:
                if not any(c.canonical_id == ref.canonical_id and c.canonical_code == ref.canonical_code and
                           c.catalog_type == ref.catalog_type and c.catalog_version == ref.catalog_version and
                           c.candidate_role == ref.semantic_role and c.permission_allowed is True for c in accepted):
                    raise ValueError('SEMANTIC_RESOLUTION_FAILURE: plan reference not selected from candidates')
        plan_type = AuthorizedLogicalPlan if isinstance(permission, AuthorizedScopeContext) else LogicalPlan
        plan_id = 'plan:' + parse.text_digest[:24] + ':' + str(task_version)
        if isinstance(permission, AuthorizedScopeContext):
            from .authorized_contract import contract_digest
            plan_id = 'plan:' + contract_digest({'context': permission.fingerprint(), 'turn': parse.text_digest,
                'message': parse.text_ref, 'task': resolution.target_task_id, 'version': task_version})
        return plan_type(plan_id=plan_id,
                           schema_version='0.2.2' if isinstance(permission, AuthorizedScopeContext) else '0.2.1',
                           topic_id=resolution.target_topic_id, task_id=resolution.target_task_id,
                           task_version=task_version, service_route=service_route, analysis_goals=analysis_goals,
                           payload=payload, delivery_spec=delivery_spec or DeliverySpec(),
                           permission_requirement=permission, snapshot_requirement=snapshot,
                           permission_proofs=authorizations, version_metadata=version_metadata,
                           current_turn_ref=parse.text_ref, current_turn_digest=parse.text_digest)


def compile_executable_plan(plan: LogicalPlan) -> ExecutablePlan:
    from .legacy_adapter import assess_legacy_adapter
    from .result_contract import ResultContractCompiler
    return ExecutablePlan(logical_plan=plan,
                          execution_backend=PayloadContractRegistry.get(plan.payload.payload_type).execution_backend,
                          result_contract=ResultContractCompiler.compile(plan),
                          backend_contract=BackendContract(contract_id='shadow-backend', contract_version='0.2.1', semantic_fingerprint=plan.semantic_fingerprint),
                          adapter_report=assess_legacy_adapter(plan))
