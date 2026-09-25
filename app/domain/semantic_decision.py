"""Internal semantic handoff between context recognition and execution.

The contract is deliberately absent from the HTTP/SSE models.  It can only be
attached to ``ChatRequest`` as a private attribute by a trusted in-process
planner, so callers cannot manufacture semantic bindings or scope proof.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.domain.models import PrimaryIntent, StrictModel
from app.domain.semantic_scope import AuthorizedSemanticScope


class SemanticDecisionSource(StrEnum):
    V2_AUTHORIZED_PLAN = "V2_AUTHORIZED_PLAN"
    V1_SEMANTIC_FALLBACK = "V1_SEMANTIC_FALLBACK"


class SemanticDecisionField(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_type: str = Field(min_length=1, max_length=50)
    canonical_id: str | None = Field(default=None, min_length=1, max_length=256)
    canonical_code: str | None = Field(default=None, min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=500)
    semantic_model_id: str | None = Field(default=None, min_length=1, max_length=128)
    catalog_version: str | None = Field(default=None, min_length=1, max_length=256)
    business_domain_ids: tuple[str, ...] = Field(default=(), max_length=50)
    resolution_source: str | None = Field(default=None, min_length=1, max_length=128)
    binding_status: Literal["AUTHORIZED", "UNBOUND"] = "AUTHORIZED"

    @model_validator(mode="after")
    def authorized_field_has_identity(self) -> "SemanticDecisionField":
        if self.binding_status == "AUTHORIZED":
            if not all((
                self.canonical_id,
                self.canonical_code,
                self.semantic_model_id,
                self.catalog_version,
                self.resolution_source,
            )):
                raise ValueError(
                    "authorized semantic field requires complete catalog identity"
                )
            if any(
                not value.isascii()
                or not value.isdecimal()
                or int(value) <= 0
                or str(int(value)) != value
                for value in self.business_domain_ids
            ):
                raise ValueError("semantic field contains an invalid business domain")
        elif any((
            self.canonical_id,
            self.canonical_code,
            self.semantic_model_id,
            self.catalog_version,
            self.business_domain_ids,
            self.resolution_source,
        )):
            raise ValueError("unbound semantic field cannot carry catalog authority")
        return self


class SemanticDecisionFilter(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: SemanticDecisionField
    operator: str = Field(min_length=1, max_length=50)
    value: JsonValue = None
    value_type: str = Field(min_length=1, max_length=50)
    source: str = Field(min_length=1, max_length=100)
    value_refs: tuple[SemanticDecisionField, ...] = ()


class SemanticDecisionTimeRange(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: datetime | None = None
    end_exclusive: datetime | None = None
    grain: str = Field(default="NONE", min_length=1, max_length=30)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def coherent_range(self) -> "SemanticDecisionTimeRange":
        if (self.start is None) != (self.end_exclusive is None):
            raise ValueError("semantic decision time range must contain both bounds")
        if self.start is not None and self.end_exclusive <= self.start:
            raise ValueError("semantic decision time range must be non-empty")
        return self


class SemanticDecisionTask(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    intent: PrimaryIntent | None = None
    secondary_intents: tuple[PrimaryIntent, ...] = ()
    intent_confidence: float = Field(default=0.0, ge=0, le=1)
    payload_type: str | None = Field(default=None, max_length=50)
    metrics: tuple[SemanticDecisionField, ...] = ()
    dimensions: tuple[SemanticDecisionField, ...] = ()
    fields: tuple[SemanticDecisionField, ...] = ()
    filters: tuple[SemanticDecisionFilter, ...] = ()
    time_range: SemanticDecisionTimeRange | None = None
    business_objects: tuple[SemanticDecisionField, ...] = ()
    depends_on: tuple[str, ...] = ()
    ranking_limit: int | None = Field(default=None, ge=1, le=10000)
    comparison_type: str | None = Field(default=None, max_length=100)
    forecast_horizon_periods: int | None = Field(default=None, ge=1, le=1000)
    forecast_granularity: str | None = Field(default=None, max_length=30)
    semantic_complete: bool = False
    adaptation_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def authoritative_intent_is_complete(self) -> "SemanticDecisionTask":
        if self.semantic_complete and self.intent is None:
            raise ValueError("a complete semantic task requires an intent")
        if self.semantic_complete:
            refs = [
                *self.metrics,
                *self.dimensions,
                *self.fields,
                *self.business_objects,
                *(item.field for item in self.filters),
                *(
                    ref
                    for item in self.filters
                    for ref in item.value_refs
                ),
            ]
            if any(item.binding_status != "AUTHORIZED" for item in refs):
                raise ValueError(
                    "a complete semantic task cannot contain unbound fields"
                )
        return self


class SemanticScopeProof(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal["TRUSTED_UPSTREAM_BACKEND"] = "TRUSTED_UPSTREAM_BACKEND"
    authorized_scope: AuthorizedSemanticScope
    authorized_scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_scope_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    plan_semantic_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    plan_id: str | None = Field(default=None, min_length=1, max_length=256)
    catalog_version: str | None = Field(default=None, min_length=1, max_length=256)
    semantic_model_version: str | None = Field(
        default=None, min_length=1, max_length=256
    )

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> "SemanticScopeProof":
        if self.authorized_scope.fingerprint() != self.authorized_scope_fingerprint:
            raise ValueError("semantic decision scope fingerprint mismatch")
        return self


class SemanticDecision(StrictModel):
    """One immutable, scope-bound semantic decision for the execution bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["semantic-decision-v1"] = "semantic-decision-v1"
    source: SemanticDecisionSource
    status: Literal["ACCEPTED", "REQUIRES_V1_FALLBACK"]
    message_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=100)
    conversation_state: str = Field(min_length=1, max_length=100)
    original_question: str = Field(min_length=1, max_length=4000)
    completed_question: str = Field(min_length=1, max_length=4000)
    tasks: tuple[SemanticDecisionTask, ...] = Field(min_length=1, max_length=5)
    scope_proof: SemanticScopeProof
    clarification_needed: bool = False
    clarification_reason: str | None = Field(default=None, max_length=500)
    fallback_reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def coherent_status(self) -> "SemanticDecision":
        if self.status == "ACCEPTED":
            if self.source != SemanticDecisionSource.V2_AUTHORIZED_PLAN:
                raise ValueError("only a V2 authorized plan may be accepted")
            if self.clarification_needed or not all(
                task.semantic_complete for task in self.tasks
            ):
                raise ValueError("accepted semantic decision must be complete")
            scope = self.scope_proof.authorized_scope
            for task in self.tasks:
                refs = [
                    *task.metrics,
                    *task.dimensions,
                    *task.fields,
                    *task.business_objects,
                    *(item.field for item in task.filters),
                    *(ref for item in task.filters for ref in item.value_refs),
                ]
                for ref in refs:
                    if ref.semantic_model_id != str(scope.semantic_model_id):
                        raise ValueError(
                            "accepted semantic field belongs to another model"
                        )
                    domains = {int(value) for value in ref.business_domain_ids}
                    if scope.business_domain_ids and (
                        not domains
                        or not domains.issubset(set(scope.business_domain_ids))
                    ):
                        raise ValueError(
                            "accepted semantic field is outside the authorized domains"
                        )
                    if (
                        self.scope_proof.catalog_version is not None
                        and ref.catalog_version
                        != self.scope_proof.catalog_version
                    ):
                        raise ValueError(
                            "accepted semantic field belongs to another catalog version"
                        )
                    if (
                        ref.catalog_type == "ENTITY_VALUE"
                        and not str(ref.resolution_source).startswith(
                            "VERIFIED_SOURCE_"
                        )
                    ):
                        raise ValueError(
                            "accepted entity value lacks current-source proof"
                        )
        elif not self.fallback_reason:
            raise ValueError("V1 semantic fallback requires a reason")
        return self

    @property
    def can_skip_v1_intent_model(self) -> bool:
        return bool(
            self.status == "ACCEPTED"
            and self.source == SemanticDecisionSource.V2_AUTHORIZED_PLAN
            and not self.clarification_needed
            and len(self.tasks) == 1
            and self.tasks[0].semantic_complete
            and self.tasks[0].question == self.completed_question
        )

    @property
    def can_skip_v1_task_decomposition(self) -> bool:
        """Verifiable single-task evidence carried by a V2 surface handoff.

        Recognition already proved a self-contained NEW_TASK with exactly one
        task and no pending clarification; ASL still owns field binding, so
        only the duplicate V1 planner computation may be skipped while the
        V1 intent model stays authoritative.  ``completed_question ==
        question`` alone is deliberately insufficient: the NEW_TASK relation
        evidence, the single-task shape and the fallback route are all
        required before decomposition may be bypassed.
        """
        return bool(
            self.source == SemanticDecisionSource.V1_SEMANTIC_FALLBACK
            and self.status == "REQUIRES_V1_FALLBACK"
            and not self.clarification_needed
            and self.conversation_state == "NEW_TASK"
            and len(self.tasks) == 1
            and self.tasks[0].question == self.completed_question
        )

    def request_single_task_evidence_mismatch(
        self,
        *,
        message_id: str,
        conversation_id: str,
        application_id: str,
        completed_question: str,
        authorized_scope: AuthorizedSemanticScope,
    ) -> str | None:
        """Scope identity gate accepting surface single-task evidence.

        Mirrors ``request_mismatch_reason`` but does not require a complete
        V2 authorized plan; it accepts ``can_skip_v1_task_decomposition``
        instead, keeping every identity/scope check fail-closed.
        """
        if self.message_id != message_id:
            return "SEMANTIC_DECISION_MESSAGE_MISMATCH"
        if self.conversation_id != conversation_id:
            return "SEMANTIC_DECISION_CONVERSATION_MISMATCH"
        if self.application_id != application_id:
            return "SEMANTIC_DECISION_APPLICATION_MISMATCH"
        if self.completed_question != completed_question:
            return "SEMANTIC_DECISION_QUESTION_MISMATCH"
        if self.scope_proof.authorized_scope != authorized_scope:
            return "SEMANTIC_DECISION_SCOPE_MISMATCH"
        if self.scope_proof.authorized_scope_fingerprint != authorized_scope.fingerprint():
            return "SEMANTIC_DECISION_SCOPE_FINGERPRINT_MISMATCH"
        if not self.can_skip_v1_task_decomposition:
            return self.fallback_reason or "SEMANTIC_DECISION_NOT_EXECUTION_READY"
        return None

    def request_mismatch_reason(
        self,
        *,
        message_id: str,
        conversation_id: str,
        application_id: str,
        completed_question: str,
        authorized_scope: AuthorizedSemanticScope,
    ) -> str | None:
        if self.message_id != message_id:
            return "SEMANTIC_DECISION_MESSAGE_MISMATCH"
        if self.conversation_id != conversation_id:
            return "SEMANTIC_DECISION_CONVERSATION_MISMATCH"
        if self.application_id != application_id:
            return "SEMANTIC_DECISION_APPLICATION_MISMATCH"
        if self.completed_question != completed_question:
            return "SEMANTIC_DECISION_QUESTION_MISMATCH"
        if self.scope_proof.authorized_scope != authorized_scope:
            return "SEMANTIC_DECISION_SCOPE_MISMATCH"
        if self.scope_proof.authorized_scope_fingerprint != authorized_scope.fingerprint():
            return "SEMANTIC_DECISION_SCOPE_FINGERPRINT_MISMATCH"
        if not self.can_skip_v1_intent_model:
            return self.fallback_reason or "SEMANTIC_DECISION_NOT_EXECUTION_READY"
        return None
