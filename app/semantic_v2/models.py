"""Strict Pydantic source of truth for TypedLogicalPlan v0.2.

The models in this module are a shadow-only contract.  They deliberately do
not import or mutate the legacy production request models.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Annotated, Any, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from .enums import (Calculation, ComparisonType, ControlAction, DeliveryMode,
                    ExecutionBackend, ExecutionStatus, QualityCheckType, Severity)

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


class FrozenList(list):
    """JSON-compatible immutable list used by finalized logical plans."""
    def _deny(self, *args, **kwargs):
        raise TypeError('finalized logical plan is immutable')
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = reverse = sort = __iadd__ = __imul__ = _deny

    def __deepcopy__(self, memo):
        from copy import deepcopy
        return FrozenList(deepcopy(list(self), memo))


class FrozenDict(dict):
    def _deny(self, *args, **kwargs):
        raise TypeError('finalized logical plan is immutable')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        from copy import deepcopy
        return FrozenDict(deepcopy(dict(self), memo))


def freeze_contract(value):
    """Detach and recursively freeze an artifact; caller-owned inputs stay unchanged."""
    if isinstance(value, BaseModel):
        result = value.model_copy(deep=True)
        for name in type(result).model_fields:
            object.__setattr__(result, name, freeze_contract(getattr(result, name)))
        # Every child is frozen by StrictModel's guarded __setattr__.
        object.__setattr__(result, '_contract_frozen', True)
        return result
    if isinstance(value, (list, tuple)):
        return FrozenList(freeze_contract(v) for v in value) if isinstance(value, list) else tuple(freeze_contract(v) for v in value)
    if isinstance(value, dict):
        return FrozenDict((k, freeze_contract(v)) for k, v in value.items())
    return value


class StrictModel(BaseModel):
    """Base model used by every closed V2 contract object."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    def __setattr__(self, name, value):
        if getattr(self, '_contract_frozen', False):
            raise TypeError('finalized logical plan is immutable')
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if getattr(self, '_contract_frozen', False):
            raise TypeError('finalized logical plan is immutable')
        super().__delattr__(name)

    @field_validator('*', mode='after')
    @classmethod
    def normalize_datetimes(cls, value):
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                raise ValueError('timezone-aware datetime required')
            return value.astimezone(timezone.utc)
        return value


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


class BoundSemanticRef(StrictModel):
    """Accepted catalog identity. Scope is proved again at the plan boundary."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    catalog_type: CatalogType
    semantic_role: SemanticRole
    canonical_id: Identifier
    canonical_code: Identifier
    display_name: str = Field(min_length=1, max_length=500)
    catalog_version: Identifier
    semantic_model_id: Identifier
    # v0.2.2 can represent a verified model-owned global catalog record. The
    # v0.2.1 plan boundary still rejects an empty owned-domain set.
    business_domain_ids: tuple[Identifier, ...] = Field(max_length=50)
    source_mention_ids: tuple[Identifier, ...] = ()
    resolution_source: Identifier

    @model_validator(mode='before')
    @classmethod
    def accept_legacy_python_ref(cls, value):
        # Only the old Python compatibility API can pass an accepted SemanticRef.
        # Raw model JSON cannot smuggle a resolution_status into a bound reference.
        if isinstance(value, SemanticRef):
            if value.resolution_status != ResolutionStatus.ACCEPTED:
                raise ValueError('only accepted references can be bound')
            data = value.model_dump()
            data.pop('resolution_status')
            mention_id = data.pop('mention_id')
            data['source_mention_ids'] = [mention_id] if mention_id else []
            return data
        return value


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

    @model_validator(mode='after')
    def validate_selection(self):
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids) or any(c.mention_id != self.mention_id for c in self.candidates):
            raise ValueError('candidate identities must be unique and match their mention')
        selected = next((c for c in self.candidates if c.candidate_id == self.selected_candidate_id), None)
        if self.status == ResolutionStatus.ACCEPTED:
            if selected is None or selected.status != ResolutionStatus.ACCEPTED or selected.permission_allowed is not True or selected.validation_errors or selected.rejection_reasons:
                raise ValueError('accepted candidate set requires an authorized accepted member')
        elif self.selected_candidate_id is not None:
            raise ValueError('unaccepted candidate set cannot select a candidate')
        return self


class PlanCandidateScore(StrictModel):
    """Deterministic and retrieval components of a complete plan score."""

    retrieval_score: float = Field(ge=0, le=1)
    constraint_score: float = Field(ge=0, le=1)
    permission_score: float = Field(ge=0, le=1)
    executability_score: float = Field(ge=0, le=1)
    scoring_version: Literal['weighted-v1'] = 'weighted-v1'
    weights_version: Literal['equal-v1'] = 'equal-v1'
    model_self_reported_confidence: float | None = Field(default=None, ge=0, le=1)
    calibrated_probability: None = None

    @property
    def total_score(self) -> float:
        return sum((self.retrieval_score, self.constraint_score,
                    self.permission_score, self.executability_score)) / 4


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

    @model_validator(mode='after')
    def validate_global_selection(self):
        candidates = [c for s in self.candidate_sets for c in s.candidates]
        by_id = {c.candidate_id: c for c in candidates}
        if len(by_id) != len(candidates):
            raise ValueError('candidate IDs must be globally unique')
        plan_ids = [p.plan_candidate_id for p in self.plan_candidates]
        if len(set(plan_ids)) != len(plan_ids):
            raise ValueError('plan candidate IDs must be unique')
        for plan in self.plan_candidates:
            if any(i not in by_id for i in plan.semantic_candidate_ids):
                raise ValueError('plan candidate references nonexistent candidate')
        selected = next((p for p in self.plan_candidates if p.plan_candidate_id == self.selected_plan_candidate_id), None)
        if self.status == ResolutionStatus.ACCEPTED:
            if selected is None or not selected.executable or selected.validation_errors or selected.rejection_reasons:
                raise ValueError('accepted resolution requires an executable selected plan')
            for identifier in selected.semantic_candidate_ids:
                c = by_id[identifier]
                if c.permission_allowed is not True or c.status != ResolutionStatus.ACCEPTED or c.validation_errors or c.rejection_reasons:
                    raise ValueError('selected plan contains an unauthorized or rejected candidate')
        elif self.selected_plan_candidate_id is not None:
            raise ValueError('unresolved contract cannot select a plan')
        return self


class SlotOperation(StrictModel):
    """Deterministic task-state patch produced for one slot path."""

    operation_id: Identifier
    slot_path: str = Field(min_length=1, max_length=500)
    operation: SlotOperationType
    old_value: JsonValue = None
    new_value: JsonValue = None
    target_item_id: Identifier | None = None
    evidence_mention_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    source: Literal['CURRENT_EXPLICIT', 'USER_EXPLICIT', 'CURRENT_REFERENCE_RESOLUTION', 'HISTORY', 'SYSTEM_DEFAULT', 'FIXTURE']
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
    value: Decimal = Field(allow_inf_nan=False)


class BooleanValue(StrictModel):
    value_type: Literal["BOOLEAN"] = "BOOLEAN"
    value: bool


class DateValue(StrictModel):
    value_type: Literal["DATE"] = "DATE"
    value: date


class DateTimeValue(StrictModel):
    value_type: Literal["DATETIME"] = "DATETIME"
    value: AwareDatetime


class EnumValue(StrictModel):
    value_type: Literal["ENUM"] = "ENUM"
    value: str
    enum_code: Identifier


class EntityValueRef(StrictModel):
    value_type: Literal["ENTITY_REF"] = "ENTITY_REF"
    ref: BoundSemanticRef


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

    @model_validator(mode='after')
    def compatible_values(self):
        types = {v.value_type for v in self.values}
        if len(types) != 1:
            raise ValueError('list values must have one type; NULL cannot mix with ordinary values')
        return self


class RangeValue(StrictModel):
    value_type: Literal["RANGE"] = "RANGE"
    start: ScalarFilterValue
    end: ScalarFilterValue

    @model_validator(mode='after')
    def compatible_bounds(self):
        if type(self.start) is not type(self.end) or not isinstance(self.start, (StringValue, NumberValue, DateValue, DateTimeValue)):
            raise ValueError('range bounds must be comparable scalars of the same type')
        if self.start.value > self.end.value:
            raise ValueError('range start must not exceed end')
        return self


TypedFilterValue = Annotated[
    Union[StringValue, NumberValue, BooleanValue, DateValue, DateTimeValue, EnumValue, EntityValueRef, ListValue, RangeValue, NullValue],
    Field(discriminator="value_type"),
]


class Predicate(StrictModel):
    node_type: Literal["PREDICATE"] = "PREDICATE"
    field_ref: BoundSemanticRef
    operator: FilterOperator
    value: TypedFilterValue
    source: Identifier
    mention_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    scope: Identifier
    validation_status: ProofStatus = ProofStatus.UNKNOWN

    @model_validator(mode="after")
    def validate_null_operators(self) -> "Predicate":
        """Keep null operators and value representation consistent."""

        if self.operator in {FilterOperator.AND, FilterOperator.OR, FilterOperator.NOT}:
            raise ValueError('Boolean operators require BooleanFilterGroup')
        expected = {
            FilterOperator.IN: (ListValue,), FilterOperator.NOT_IN: (ListValue,),
            FilterOperator.BETWEEN: (RangeValue,),
            FilterOperator.LIKE: (StringValue,), FilterOperator.NOT_LIKE: (StringValue,),
            FilterOperator.IS_NULL: (NullValue,), FilterOperator.IS_NOT_NULL: (NullValue,),
            **{op: (StringValue, NumberValue, DateValue, DateTimeValue) for op in
               (FilterOperator.GT, FilterOperator.GTE, FilterOperator.LT, FilterOperator.LTE)},
            FilterOperator.EQ: (StringValue, NumberValue, BooleanValue, DateValue, DateTimeValue, EnumValue, EntityValueRef),
            FilterOperator.NE: (StringValue, NumberValue, BooleanValue, DateValue, DateTimeValue, EnumValue, EntityValueRef),
        }[self.operator]
        if not isinstance(self.value, expected):
            raise ValueError('filter operator/value type mismatch')
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
    start: AwareDatetime
    end_exclusive: AwareDatetime

    @model_validator(mode="after")
    def validate_range(self) -> "TimeRange":
        """Require a non-empty interval and timezone-aware bounds."""

        if self.start.tzinfo is None or self.end_exclusive.tzinfo is None:
            raise ValueError("time range boundaries must include timezone information")
        if self.end_exclusive <= self.start:
            raise ValueError("end_exclusive must be later than start")
        return self


class TimeComparison(StrictModel):
    comparison_type: ComparisonType
    comparison_range: TimeRange


class TimeSpec(StrictModel):
    anchor: BoundSemanticRef
    range: TimeRange | None
    grain: TimeGrain
    boundary: Literal["LEFT_CLOSED_RIGHT_OPEN"] = "LEFT_CLOSED_RIGHT_OPEN"
    timezone: str = Field(min_length=1, max_length=100)
    calendar: CalendarType = CalendarType.NATURAL
    source: Identifier
    as_of: AwareDatetime
    data_watermark: AwareDatetime | None = None
    fiscal_calendar_id: Identifier | None = None
    calendar_policy_version: Identifier | None = None
    default_policy_id: Identifier | None = None
    default_policy_version: Identifier | None = None
    include_incomplete_period: bool = False
    missing_period_policy: MissingPeriodPolicy = MissingPeriodPolicy.LEAVE_MISSING
    comparison: TimeComparison | None = None

    @model_validator(mode='after')
    def validate_policies(self):
        if (self.range is None) != (self.source == 'USER_EXPLICIT_UNBOUNDED'):
            raise ValueError('unbounded time requires explicit cleared-range provenance')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError('valid IANA timezone required') from exc
        if self.calendar == CalendarType.FISCAL and not (self.fiscal_calendar_id and self.calendar_policy_version):
            raise ValueError('fiscal calendar requires governed policy identity/version')
        if self.source == 'SYSTEM_DEFAULT' and not (self.default_policy_id and self.default_policy_version):
            raise ValueError('system default time requires policy identity/version')
        return self


def require_bounded_legacy_time(value):
    """0.2.1 remains bounded; explicit unbounded time belongs to scoped 0.2.2."""
    if isinstance(value, TimeSpec) and value.range is None:
        raise ValueError('PLAN_VALIDATION_FAILURE: unbounded time requires scoped 0.2.2')
    if isinstance(value, StrictModel):
        for name in type(value).model_fields:
            require_bounded_legacy_time(getattr(value, name))
    elif isinstance(value, dict):
        for item in value.values():
            require_bounded_legacy_time(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_bounded_legacy_time(item)


class RankingSpec(StrictModel):
    rank_by: BoundSemanticRef
    direction: Literal["ASC", "DESC"]
    limit: int = Field(ge=1, le=10000)
    ties_policy: Literal["EXCLUDE_TIES", "INCLUDE_TIES"] = "EXCLUDE_TIES"
    nulls_policy: Literal["FIRST", "LAST", "EXCLUDE"] = "EXCLUDE"
    stable_tiebreakers: list[BoundSemanticRef] = Field(default_factory=list, max_length=20)


class LimitSpec(StrictModel):
    limit: int = Field(ge=1, le=10000)
    preserve_existing_order: bool = True


class TimeBaseline(StrictModel):
    baseline_type: Literal['TIME'] = 'TIME'
    range: TimeRange | None = None
    relative_period: Literal['PREVIOUS_YEAR', 'PREVIOUS_PERIOD'] | None = None

    @model_validator(mode='after')
    def one_reference(self):
        if (self.range is None) == (self.relative_period is None):
            raise ValueError('time baseline requires exactly one explicit or relative period')
        return self


class TaskBaseline(StrictModel):
    baseline_type: Literal['TASK'] = 'TASK'
    task_id: Identifier
    task_version: int = Field(ge=1)


class DatasetBaseline(StrictModel):
    baseline_type: Literal['DATASET'] = 'DATASET'
    dataset_id: Identifier


class SemanticBaseline(StrictModel):
    baseline_type: Literal['SEMANTIC'] = 'SEMANTIC'
    ref: BoundSemanticRef


ComparisonBaseline = Annotated[Union[TimeBaseline, TaskBaseline, DatasetBaseline, SemanticBaseline], Field(discriminator='baseline_type')]


class ComputedExpression(StrictModel):
    calculation: Calculation
    baseline: ComparisonBaseline | None = None


class ComputedMeasureRef(StrictModel):
    local_id: Identifier
    display_name: str
    computation_type: Literal['DELTA', 'GROWTH_RATE', 'SHARE', 'CONTRIBUTION', 'ANOMALY_SCORE', 'FORECAST', 'FORECAST_LOWER', 'FORECAST_UPPER']
    source_measure_refs: list[BoundSemanticRef] = Field(min_length=1)
    expression_spec: ComputedExpression
    unit: Identifier
    format_rule: Literal['NUMBER', 'CURRENCY', 'PERCENT']
    provenance: list[Identifier] = Field(min_length=1)


class ComparisonSpec(StrictModel):
    comparison_type: ComparisonType
    baseline: ComparisonBaseline
    current_period: TimeRange | None = None
    comparison_period: TimeRange | None = None
    calculation: Calculation
    output_metrics: list[BoundSemanticRef | ComputedMeasureRef] = Field(min_length=1, max_length=20)

    @model_validator(mode='before')
    @classmethod
    def legacy_python_comparison(cls, value):
        # Explicit finite compatibility mapping; unknown strings remain invalid.
        if isinstance(value, dict) and any(isinstance(r, SemanticRef) for r in value.get('output_metrics', [])):
            value = dict(value)
            if value.get('baseline') == 'previous_year':
                value['baseline'] = TimeBaseline(relative_period='PREVIOUS_YEAR')
            if value.get('calculation') == 'RATE':
                value['calculation'] = Calculation.GROWTH_RATE
        return value


class ChartPreferences(StrictModel):
    chart_type: Literal['LINE', 'BAR', 'PIE', 'SCATTER', 'AUTO'] = 'AUTO'


class DeliverySpec(StrictModel):
    modes: list[DeliveryMode] = Field(default_factory=lambda: [DeliveryMode.TABLE], min_length=1)
    export_format: Literal['CSV', 'XLSX', 'PDF', 'DOCX'] | None = None
    report_template_id: Identifier | None = None
    chart_preferences: ChartPreferences | None = None

    @model_validator(mode='after')
    def compatible_delivery(self):
        if self.export_format is not None and DeliveryMode.EXPORT not in self.modes:
            raise ValueError('export format requires EXPORT delivery')
        if self.report_template_id is not None and DeliveryMode.REPORT not in self.modes:
            raise ValueError('report template requires REPORT delivery')
        return self


class ProjectionItem(StrictModel):
    output_field_id: Identifier
    ref: BoundSemanticRef | ComputedMeasureRef
    role: SemanticRole
    display_label: str | None = None
    alias_policy: Literal['STABLE_ID', 'GOVERNED_ALIAS'] = 'STABLE_ID'
    position: int = Field(ge=0)


class ProjectionSpec(StrictModel):
    mode: Literal['EXPLICIT', 'SEMANTIC_DEFAULT'] = 'EXPLICIT'
    items: list[ProjectionItem] = Field(default_factory=list, max_length=100)
    default_display_policy_id: Identifier | None = None

    @model_validator(mode='after')
    def consistent_projection(self):
        if self.mode == 'SEMANTIC_DEFAULT' and not self.default_display_policy_id:
            raise ValueError('semantic default projection requires catalog display policy')
        if len({i.output_field_id for i in self.items}) != len(self.items):
            raise ValueError('projection output identities must be unique')
        if [i.position for i in self.items] != list(range(len(self.items))):
            raise ValueError('projection positions must match the declared order')
        return self


class RelationshipSpec(StrictModel):
    relation_ref: BoundSemanticRef
    source_ref: BoundSemanticRef
    target_ref: BoundSemanticRef
    cardinality: Literal['ONE_TO_ONE', 'ONE_TO_MANY', 'MANY_TO_ONE', 'MANY_TO_MANY']


class ScopePolicyDecision(StrictModel):
    policy_id: Identifier
    policy_version: Identifier
    scope: Literal['CURRENT_DATASET', 'GLOBAL', 'EXPLICIT_TIME', 'DEFAULT_TIME']
    outcome: Literal['ALLOW', 'DENY', 'REQUERY', 'REQUIRE_USER_DECISION']


class OutputFieldRequirement(StrictModel):
    output_field_id: Identifier
    semantic_ref: BoundSemanticRef | ComputedMeasureRef | None = None
    logical_role: SemanticRole
    expected_data_type: Literal['STRING', 'DECIMAL', 'BOOLEAN', 'DATE', 'DATETIME'] | None = None
    display_label: str | None = None
    required: bool = True


class OrderingRequirement(StrictModel):
    output_field_id: Identifier
    ref: BoundSemanticRef | None = None
    direction: Literal['ASC', 'DESC']
    nulls_policy: Literal['FIRST', 'LAST', 'EXCLUDE'] = 'EXCLUDE'
    ties_policy: Literal['EXCLUDE_TIES', 'INCLUDE_TIES'] = 'EXCLUDE_TIES'


class CardinalityExpectation(StrictModel):
    kind: Literal['SCALAR', 'GROUPS', 'DETAIL_ROWS', 'DISTINCT_TARGETS', 'TIME_PERIODS']
    unique_output_field_ids: list[Identifier] = Field(default_factory=list)


class ProofRequirement(StrictModel):
    check_id: Identifier
    severity: Severity = Severity.BLOCKING


class ProofCheck(StrictModel):
    check_id: Identifier
    status: ProofStatus
    severity: Severity = Severity.BLOCKING
    evidence_ids: list[Identifier] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class OutputBindingProof(StrictModel):
    output_field_id: Identifier
    asl_projection_id: Identifier
    sql_alias: Identifier
    result_column_index: int = Field(ge=0)
    result_column_name: Identifier
    status: ProofStatus
    semantic_fingerprint: Identifier


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
    minimum: Decimal | None = Field(default=None, allow_inf_nan=False)
    maximum: Decimal | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode='after')
    def ordered_bounds(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('numeric minimum exceeds maximum')
        return self


class ResultContract(StrictModel):
    semantic_fingerprint: Identifier | None = None
    required_outputs: list[OutputFieldRequirement] = Field(default_factory=list)
    required_metric_refs: list[BoundSemanticRef] = Field(default_factory=list, max_length=100)
    required_dimension_refs: list[BoundSemanticRef] = Field(default_factory=list, max_length=100)
    required_entity_refs: list[BoundSemanticRef] = Field(default_factory=list, max_length=100)
    required_columns: list[str] = Field(default_factory=list, max_length=500)
    required_time_grain: TimeGrain | None = None
    required_ordering: list[OrderingRequirement] = Field(default_factory=list, max_length=50)
    row_bounds: RowBounds = Field(default_factory=RowBounds)
    uniqueness_keys: list[list[str]] = Field(default_factory=list, max_length=50)
    expected_cardinality: CardinalityExpectation | None = None
    allow_empty: bool = True
    allow_truncated: bool = False
    snapshot_requirement: Identifier | None = None
    watermark_requirement: Identifier | None = None
    quality_requirement: Identifier | None = None
    numeric_constraints: list[NumericConstraint] = Field(default_factory=list, max_length=100)
    proof_requirements: list[ProofRequirement] = Field(default_factory=list, max_length=100)


class ContractProof(StrictModel):
    status: ProofStatus
    checks: list[ProofCheck] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=100)

    @field_validator('checks', mode='before')
    @classmethod
    def legacy_check_map(cls, value):
        if isinstance(value, dict):
            return [ProofCheck(check_id=k, status=v) for k, v in value.items()]
        return value

    @model_validator(mode='after')
    def check_status(self):
        if len({c.check_id for c in self.checks}) != len(self.checks):
            raise ValueError('duplicate proof checks')
        if self.status == ProofStatus.PASS and (self.errors or any(c.status != ProofStatus.PASS for c in self.checks if c.severity == Severity.BLOCKING)):
            raise ValueError('PASS cannot hide a blocking failure or unknown proof')
        return self

    @property
    def warnings(self):
        return [c.check_id for c in self.checks if c.severity == Severity.ADVISORY and c.status != ProofStatus.PASS]


class CompensationStep(StrictModel):
    feature: Identifier
    implementation_id: Identifier
    implementation_version: Identifier
    precondition_check_ids: list[Identifier] = Field(min_length=1)
    proof_check_ids: list[Identifier] = Field(min_length=1)


class CompensationPlan(StrictModel):
    steps: list[CompensationStep] = Field(default_factory=list)
    verified_checks: list[ProofCheck] = Field(default_factory=list)


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
    compensation_plan: CompensationPlan | None = None

    @model_validator(mode="after")
    def validate_safety(self) -> "AdapterReport":
        """Unsafe or unsupported loss can never be executable."""

        if self.adapter_status in {AdapterStatus.LOSSY_UNSAFE, AdapterStatus.UNSUPPORTED}:
            if self.can_execute_safely:
                raise ValueError("unsafe or unsupported adapter output cannot execute")
        if self.adapter_status == AdapterStatus.LOSSY_COMPENSATED and self.can_execute_safely:
            if self.compensation_plan is None or not self.compensation_plan.steps:
                raise ValueError('safe compensation requires an implemented, proven compensation plan')
            # No ASL compensation implementation is shipped in this phase.
            raise ValueError('no governed legacy compensation implementation is available')
        return self


class ReadinessBlocker(StrictModel):
    blocker_id: Identifier
    plan_path: Identifier
    blocker_type: ErrorType
    source_stage: Literal['PARSE', 'RESOLUTION', 'PLAN', 'PERMISSION', 'DATA', 'ASL', 'SQL', 'RESULT']
    severity: Severity = Severity.BLOCKING
    user_action_required: bool = False
    expected_answer_type: Literal['OPTION_ID', 'SEMANTIC_REF', 'TYPED_VALUE', 'NONE'] = 'NONE'
    candidate_ids: list[Identifier] = Field(default_factory=list)
    system_repairable: bool = False
    message_code: Identifier

    @model_validator(mode='after')
    def only_user_ambiguity(self):
        if self.user_action_required and self.blocker_type != ErrorType.USER_AMBIGUITY:
            raise ValueError('system blockers cannot ask users for business slots; use UserDecision for product choices')
        return self


class ClarificationOption(StrictModel):
    option_id: Identifier
    display_label: str = Field(min_length=1)
    canonical_ref: BoundSemanticRef | None = None
    typed_value: TypedFilterValue | None = None
    evidence: list[Identifier] = Field(min_length=1)

    @model_validator(mode='after')
    def option_value(self):
        if (self.canonical_ref is None) == (self.typed_value is None):
            raise ValueError('clarification option requires exactly one canonical or typed value')
        return self


class ProceedDecision(StrictModel):
    decision_type: Literal['PROCEED'] = 'PROCEED'
    create_pending: Literal[False] = False


class SystemRepairDecision(StrictModel):
    decision_type: Literal['SYSTEM_REPAIR'] = 'SYSTEM_REPAIR'
    blockers: list[ReadinessBlocker] = Field(min_length=1)
    create_pending: Literal[False] = False

    @model_validator(mode='after')
    def system_only(self):
        if any(b.blocker_type == ErrorType.USER_AMBIGUITY for b in self.blockers):
            raise ValueError('user ambiguity is not a system repair')
        return self


class UserDecision(StrictModel):
    decision_type: Literal['USER_DECISION'] = 'USER_DECISION'
    reason: Literal['WATERMARK_ADJUSTMENT', 'LEGAL_SOURCE_SELECTION', 'SENSITIVE_EXPORT', 'HIGH_COST_OPERATION']
    option_ids: list[Identifier] = Field(min_length=1)
    create_pending: Literal[False] = False


class TerminalDecision(StrictModel):
    decision_type: Literal['TERMINAL'] = 'TERMINAL'
    reason_type: ErrorType
    create_pending: Literal[False] = False


class ClarificationDecision(StrictModel):
    decision_type: Literal['CLARIFICATION'] = 'CLARIFICATION'
    reason_type: Literal[ErrorType.USER_AMBIGUITY] = ErrorType.USER_AMBIGUITY
    options: list[ClarificationOption] = Field(default_factory=list)
    blocker: ReadinessBlocker | None = None
    selected_option_id: Identifier | None = None
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
        if self.blocker is not None and (self.blocker.blocker_type != ErrorType.USER_AMBIGUITY or not self.blocker.user_action_required):
            raise ValueError('clarification requires a user-actionable ambiguity')
        ids = [o.option_id for o in self.options]
        if len(set(ids)) != len(ids) or (self.selected_option_id is not None and self.selected_option_id not in ids):
            raise ValueError('invalid clarification option identity')
        return self


DecisionOutcome = Annotated[Union[ProceedDecision, ClarificationDecision, UserDecision, SystemRepairDecision, TerminalDecision], Field(discriminator='decision_type')]


class PermissionContext(StrictModel):
    tenant_id: Identifier
    user_id: Identifier
    application_id: Identifier
    allowed_business_domain_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    authorization_decision_id: Identifier | None = None
    policy_snapshot_id: Identifier | None = None
    row_scope_hash: Identifier | None = None
    column_scope_hash: Identifier | None = None
    metric_scope_hash: Identifier | None = None


class SnapshotContext(StrictModel):
    catalog_publish_id: Identifier | None = None
    catalog_version: Identifier
    semantic_model_version: Identifier
    vector_index_version: Identifier | None = None
    semantic_model_id: Identifier | None = None
    database_id: Identifier | None = None
    business_domain_ids: list[Identifier] = Field(default_factory=list)
    knowledge_base_names: list[Identifier] = Field(default_factory=list)
    data_snapshot_id: Identifier | None = None
    data_watermark: AwareDatetime | None = None


class VersionMetadata(StrictModel):
    prompt_version: Identifier
    policy_version: Identifier
    adapter_version: Identifier
    plan_schema_version: Literal["0.2.1"] = "0.2.1"
    current_turn_parser_version: Identifier = 'fixture-parse-v1'
    turn_resolver_version: Identifier = 'turn-resolver-v1'
    semantic_resolver_version: Identifier = 'bound-validator-v1'
    candidate_scoring_version: Identifier = 'weighted-v1'
    logical_plan_compiler_version: Identifier = 'logical-compiler-v1'
    slot_reducer_version: Identifier = 'atomic-reducer-v1'
    result_contract_compiler_version: Identifier = 'result-compiler-v1'
    execution_router_version: Identifier = 'payload-router-v1'
    temporal_policy_version: Identifier = 'temporal-v1'
    schema_version: Literal['0.2.1'] = '0.2.1'


class Provenance(StrictModel):
    current_turn_text: str = Field(min_length=1, max_length=8000)
    current_turn_id: Identifier
    source: Literal["REAL", "MOCK", "PARTIAL", "UNAVAILABLE"]
    digests: dict[str, str] = Field(default_factory=dict)


class BasePayload(StrictModel):
    filters: FilterExpression | None = None
    projection_spec: ProjectionSpec = Field(default_factory=ProjectionSpec)


class ControlPayload(StrictModel):
    payload_type: Literal["CONTROL"] = "CONTROL"
    control_action: ControlAction


class ChatPayload(StrictModel):
    payload_type: Literal["CHAT"] = "CHAT"
    response_mode: Literal["LIGHTWEIGHT"] = "LIGHTWEIGHT"


class MetricQueryPayloadBase(BasePayload):
    measures: list[BoundSemanticRef] = Field(min_length=1, max_length=50)
    time: TimeSpec | None = None
    group_by: list[BoundSemanticRef] = Field(default_factory=list, max_length=50)


class ScalarAggregatePayload(MetricQueryPayloadBase):
    payload_type: Literal["SCALAR_AGGREGATE"] = "SCALAR_AGGREGATE"
    group_by: list[BoundSemanticRef] = Field(default_factory=list, max_length=0)


class GroupedAggregatePayload(MetricQueryPayloadBase):
    payload_type: Literal["GROUPED_AGGREGATE"] = "GROUPED_AGGREGATE"
    group_by: list[BoundSemanticRef] = Field(min_length=1, max_length=50)


class TimeSeriesPayload(MetricQueryPayloadBase):
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
    ranking_target: BoundSemanticRef
    ranking: RankingSpec


class ComparisonPayload(MetricQueryPayloadBase):
    payload_type: Literal["COMPARISON"] = "COMPARISON"
    comparison: ComparisonSpec


class RelationListPayload(BasePayload):
    payload_type: Literal["RELATION_LIST"] = "RELATION_LIST"
    source_entity: BoundSemanticRef
    target_entity: BoundSemanticRef | None = None
    relation_target: BoundSemanticRef | None = None
    relationship_spec: RelationshipSpec | None = None
    limit: LimitSpec | None = None

    @model_validator(mode="after")
    def require_target(self) -> "RelationListPayload":
        """Require either a target entity or an explicit relation target."""

        if self.target_entity is None and self.relation_target is None:
            raise ValueError("relation list requires target_entity or relation_target")
        return self


class DetailRowsPayload(BasePayload):
    payload_type: Literal["DETAIL_ROWS"] = "DETAIL_ROWS"
    source_entity: BoundSemanticRef
    limit: LimitSpec | None = None

    @model_validator(mode='after')
    def require_projection(self):
        if not self.projection_spec.items:
            raise ValueError('detail rows require an explicit or governed default projection')
        return self


class DatasetFilterOperation(StrictModel):
    operation_type: Literal['FILTER'] = 'FILTER'
    filter_expression: FilterExpression


class DatasetSortOperation(StrictModel):
    operation_type: Literal['SORT'] = 'SORT'
    ordering: list[OrderingRequirement] = Field(min_length=1)


class DatasetLimitOperation(StrictModel):
    operation_type: Literal['LIMIT'] = 'LIMIT'
    limit: int = Field(ge=1, le=10000)
    preserve_existing_order: Literal[True] = True


class DatasetProjectOperation(StrictModel):
    operation_type: Literal['PROJECT'] = 'PROJECT'
    projection_spec: ProjectionSpec

    @model_validator(mode='after')
    def nonempty_projection(self):
        if not self.projection_spec.items:
            raise ValueError('PROJECT requires at least one projection')
        return self


class DrilldownSpec(StrictModel):
    dimension: BoundSemanticRef
    selected_values: ListValue


class DatasetDrilldownOperation(StrictModel):
    operation_type: Literal['DRILL_DOWN'] = 'DRILL_DOWN'
    drilldown_spec: DrilldownSpec


DatasetOperation = Annotated[Union[DatasetFilterOperation, DatasetSortOperation, DatasetLimitOperation, DatasetProjectOperation, DatasetDrilldownOperation], Field(discriminator='operation_type')]


class DatasetTransformPayload(StrictModel):
    payload_type: Literal["DATASET_TRANSFORM"] = "DATASET_TRANSFORM"
    source_dataset_id: Identifier
    operation: DatasetOperation

    @model_validator(mode='before')
    @classmethod
    def legacy_python_limit(cls, value):
        if isinstance(value, dict) and value.get('operation') == 'LIMIT' and isinstance(value.get('limit'), LimitSpec):
            value = dict(value)
            limit = value.pop('limit')
            value['operation'] = DatasetLimitOperation(limit=limit.limit, preserve_existing_order=limit.preserve_existing_order)
        return value


class MetricDefinitionPayload(StrictModel):
    payload_type: Literal["METRIC_DEFINITION"] = "METRIC_DEFINITION"
    metric_refs: list[BoundSemanticRef] = Field(min_length=1, max_length=50)


class MetadataPayload(StrictModel):
    payload_type: Literal["METADATA"] = "METADATA"
    targets: list[BoundSemanticRef] = Field(min_length=1, max_length=100)


class MetricTarget(StrictModel):
    target_type: Literal['METRIC'] = 'METRIC'
    ref: BoundSemanticRef


class FieldTarget(StrictModel):
    target_type: Literal['FIELD'] = 'FIELD'
    ref: BoundSemanticRef


class ColumnTarget(StrictModel):
    target_type: Literal['COLUMN'] = 'COLUMN'
    ref: BoundSemanticRef


class TableTarget(StrictModel):
    target_type: Literal['TABLE'] = 'TABLE'
    ref: BoundSemanticRef


class EntityTarget(StrictModel):
    target_type: Literal['ENTITY'] = 'ENTITY'
    ref: BoundSemanticRef


class DatasetTarget(StrictModel):
    target_type: Literal['DATASET'] = 'DATASET'
    ref: BoundSemanticRef


class ReportTarget(StrictModel):
    target_type: Literal['REPORT'] = 'REPORT'
    ref: BoundSemanticRef


LineageTarget = Annotated[Union[MetricTarget, FieldTarget, ColumnTarget, TableTarget, EntityTarget, DatasetTarget, ReportTarget], Field(discriminator='target_type')]


class LineagePayload(StrictModel):
    payload_type: Literal["LINEAGE"] = "LINEAGE"
    lineage_target: LineageTarget
    direction: Literal["UPSTREAM", "DOWNSTREAM", "BOTH"] = "BOTH"

    @field_validator('lineage_target', mode='before')
    @classmethod
    def legacy_python_target(cls, value):
        if isinstance(value, SemanticRef):
            kinds = {'METRIC': 'METRIC', 'ATTRIBUTE': 'FIELD', 'DIMENSION': 'FIELD', 'PHYSICAL_COLUMN': 'COLUMN', 'PHYSICAL_TABLE': 'TABLE', 'ENTITY': 'ENTITY', 'DATASET': 'DATASET', 'REPORT': 'REPORT'}
            return {'target_type': kinds[value.catalog_type.value], 'ref': BoundSemanticRef.model_validate(value)}
        return value

    @model_validator(mode='after')
    def target_catalog_type(self):
        allowed = {'METRIC': {'METRIC'}, 'FIELD': {'ATTRIBUTE', 'DIMENSION'}, 'COLUMN': {'PHYSICAL_COLUMN'}, 'TABLE': {'PHYSICAL_TABLE'}, 'ENTITY': {'ENTITY'}, 'DATASET': {'DATASET'}, 'REPORT': {'REPORT'}}
        if self.lineage_target.ref.catalog_type.value not in allowed[self.lineage_target.target_type]:
            raise ValueError('lineage target/catalog type mismatch')
        return self


class DataQualityPayload(StrictModel):
    payload_type: Literal["DATA_QUALITY"] = "DATA_QUALITY"
    quality_target: BoundSemanticRef
    checks: list[QualityCheckType] = Field(min_length=1, max_length=50)


class AlgorithmRef(StrictModel):
    algorithm_id: Identifier
    algorithm_version: Identifier
    policy_source: Literal['SYSTEM_POLICY', 'USER_GOVERNED_SELECTION']
    parameter_profile_id: Identifier | None = None

    @model_validator(mode='after')
    def governed_algorithm(self):
        from .registries import PayloadContractRegistry
        PayloadContractRegistry.validate_algorithm(self)
        return self


class AnomalyPayload(TimeSeriesPayload):
    payload_type: Literal["ANOMALY"] = "ANOMALY"
    algorithm: AlgorithmRef


class RootCausePayload(TimeSeriesPayload):
    payload_type: Literal["ROOT_CAUSE"] = "ROOT_CAUSE"
    decomposition_dimensions: list[BoundSemanticRef] = Field(min_length=1, max_length=20)


class ForecastPayload(TimeSeriesPayload):
    payload_type: Literal["FORECAST"] = "FORECAST"
    horizon_periods: int = Field(ge=1, le=1000)
    horizon_grain: TimeGrain
    algorithm: AlgorithmRef

    @model_validator(mode='after')
    def coherent_forecast(self):
        from .registries import PayloadContractRegistry
        if self.horizon_grain == TimeGrain.NONE or self.horizon_grain != self.time.grain:
            raise ValueError('forecast horizon must match the non-NONE history grain')
        PayloadContractRegistry.validate_algorithm(self.algorithm, payload_type='FORECAST', grain=self.horizon_grain)
        return self


class ReportPayload(StrictModel):
    """Composition of existing tasks only; analysis delivery belongs to DeliverySpec."""
    payload_type: Literal["REPORT_COMPOSITION"] = "REPORT_COMPOSITION"
    source_task_ids: list[Identifier] = Field(min_length=1, max_length=50)
    delivery_target: BoundSemanticRef | None = None


class CapabilityHelpPayload(StrictModel):
    payload_type: Literal['CAPABILITY_HELP'] = 'CAPABILITY_HELP'
    requested_route: ServiceRoute | None = None


class OutOfScopePayload(StrictModel):
    payload_type: Literal['OUT_OF_SCOPE'] = 'OUT_OF_SCOPE'
    reason_code: Literal['UNSUPPORTED_REQUEST', 'UNSUPPORTED_CAPABILITY']


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
        CapabilityHelpPayload,
        OutOfScopePayload,
    ],
    Field(discriminator="payload_type"),
]


class PlanEnvelope(StrictModel):
    """SHADOW_BUNDLE / DEBUG_EXPORT / COMPATIBILITY_VIEW; never execution truth."""

    schema_version: Literal["0.2.1"] = "0.2.1"
    usage: Literal['SHADOW_BUNDLE', 'DEBUG_EXPORT', 'COMPATIBILITY_VIEW'] = 'SHADOW_BUNDLE'
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
    clarification_decision: DecisionOutcome | None = None
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

        require_bounded_legacy_time(self.payload)
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


class DatasetAncestry(StrictModel):
    dataset_id: Identifier
    root_dataset_id: Identifier
    parent_dataset_ids: list[Identifier] = Field(default_factory=list)
    transformation_log: list[DatasetOperation] = Field(default_factory=list)
    stable_ordering: list[OrderingRequirement] = Field(default_factory=list)
    source_task_id: Identifier
    source_task_version: int = Field(ge=1)

    @model_validator(mode='after')
    def ancestry_identity(self):
        if self.dataset_id in self.parent_dataset_ids or len(set(self.parent_dataset_ids)) != len(self.parent_dataset_ids):
            raise ValueError('dataset ancestry cannot contain self or duplicate parents')
        if self.dataset_id == self.root_dataset_id and self.parent_dataset_ids:
            raise ValueError('root dataset cannot have parents')
        if self.dataset_id != self.root_dataset_id and not self.parent_dataset_ids:
            raise ValueError('derived dataset requires a parent')
        return self


class DatasetTransformSafetyContract(StrictModel):
    source_dataset_id: Identifier
    root_dataset_id: Identifier
    parent_dataset_ids: list[Identifier] = Field(default_factory=list)
    source_row_count: int = Field(ge=0)
    source_total_row_count: int | None = Field(default=None, ge=0)
    source_truncated: bool
    source_completeness: Literal['COMPLETE', 'PARTIAL', 'UNKNOWN']
    source_ordering_proof: ProofCheck
    global_topn_proof: ProofCheck | None = None
    snapshot_id: Identifier
    data_as_of: AwareDatetime
    requested_operation: Literal['FILTER', 'SORT', 'LIMIT', 'PROJECT', 'DRILL_DOWN', 'GLOBAL_TOP_N', 'GLOBAL_AGGREGATE']

    @property
    def safe_to_execute(self) -> bool:
        if self.requested_operation == 'GLOBAL_AGGREGATE':
            return not self.source_truncated and self.source_completeness == 'COMPLETE'
        if self.requested_operation == 'GLOBAL_TOP_N':
            return (not self.source_truncated and self.source_completeness == 'COMPLETE') or bool(
                self.global_topn_proof and self.global_topn_proof.check_id == 'GLOBAL_TOP_N_COMPLETE' and
                self.global_topn_proof.status == ProofStatus.PASS and
                {self.source_dataset_id, self.snapshot_id} <= set(self.global_topn_proof.evidence_ids))
        if self.requested_operation == 'LIMIT':
            return self.source_ordering_proof.status == ProofStatus.PASS
        if self.requested_operation == 'DRILL_DOWN':
            return False  # A new grain requires a source query; no local implementation claimed.
        return True

    @property
    def failure_reason(self) -> str | None:
        return None if self.safe_to_execute else 'REQUERY_OR_SYSTEM_REPAIR_REQUIRED'

    @model_validator(mode='after')
    def coherent_counts(self):
        if self.source_total_row_count is not None and self.source_total_row_count < self.source_row_count:
            raise ValueError('source total cannot be below materialized rows')
        if self.source_completeness == 'COMPLETE' and (self.source_truncated or self.source_total_row_count != self.source_row_count):
            raise ValueError('COMPLETE requires known equal counts and no truncation')
        return self


class SourceDatasetRef(StrictModel):
    dataset_id: Identifier
    source_task_id: Identifier
    source_task_version: int = Field(ge=1)
    snapshot_id: Identifier


class TaskSemanticState(StrictModel):
    subject: BoundSemanticRef | None = None
    metrics: list[BoundSemanticRef] = Field(default_factory=list)
    dimensions: list[BoundSemanticRef] = Field(default_factory=list)
    projection_spec: ProjectionSpec = Field(default_factory=ProjectionSpec)
    filter_expression: FilterExpression | None = None
    time_spec: TimeSpec | None = None
    ranking_spec: RankingSpec | None = None
    comparison_spec: ComparisonSpec | None = None
    relationship_spec: RelationshipSpec | None = None
    source_dataset_ref: SourceDatasetRef | None = None
    delivery_spec: DeliverySpec = Field(default_factory=DeliverySpec)
    analysis_goals: list[AnalysisGoal] = Field(default_factory=list)
    policy_decisions: list[ScopePolicyDecision] = Field(default_factory=list)


class ExecutionAttemptRecord(StrictModel):
    execution_id: Identifier
    task_id: Identifier
    task_version: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    status: ExecutionStatus = ExecutionStatus.CREATED
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    execution_backend: ExecutionBackend
    snapshot_id: Identifier
    data_watermark: AwareDatetime | None = None
    catalog_version: Identifier
    vector_index_version: Identifier
    semantic_model_version: Identifier
    policy_version: Identifier
    asl_digest: Identifier | None = None
    sql_digest: Identifier | None = None
    dataset_id: Identifier | None = None
    error_type: ErrorType | None = None
    proof_chain: ProofChain | None = None

    @model_validator(mode='after')
    def execution_lifecycle(self):
        if self.status in {ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED} and self.started_at is None:
            raise ValueError('started execution requires started_at')
        if self.status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED} and self.completed_at is None:
            raise ValueError('terminal execution requires completed_at')
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError('execution timestamps are reversed')
        if self.status == ExecutionStatus.SUCCEEDED:
            from .result_contract import completed_allowed
            if self.error_type or self.proof_chain is None or not completed_allowed(self.proof_chain.plan, self.proof_chain.asl, self.proof_chain.sql_plan, self.proof_chain.result):
                raise ValueError('successful execution requires all blocking proofs to pass')
            if not all(any(check.severity == Severity.BLOCKING for check in proof.checks)
                       for proof in (self.proof_chain.plan, self.proof_chain.asl, self.proof_chain.sql_plan, self.proof_chain.result)):
                raise ValueError('successful execution requires explicit checks in every proof stage')
        return self


BooleanFilterGroup.model_rebuild()
