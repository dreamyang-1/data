"""Strict Pydantic source of truth for TypedLogicalPlan v0.2.

The models in this module are a shadow-only contract.  They deliberately do
not import or mutate the legacy production request models.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    AdapterStatus,
    AnalysisGoal,
    CalendarType,
    CandidateRejectionReason,
    CatalogType,
    DialogueAct,
    ErrorType,
    FilterOperator,
    MissingPeriodPolicy,
    Presence,
    ProofStatus,
    QueryShape,
    ResolutionStatus,
    SemanticRole,
    ServiceRoute,
    SlotOperationType,
    TimeGrain,
)


class StrictModel(BaseModel):
    """Base model used by every closed V2 contract object."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class DialogueSpec(StrictModel):
    """Resolved dialogue action and its deterministic routing evidence."""

    act: DialogueAct
    explicit: bool = True
    evidence_mention_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    reason_codes: list[str] = Field(default_factory=list, max_length=50)


class TopicTarget(StrictModel):
    """Target topic/task selected for this turn."""

    topic_id: Identifier | None = None
    task_id: Identifier | None = None
    candidate_topic_ids: list[Identifier] = Field(default_factory=list, max_length=10)


class SemanticAxes(StrictModel):
    """Independent intent axes; legacy primary intent is not authoritative."""

    service_route: ServiceRoute
    analysis_goals: list[AnalysisGoal] = Field(default_factory=list, max_length=20)
    query_shape: QueryShape
    legacy_primary_intent: str | None = Field(default=None, max_length=100)


class Mention(StrictModel):
    """Unicode code-point span in the current user utterance."""

    mention_id: Identifier
    surface: str = Field(min_length=1, max_length=1000)
    normalized_surface: str = Field(min_length=1, max_length=1000)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    candidate_roles: list[SemanticRole] = Field(min_length=1, max_length=20)
    explicit: bool = True
    negated: bool = False
    clause_id: Identifier | None = None
    coordination_group_id: Identifier | None = None
    modifier_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    source_turn_id: Identifier

    @model_validator(mode="after")
    def validate_interval(self) -> "Mention":
        """Reject empty or reversed half-open spans."""

        if self.end_char <= self.start_char:
            raise ValueError("mention span must be a non-empty half-open interval")
        return self


class SemanticRef(StrictModel):
    """One canonical catalog item bound to a query role."""

    catalog_type: CatalogType
    semantic_role: SemanticRole
    canonical_id: Identifier
    canonical_code: Identifier
    display_name: str = Field(min_length=1, max_length=500)
    catalog_version: Identifier
    semantic_model_id: Identifier
    business_domain_ids: list[Identifier] = Field(min_length=1, max_length=50)
    mention_id: Identifier | None = None
    resolution_status: ResolutionStatus
    resolution_source: Identifier


class SemanticCandidate(StrictModel):
    """Auditable candidate retained before global plan selection."""

    candidate_id: Identifier
    mention_id: Identifier
    candidate_role: SemanticRole
    catalog_type: CatalogType
    canonical_id: Identifier
    canonical_code: Identifier
    display_name: str = Field(min_length=1, max_length=500)
    retrieval_method: Identifier
    raw_score: float
    normalized_score: float = Field(ge=0, le=1)
    exact_match: bool = False
    alias_match: bool = False
    field_ownership_valid: bool | None = None
    relationship_reachable: bool | None = None
    metric_dimension_compatible: bool | None = None
    permission_allowed: bool | None = None
    catalog_version: Identifier
    status: ResolutionStatus
    rejection_reasons: list[CandidateRejectionReason] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list, max_length=50)


class SemanticCandidateSet(StrictModel):
    """N-best candidates for one mention across role hypotheses."""

    mention_id: Identifier
    candidates: list[SemanticCandidate] = Field(min_length=1, max_length=100)
    selected_candidate_id: Identifier | None = None
    status: ResolutionStatus


class PlanCandidateScore(StrictModel):
    """Deterministic and retrieval components of a complete plan score."""

    retrieval_score: float = Field(ge=0, le=1)
    constraint_score: float = Field(ge=0, le=1)
    permission_score: float = Field(ge=0, le=1)
    executability_score: float = Field(ge=0, le=1)
    total_score: float = Field(ge=0, le=1)
    model_self_reported_confidence: float | None = Field(default=None, ge=0, le=1)
    calibrated_probability: None = None


class PlanCandidate(StrictModel):
    """One globally constrained plan candidate built only from catalog IDs."""

    plan_candidate_id: Identifier
    semantic_candidate_ids: list[Identifier] = Field(min_length=1, max_length=500)
    score: PlanCandidateScore
    executable: bool
    rejection_reasons: list[CandidateRejectionReason] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list, max_length=100)


class SemanticResolutionContract(StrictModel):
    """Complete N-best resolution result for one shadow planning request."""

    schema_version: Literal["2.0"] = "2.0"
    candidate_sets: list[SemanticCandidateSet] = Field(default_factory=list, max_length=500)
    plan_candidates: list[PlanCandidate] = Field(default_factory=list, max_length=100)
    selected_plan_candidate_id: Identifier | None = None
    status: ResolutionStatus


class SlotOperation(StrictModel):
    """Deterministic task-state patch produced for one slot path."""

    operation_id: Identifier
    slot_path: str = Field(min_length=1, max_length=500)
    operation: SlotOperationType
    old_value: Any = None
    new_value: Any = None
    target_item_id: Identifier | None = None
    evidence_mention_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    source: Identifier
    reason_code: Identifier
    base_task_version: int = Field(ge=0)
    presence: Presence

    @model_validator(mode="after")
    def validate_clear_presence(self) -> "SlotOperation":
        """Make an explicit clear durable and unambiguous."""

        if self.operation == SlotOperationType.CLEAR:
            if self.presence != Presence.EXPLICITLY_CLEARED:
                raise ValueError("CLEAR requires EXPLICITLY_CLEARED presence")
            if self.new_value not in (None, [], {}):
                raise ValueError("CLEAR cannot provide a non-empty new_value")
        return self


class StringValue(StrictModel):
    value_type: Literal["STRING"] = "STRING"
    value: str


class NumberValue(StrictModel):
    value_type: Literal["NUMBER"] = "NUMBER"
    value: float


class BooleanValue(StrictModel):
    value_type: Literal["BOOLEAN"] = "BOOLEAN"
    value: bool


class DateValue(StrictModel):
    value_type: Literal["DATE"] = "DATE"
    value: date


class DateTimeValue(StrictModel):
    value_type: Literal["DATETIME"] = "DATETIME"
    value: datetime


class EnumValue(StrictModel):
    value_type: Literal["ENUM"] = "ENUM"
    value: str
    enum_code: Identifier


class EntityValueRef(StrictModel):
    value_type: Literal["ENTITY_REF"] = "ENTITY_REF"
    canonical_id: Identifier
    canonical_code: Identifier
    display_name: str = Field(min_length=1, max_length=500)


class NullValue(StrictModel):
    value_type: Literal["NULL"] = "NULL"
    value: None = None


ScalarFilterValue = Annotated[
    Union[StringValue, NumberValue, BooleanValue, DateValue, DateTimeValue, EnumValue, EntityValueRef, NullValue],
    Field(discriminator="value_type"),
]


class ListValue(StrictModel):
    value_type: Literal["LIST"] = "LIST"
    values: list[ScalarFilterValue] = Field(min_length=1, max_length=1000)


class RangeValue(StrictModel):
    value_type: Literal["RANGE"] = "RANGE"
    start: ScalarFilterValue
    end: ScalarFilterValue


TypedFilterValue = Annotated[
    Union[StringValue, NumberValue, BooleanValue, DateValue, DateTimeValue, EnumValue, EntityValueRef, ListValue, RangeValue, NullValue],
    Field(discriminator="value_type"),
]


class Predicate(StrictModel):
    node_type: Literal["PREDICATE"] = "PREDICATE"
    field_ref: SemanticRef
    operator: FilterOperator
    value: TypedFilterValue
    source: Identifier
    mention_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    scope: Identifier
    validation_status: ProofStatus = ProofStatus.UNKNOWN

    @model_validator(mode="after")
    def validate_null_operators(self) -> "Predicate":
        """Keep null operators and value representation consistent."""

        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if not isinstance(self.value, NullValue):
                raise ValueError("null operators require NullValue")
        return self


class BooleanFilterGroup(StrictModel):
    node_type: Literal["BOOLEAN_GROUP"] = "BOOLEAN_GROUP"
    operator: Literal["AND", "OR", "NOT"]
    children: list[Union[Predicate, "BooleanFilterGroup"]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_not_arity(self) -> "BooleanFilterGroup":
        """NOT is unary; AND/OR retain one or more explicit children."""

        if self.operator == "NOT" and len(self.children) != 1:
            raise ValueError("NOT filter group requires exactly one child")
        return self


FilterExpression = Annotated[Union[Predicate, BooleanFilterGroup], Field(discriminator="node_type")]


class TimeRange(StrictModel):
    start: datetime
    end_exclusive: datetime

    @model_validator(mode="after")
    def validate_range(self) -> "TimeRange":
        """Require a non-empty interval and timezone-aware bounds."""

        if self.start.tzinfo is None or self.end_exclusive.tzinfo is None:
            raise ValueError("time range boundaries must include timezone information")
        if self.end_exclusive <= self.start:
            raise ValueError("end_exclusive must be later than start")
        return self


class TimeComparison(StrictModel):
    comparison_type: Identifier
    comparison_range: TimeRange


class TimeSpec(StrictModel):
    anchor: SemanticRef
    range: TimeRange
    grain: TimeGrain
    boundary: Literal["LEFT_CLOSED_RIGHT_OPEN"] = "LEFT_CLOSED_RIGHT_OPEN"
    timezone: str = Field(min_length=1, max_length=100)
    calendar: CalendarType = CalendarType.NATURAL
    source: Identifier
    as_of: datetime
    data_watermark: datetime | None = None
    include_incomplete_period: bool = False
    missing_period_policy: MissingPeriodPolicy = MissingPeriodPolicy.LEAVE_MISSING
    comparison: TimeComparison | None = None


class RankingSpec(StrictModel):
    rank_by: SemanticRef
    direction: Literal["ASC", "DESC"]
    limit: int = Field(ge=1, le=10000)
    ties_policy: Literal["EXCLUDE_TIES", "INCLUDE_TIES"] = "EXCLUDE_TIES"
    nulls_policy: Literal["FIRST", "LAST", "EXCLUDE"] = "EXCLUDE"
    stable_tiebreakers: list[SemanticRef] = Field(default_factory=list, max_length=20)
    preserve_existing_order: bool = False


class LimitSpec(StrictModel):
    limit: int = Field(ge=1, le=10000)
    preserve_existing_order: bool = True


class ComparisonSpec(StrictModel):
    comparison_type: Identifier
    baseline: str = Field(min_length=1, max_length=500)
    current_period: TimeRange | None = None
    comparison_period: TimeRange | None = None
    calculation: Identifier
    output_metrics: list[SemanticRef] = Field(min_length=1, max_length=20)


class RowBounds(StrictModel):
    minimum: int = Field(default=0, ge=0)
    maximum: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "RowBounds":
        """Ensure maximum is not below minimum."""

        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("row bound maximum must not be below minimum")
        return self


class NumericConstraint(StrictModel):
    column: Identifier
    finite_only: bool = True
    minimum: float | None = None
    maximum: float | None = None


class ResultContract(StrictModel):
    required_metric_refs: list[SemanticRef] = Field(default_factory=list, max_length=100)
    required_dimension_refs: list[SemanticRef] = Field(default_factory=list, max_length=100)
    required_entity_refs: list[SemanticRef] = Field(default_factory=list, max_length=100)
    required_columns: list[str] = Field(default_factory=list, max_length=500)
    required_time_grain: TimeGrain | None = None
    required_ordering: list[SemanticRef] = Field(default_factory=list, max_length=50)
    row_bounds: RowBounds = Field(default_factory=RowBounds)
    uniqueness_keys: list[list[str]] = Field(default_factory=list, max_length=50)
    expected_cardinality: str | None = Field(default=None, max_length=100)
    allow_empty: bool = True
    allow_truncated: bool = False
    snapshot_requirement: Identifier | None = None
    watermark_requirement: Identifier | None = None
    quality_requirement: Identifier | None = None
    numeric_constraints: list[NumericConstraint] = Field(default_factory=list, max_length=100)
    proof_requirements: list[Identifier] = Field(default_factory=list, max_length=100)


class ContractProof(StrictModel):
    status: ProofStatus
    checks: dict[str, ProofStatus] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=100)


class ProofChain(StrictModel):
    plan: ContractProof
    asl: ContractProof
    sql_plan: ContractProof
    result: ContractProof


class AdapterReport(StrictModel):
    adapter_status: AdapterStatus
    dropped_features: list[str] = Field(default_factory=list, max_length=100)
    approximated_features: list[str] = Field(default_factory=list, max_length=100)
    post_processing_required: list[str] = Field(default_factory=list, max_length=100)
    validation_errors: list[str] = Field(default_factory=list, max_length=100)
    can_execute_safely: bool

    @model_validator(mode="after")
    def validate_safety(self) -> "AdapterReport":
        """Unsafe or unsupported loss can never be executable."""

        if self.adapter_status in {AdapterStatus.LOSSY_UNSAFE, AdapterStatus.UNSUPPORTED}:
            if self.can_execute_safely:
                raise ValueError("unsafe or unsupported adapter output cannot execute")
        return self


class ClarificationDecision(StrictModel):
    reason_type: ErrorType
    blocking: bool
    affected_plan_paths: list[str] = Field(default_factory=list, max_length=50)
    candidate_answers: list[str] = Field(default_factory=list, max_length=20)
    information_gain: float = Field(default=0, ge=0)
    already_asked: bool = False
    asked_slots: list[str] = Field(default_factory=list, max_length=50)
    fallback_available: bool = False
    assumption_available: bool = False
    base_task_version: int = Field(ge=0)
    state_version: int = Field(ge=0)
    create_pending: bool = False

    @model_validator(mode="after")
    def enforce_user_ambiguity_only(self) -> "ClarificationDecision":
        """Only genuine user ambiguity may create a Pending clarification."""

        if self.create_pending and self.reason_type != ErrorType.USER_AMBIGUITY:
            raise ValueError("only USER_AMBIGUITY may create pending clarification")
        if self.already_asked and self.create_pending:
            raise ValueError("the same clarification slot must not be asked twice")
        return self


class PermissionContext(StrictModel):
    tenant_id: Identifier
    user_id: Identifier
    application_id: Identifier
    allowed_business_domain_ids: list[Identifier] = Field(default_factory=list, max_length=100)


class SnapshotContext(StrictModel):
    catalog_version: Identifier
    semantic_model_version: Identifier
    data_snapshot_id: Identifier | None = None
    data_watermark: datetime | None = None


class VersionMetadata(StrictModel):
    prompt_version: Identifier
    policy_version: Identifier
    adapter_version: Identifier
    plan_schema_version: Literal["0.2"] = "0.2"


class Provenance(StrictModel):
    current_turn_text: str = Field(min_length=1, max_length=8000)
    current_turn_id: Identifier
    source: Literal["REAL", "MOCK", "PARTIAL", "UNAVAILABLE"]
    digests: dict[str, str] = Field(default_factory=dict)


class BasePayload(StrictModel):
    filters: FilterExpression | None = None
    projections: list[SemanticRef] = Field(default_factory=list, max_length=100)


class ControlPayload(StrictModel):
    payload_type: Literal["CONTROL"] = "CONTROL"
    control_action: DialogueAct


class ChatPayload(StrictModel):
    payload_type: Literal["CHAT"] = "CHAT"
    response_mode: Literal["LIGHTWEIGHT"] = "LIGHTWEIGHT"


class ScalarAggregatePayload(BasePayload):
    payload_type: Literal["SCALAR_AGGREGATE"] = "SCALAR_AGGREGATE"
    measures: list[SemanticRef] = Field(min_length=1, max_length=50)
    time: TimeSpec | None = None


class GroupedAggregatePayload(ScalarAggregatePayload):
    payload_type: Literal["GROUPED_AGGREGATE"] = "GROUPED_AGGREGATE"
    group_by: list[SemanticRef] = Field(min_length=1, max_length=50)


class TimeSeriesPayload(GroupedAggregatePayload):
    payload_type: Literal["TIME_SERIES"] = "TIME_SERIES"
    time: TimeSpec

    @model_validator(mode="after")
    def require_time_grain(self) -> "TimeSeriesPayload":
        """A time series cannot use the NONE grain."""

        if self.time.grain == TimeGrain.NONE:
            raise ValueError("time series requires a non-NONE grain")
        return self


class RankingPayload(GroupedAggregatePayload):
    payload_type: Literal["RANKING"] = "RANKING"
    ranking_target: SemanticRef
    ranking: RankingSpec


class ComparisonPayload(GroupedAggregatePayload):
    payload_type: Literal["COMPARISON"] = "COMPARISON"
    comparison: ComparisonSpec


class RelationListPayload(BasePayload):
    payload_type: Literal["RELATION_LIST"] = "RELATION_LIST"
    source_entity: SemanticRef
    target_entity: SemanticRef | None = None
    relation_target: SemanticRef | None = None
    limit: LimitSpec | None = None

    @model_validator(mode="after")
    def require_target(self) -> "RelationListPayload":
        """Require either a target entity or an explicit relation target."""

        if self.target_entity is None and self.relation_target is None:
            raise ValueError("relation list requires target_entity or relation_target")
        return self


class DetailRowsPayload(BasePayload):
    payload_type: Literal["DETAIL_ROWS"] = "DETAIL_ROWS"
    source_entity: SemanticRef
    fields: list[SemanticRef] = Field(min_length=1, max_length=100)
    limit: LimitSpec | None = None


class DatasetTransformPayload(StrictModel):
    payload_type: Literal["DATASET_TRANSFORM"] = "DATASET_TRANSFORM"
    source_dataset_id: Identifier
    operation: Literal["FILTER", "SORT", "LIMIT", "PROJECT", "DRILL_DOWN"]
    filters: FilterExpression | None = None
    ranking: RankingSpec | None = None
    limit: LimitSpec | None = None
    projections: list[SemanticRef] = Field(default_factory=list, max_length=100)


class MetricDefinitionPayload(StrictModel):
    payload_type: Literal["METRIC_DEFINITION"] = "METRIC_DEFINITION"
    metric_refs: list[SemanticRef] = Field(min_length=1, max_length=50)


class MetadataPayload(StrictModel):
    payload_type: Literal["METADATA"] = "METADATA"
    targets: list[SemanticRef] = Field(min_length=1, max_length=100)


class LineagePayload(StrictModel):
    payload_type: Literal["LINEAGE"] = "LINEAGE"
    lineage_target: SemanticRef
    direction: Literal["UPSTREAM", "DOWNSTREAM", "BOTH"] = "BOTH"


class DataQualityPayload(StrictModel):
    payload_type: Literal["DATA_QUALITY"] = "DATA_QUALITY"
    quality_target: SemanticRef
    checks: list[str] = Field(default_factory=list, max_length=50)


class AnomalyPayload(TimeSeriesPayload):
    payload_type: Literal["ANOMALY"] = "ANOMALY"
    method_id: Identifier


class RootCausePayload(TimeSeriesPayload):
    payload_type: Literal["ROOT_CAUSE"] = "ROOT_CAUSE"
    decomposition_dimensions: list[SemanticRef] = Field(min_length=1, max_length=20)


class ForecastPayload(TimeSeriesPayload):
    payload_type: Literal["FORECAST"] = "FORECAST"
    horizon: int = Field(ge=1, le=1000)
    method_id: Identifier | None = None


class ReportPayload(StrictModel):
    payload_type: Literal["REPORT"] = "REPORT"
    source_task_ids: list[Identifier] = Field(min_length=1, max_length=50)
    delivery_target: SemanticRef | None = None


PlanPayload = Annotated[
    Union[
        ControlPayload,
        ChatPayload,
        ScalarAggregatePayload,
        GroupedAggregatePayload,
        TimeSeriesPayload,
        RankingPayload,
        ComparisonPayload,
        RelationListPayload,
        DetailRowsPayload,
        DatasetTransformPayload,
        MetricDefinitionPayload,
        MetadataPayload,
        LineagePayload,
        DataQualityPayload,
        AnomalyPayload,
        RootCausePayload,
        ForecastPayload,
        ReportPayload,
    ],
    Field(discriminator="payload_type"),
]


class PlanEnvelope(StrictModel):
    """Versioned envelope for a single semantic V2 shadow task."""

    schema_version: Literal["0.2"] = "0.2"
    plan_id: Identifier
    conversation_id: Identifier
    message_id: Identifier
    topic_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    base_task_version: int | None = Field(default=None, ge=0)
    conversation_state_version: int = Field(ge=0)
    dialogue: DialogueSpec
    topic_target: TopicTarget
    semantics: SemanticAxes
    mentions: list[Mention] = Field(default_factory=list, max_length=500)
    semantic_bindings: list[SemanticRef] = Field(default_factory=list, max_length=500)
    semantic_candidates: list[SemanticCandidateSet] = Field(default_factory=list, max_length=500)
    slot_operations: list[SlotOperation] = Field(default_factory=list, max_length=500)
    payload: PlanPayload
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    unresolved_items: list[str] = Field(default_factory=list, max_length=100)
    clarification_decision: ClarificationDecision | None = None
    result_contract: ResultContract | None = None
    proof_chain: ProofChain | None = None
    adapter_report: AdapterReport | None = None
    permission_context: PermissionContext
    snapshot_context: SnapshotContext
    version_metadata: VersionMetadata
    provenance: Provenance

    @model_validator(mode="after")
    def validate_mentions_and_scope(self) -> "PlanEnvelope":
        """Validate spans, mention references, and tenant-safe scope."""

        text = self.provenance.current_turn_text
        mention_ids = {item.mention_id for item in self.mentions}
        if len(mention_ids) != len(self.mentions):
            raise ValueError("mention_id values must be unique")
        for mention in self.mentions:
            if mention.end_char > len(text):
                raise ValueError("mention span exceeds current turn text")
            if text[mention.start_char:mention.end_char] != mention.surface:
                raise ValueError("mention span does not match current turn text")
        for binding in self.semantic_bindings:
            if binding.mention_id is not None and binding.mention_id not in mention_ids:
                raise ValueError("semantic binding references an unknown mention")
            if not set(binding.business_domain_ids).issubset(
                set(self.permission_context.allowed_business_domain_ids)
            ):
                raise ValueError("semantic binding is outside permission scope")
        if self.dialogue.act == DialogueAct.REFRESH and self.base_task_version not in (
            None,
            self.task_version,
        ):
            raise ValueError("REFRESH must preserve the task version semantics")
        if self.dialogue.act == DialogueAct.REVISE and (
            self.base_task_version is None or self.task_version <= self.base_task_version
        ):
            raise ValueError("REVISE must create a newer task version")
        return self


BooleanFilterGroup.model_rebuild()
