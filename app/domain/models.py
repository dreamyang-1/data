from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
import math
import re
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrimaryIntent(StrEnum):
    METRIC_QUERY = "METRIC_QUERY"
    DETAIL_QUERY = "DETAIL_QUERY"
    TREND_ANALYSIS = "TREND_ANALYSIS"
    COMPARISON_ANALYSIS = "COMPARISON_ANALYSIS"
    COMPOSITION_ANALYSIS = "COMPOSITION_ANALYSIS"
    ANOMALY_ANALYSIS = "ANOMALY_ANALYSIS"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    FORECAST_ANALYSIS = "FORECAST_ANALYSIS"
    REPORT_GENERATION = "REPORT_GENERATION"
    METRIC_DEFINITION = "METRIC_DEFINITION"
    DATA_LINEAGE = "DATA_LINEAGE"
    DATA_QUALITY = "DATA_QUALITY"
    CAPABILITY_HELP = "CAPABILITY_HELP"
    CHAT = "CHAT"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ConversationControl(StrEnum):
    NEW_REQUEST = "NEW_REQUEST"
    CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
    FOLLOW_UP = "FOLLOW_UP"
    CORRECTION = "CORRECTION"
    CANCEL = "CANCEL"
    FEEDBACK = "FEEDBACK"


class TurnRelation(StrEnum):
    """Relationship between the raw current turn and conversation state."""

    STANDALONE_NEW_TOPIC = "STANDALONE_NEW_TOPIC"
    CURRENT_TOPIC_FOLLOWUP = "CURRENT_TOPIC_FOLLOWUP"
    CURRENT_TOPIC_MODIFICATION = "CURRENT_TOPIC_MODIFICATION"
    CURRENT_TOPIC_DRILLDOWN = "CURRENT_TOPIC_DRILLDOWN"
    HISTORICAL_TOPIC_RETURN = "HISTORICAL_TOPIC_RETURN"
    CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
    CORRECTION = "CORRECTION"
    AMBIGUOUS_RELATION = "AMBIGUOUS_RELATION"


class ContextMode(StrEnum):
    NONE = "NONE"
    CURRENT_THREAD = "CURRENT_THREAD"
    HISTORICAL_THREAD = "HISTORICAL_THREAD"
    CLARIFICATION_RESUME = "CLARIFICATION_RESUME"


class SlotSource(StrEnum):
    CURRENT_EXPLICIT = "CURRENT_EXPLICIT"
    CURRENT_INFERRED = "CURRENT_INFERRED"
    CURRENT_REFERENCE_RESOLUTION = "CURRENT_REFERENCE_RESOLUTION"
    ACTIVE_THREAD_STATE = "ACTIVE_THREAD_STATE"
    HISTORICAL_EPISODE = "HISTORICAL_EPISODE"
    RESULT_ARTIFACT = "RESULT_ARTIFACT"


class SlotOperationType(StrEnum):
    """Auditable merge operation applied to one canonical dialogue slot."""

    KEEP = "KEEP"
    INHERIT = "INHERIT"
    ADD = "ADD"
    REPLACE = "REPLACE"
    REMOVE = "REMOVE"
    CLEAR = "CLEAR"


class AnalysisOperator(StrEnum):
    FILTER = "FILTER"
    GROUP_BY = "GROUP_BY"
    AGGREGATE = "AGGREGATE"
    COMPARE = "COMPARE"
    RATIO = "RATIO"
    TOP_N = "TOP_N"
    BOTTOM_N = "BOTTOM_N"
    SORT = "SORT"
    TIME_BUCKET = "TIME_BUCKET"
    DECOMPOSE = "DECOMPOSE"
    ANOMALY_DETECT = "ANOMALY_DETECT"
    FORECAST = "FORECAST"
    EXPLAIN = "EXPLAIN"
    RENDER_TABLE = "RENDER_TABLE"
    RENDER_CHART = "RENDER_CHART"


class TimeRange(StrictModel):
    start: date
    end_exclusive: date
    timezone: str = "Asia/Shanghai"


class SlotProvenance(StrictModel):
    value: Any
    source: SlotSource
    source_turn: str | None = Field(default=None, max_length=128)
    source_thread: str | None = Field(default=None, max_length=128)
    source_episode: str | None = Field(default=None, max_length=128)
    confidence: float = Field(default=1.0, ge=0, le=1)


class SlotOperation(StrictModel):
    """A structured context merge decision; never an executable SQL operation."""

    slot: str = Field(min_length=1, max_length=200)
    operation: SlotOperationType
    old_value: Any = None
    new_value: Any = None
    evidence_span: str | None = Field(default=None, max_length=500)
    source: SlotSource
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason_code: str = Field(default="", max_length=100)


class CurrentTurnFacts(StrictModel):
    raw_query: str = Field(min_length=1, max_length=8000)
    explicit_slots: dict[str, SlotProvenance] = Field(default_factory=dict)
    inferred_slots: dict[str, SlotProvenance] = Field(default_factory=dict)
    reference_signals: list[str] = Field(default_factory=list)
    followup_signals: list[str] = Field(default_factory=list)
    topic_shift_signals: list[str] = Field(default_factory=list)
    core_subjects: dict[str, str] = Field(default_factory=dict)
    omitted_slots: list[str] = Field(default_factory=list)
    temporal_references: list[dict[str, Any]] = Field(default_factory=list)
    is_self_contained: bool = False


class TurnAdmissionDecision(StrictModel):
    relation: TurnRelation
    confidence: float = Field(ge=0, le=1)
    context_mode: ContextMode
    current_turn_facts: CurrentTurnFacts
    context_dependent: bool = False
    core_subject_changed: bool = False
    inherit_business_context: bool = False
    create_new_analysis_thread: bool = False
    historical_recall_required: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    protected_slots: list[str] = Field(default_factory=list)
    cleared_slots: list[str] = Field(default_factory=list)
    inheritance_slots: list[str] = Field(default_factory=list)
    previous_thread_id: str | None = Field(default=None, max_length=128)
    previous_episode_id: str | None = Field(default=None, max_length=128)
    selected_thread_id: str | None = Field(default=None, max_length=128)
    selected_episode_id: str | None = Field(default=None, max_length=128)
    context_before: dict[str, Any] = Field(default_factory=dict)
    context_delta: dict[str, Any] = Field(default_factory=dict)
    context_after: dict[str, Any] = Field(default_factory=dict)
    context_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    slot_operations: list[SlotOperation] = Field(default_factory=list, max_length=100)
    needs_clarification: bool = False


class TemporalAnchor(StrictModel):
    range_start: date
    range_end: date
    grain: Literal["day", "week", "month", "quarter", "year"] = "month"
    available_periods: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("available_periods")
    @classmethod
    def validate_available_periods(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        if any(not re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", value) for value in normalized):
            raise ValueError("available periods must use YYYY-MM")
        return normalized


class ResolvedPeriodComparison(StrictModel):
    left_period: str = Field(pattern=r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")
    right_period: str = Field(pattern=r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")
    operation: Literal["DECLINE", "RECOVERY", "CHANGE"] = "CHANGE"


class MetricRef(StrictModel):
    input: str
    metric_id: str | None = None
    version: str | None = None
    canonical_name: str | None = None
    unit: str | None = None


class IntentCandidate(StrictModel):
    intent: PrimaryIntent
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class SemanticAmbiguity(StrictModel):
    type: Literal[
        "turn_relation", "schema_relation", "metric", "dimension", "filter",
        "entity_value", "entity_role", "filter_slot", "operation_intent",
        "time_anchor", "comparison", "data_source", "context",
        "fact_conflict", "rewrite_conflict", "historical_branch", "subject",
        "unknown",
    ]
    question: str = Field(min_length=1, max_length=500)
    candidates: list[str] = Field(default_factory=list, max_length=10)
    ambiguity_id: str | None = Field(default=None, max_length=128)
    phrase: str | None = Field(default=None, max_length=200)
    affected_slots: list[str] = Field(default_factory=list, max_length=20)
    candidate_details: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    material_impact: str | None = Field(default=None, max_length=300)
    blocking: bool = True
    semantic_model_id: int | None = Field(default=None, gt=0)
    semantic_model_version: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def normalize_candidates(self) -> "SemanticAmbiguity":
        normalized = list(dict.fromkeys(value.strip() for value in self.candidates))
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("semantic ambiguity candidates must be non-empty and bounded")
        self.candidates = normalized
        return self


class DependencyConstraint(StrictModel):
    """Internal, scope-checked value constraint passed between DAG tasks.

    Values come only from a complete immutable predecessor dataset.  The HTTP
    retrieval adapter must prove that the generated ASL contains this exact
    constraint before SQL translation/execution is allowed.
    """

    source_task_id: str = Field(pattern=r"^task-[1-5]$")
    source_dataset_id: str = Field(min_length=1, max_length=128)
    source_column: str = Field(min_length=1, max_length=257)
    values: list[str | int | float | bool] = Field(min_length=1, max_length=50)
    value_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticFilterBinding(StrictModel):
    """Vector-catalog proof for one executable business filter.

    The intent model may propose a field, but only the current semantic model's
    entity-value catalog is allowed to bind the final attribute/value pair.
    Keeping the proof on the canonical request lets ASL validation compare the
    generated semantic attribute code instead of accepting a different field
    merely because it happens to preserve the same literal value.
    """

    filter_index: int = Field(ge=0, le=99)
    input_value: str = Field(min_length=1, max_length=500)
    canonical_value: str = Field(min_length=1, max_length=500)
    canonical_name: str = Field(min_length=1, max_length=100)
    attribute_code: str = Field(min_length=1, max_length=257)
    record_id: str | None = Field(default=None, max_length=512)
    score: float = Field(ge=0, le=1)
    business_domain_id: int | None = Field(default=None, gt=0)
    semantic_model_version: str | None = Field(default=None, max_length=128)
    source: Literal["ENTITY_ATTRIBUTE_VECTOR"] = "ENTITY_ATTRIBUTE_VECTOR"


class CanonicalAnalysisRequest(StrictModel):
    schema_version: str = "1.0"
    request_id: UUID = Field(default_factory=uuid4)
    conversation_id: str
    application_id: str = ""
    tenant_id: str
    user_id: str
    source_dataset_id: str | None = Field(default=None, max_length=128)
    analysis_thread_id: str | None = Field(default=None, max_length=128)
    turn_relation: TurnRelation | None = None
    model_turn_relation: TurnRelation | None = None
    model_turn_relation_confidence: float | None = Field(default=None, ge=0, le=1)
    context_mode: ContextMode = ContextMode.NONE
    slot_provenance: dict[str, SlotProvenance] = Field(default_factory=dict)
    slot_operations: list[SlotOperation] = Field(default_factory=list, max_length=100)
    turn_admission: TurnAdmissionDecision | None = None
    temporal_anchor: TemporalAnchor | None = None
    resolved_periods: list[str] = Field(default_factory=list, max_length=24)
    resolved_comparison: ResolvedPeriodComparison | None = None
    followup_type: str | None = Field(default=None, max_length=100)
    query_resolution_type: str | None = Field(default=None, max_length=100)
    execution_mode: Literal["QUERY_DATABASE", "REUSE_PREVIOUS_RESULT"] = "QUERY_DATABASE"
    execution_contract_transform: str | None = Field(default=None, max_length=100)
    semantic_entity_mentions: list[str] = Field(default_factory=list, max_length=50)
    original_question: str
    rewritten_question: str | None = None
    rewrite_events: list[dict[str, Any]] = Field(default_factory=list)
    rewrite_context_applied: bool = False
    rewrite_degraded: bool = False
    primary_intent: PrimaryIntent
    secondary_intents: list[PrimaryIntent] = Field(default_factory=list)
    conversation_control: ConversationControl = ConversationControl.NEW_REQUEST
    operators: list[AnalysisOperator] = Field(default_factory=list)
    metrics: list[MetricRef] = Field(default_factory=list)
    asl_template: dict[str, Any] | None = None
    entity: str | None = None
    metric_subject_entity: str | None = Field(
        default=None,
        max_length=128,
        exclude=True,
        description=(
            "指标公式或事实表的内部计算主体；不得作为用户查询对象展示"
        ),
    )
    fields: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    semantic_filter_bindings: list[SemanticFilterBinding] = Field(
        default_factory=list,
        max_length=100,
        description="当前语义模型实体属性向量库确认的筛选字段和值",
    )
    time_range: TimeRange | None = None
    comparison_type: str | None = None
    ranking_limit: int | None = Field(default=None, ge=1, le=100)
    forecast_horizon_periods: int | None = Field(default=None, ge=1, le=36)
    forecast_granularity: Literal["day", "week", "month", "quarter", "year"] | None = None
    forecast_history_provided: bool = False
    report_template_id: str | None = None
    missing_slots: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risk_level: str = "LOW"
    intent_source: str = "RULE"
    intent_confidence: float = Field(default=0.6, ge=0, le=1)
    intent_candidates: list[IntentCandidate] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    semantic_ambiguities: list[SemanticAmbiguity] = Field(default_factory=list, max_length=5)
    knowledge_base_names: list[str] = Field(default_factory=list)
    semantic_model_id: int | None = Field(default=None, gt=0)
    semantic_model_version: str | None = Field(default=None, max_length=128)
    # Presentation-only proof of slots resolved from the current semantic
    # vector snapshot. It must not enter ASL payloads or persisted state.
    semantic_display_slots: dict[str, Any] = Field(default_factory=dict, exclude=True)
    database_id: int | None = Field(
        default=None,
        gt=0,
        description="后端为本次请求显式选择的平台已注册数据库ID",
    )
    business_domain_ids: list[int] = Field(default_factory=list)
    resolved_business_domain_ids: list[int] = Field(
        default_factory=list,
        max_length=50,
        description="AUTO 模式下由当前语义向量命中确定的执行域，不代表调用方显式选择",
    )
    business_domain_selection_mode: Literal["AUTO", "EXPLICIT"] = "AUTO"
    confirmed_memory_ids: list[str] = Field(default_factory=list)
    confirmed_preferences: list[str] = Field(default_factory=list)
    dependency_constraints: list[DependencyConstraint] = Field(
        default_factory=list,
        max_length=5,
        description="DAG内部已校验的上游离散值约束，随待补充状态安全持久化",
    )
    # Version of the pending state consumed by this request. It is an internal
    # CAS token and must never be sent to downstream query/model services.
    pending_state_version: int | None = Field(default=None, ge=1, exclude=True)


class HistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)
    message_id: str | None = Field(default=None, max_length=128)
    created_at: datetime | None = None


class ToolConfig(StrictModel):
    """Platform-provided HTTP tool declaration.

    The declaration is intentionally data-only. Runtime dispatch remains
    controlled by the agent and never evaluates caller-provided Python code.
    """

    # Tool declarations are owned by the platform and may gain transport
    # metadata independently. Keep the request root strict, but tolerate
    # unknown tool-level extension fields.
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=8, max_length=2048)
    headers: dict[str, str] | None = Field(default=None)
    http_method: Literal["get", "post"] = "post"
    input_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="inputSchema", serialization_alias="inputSchema"
    )
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    type: Literal["", "tool", "flow"] = ""
    timeout: int = Field(
        default=15,
        ge=1,
        le=180,
        validation_alias=AliasChoices("timeout", "time_out"),
    )

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^https?://[^\s]+$", value, re.I):
            raise ValueError("tool url must be an absolute HTTP(S) URL")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[\w.:-]+", value, re.UNICODE):
            raise ValueError(
                "tool name may only contain Unicode letters, numbers, underscore, dot, colon, or hyphen"
            )
        return value

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 30:
            raise ValueError("tool headers must contain at most 30 items")
        for key, item in value.items():
            if not key or len(key) > 100 or len(item) > 2048:
                raise ValueError("tool header name or value is invalid")
            if "\r" in key or "\n" in key or "\r" in item or "\n" in item:
                raise ValueError("tool headers must not contain CR/LF")
        return value


class SkillConfig(StrictModel):
    """Allow an application to expose a configured skill to this request."""

    code: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    slug: str = Field(min_length=1, max_length=100)
    authority: str = Field(default="", max_length=500)

    @field_validator("slug")
    @classmethod
    def validate_platform_skill_slug(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("skill slug contains an unsafe path component")
        return value


class McpConfig(StrictModel):
    """MCP server declaration compatible with the platform agent contract."""

    mcp_server_url: str = Field(min_length=8, max_length=2048)
    connect_type: Literal["sse", "streamable_http"] = "sse"
    headers: dict[str, str] | None = None

    @field_validator("mcp_server_url")
    @classmethod
    def validate_mcp_url(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^https?://[^\s]+$", value, re.I):
            raise ValueError("MCP server URL must be an absolute HTTP(S) URL")
        return value

    @field_validator("headers")
    @classmethod
    def validate_mcp_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return ToolConfig.validate_headers(value)


class ChatRequest(StrictModel):
    _file_inspection: dict[str, Any] = PrivateAttr(default_factory=dict)
    # These flags are set only by the refresh endpoints.  Keeping them as
    # private attributes prevents transport-only refresh semantics from
    # leaking into ASL/SQL payloads or request fingerprints.
    _bypass_repeat_query_cache: bool = PrivateAttr(default=False)
    _is_regeneration_execution: bool = PrivateAttr(default=False)
    _regeneration_mode: str = PrivateAttr(default="NONE")
    conversation_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    application_id: str = Field(min_length=1, max_length=100)
    semantic_model_id: int | None = Field(default=None, gt=0, strict=True)
    database_id: int | None = Field(
        default=None,
        gt=0,
        strict=True,
        description=(
            "可选；后端为本次请求选择的平台已注册数据库ID。未传时使用语义模型默认数据源"
        ),
    )
    # Deprecated compatibility field. New callers should use
    # business_domain_ids; omitting both enables semantic-model-wide routing.
    business_domain_id: int | None = Field(default=None, gt=0, strict=True)
    business_domain_ids: list[int] = Field(default_factory=list, max_length=50)
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=50)
    history: list[HistoryMessage] = Field(
        default_factory=list,
        max_length=100,
        description="最多接收100条有序历史；执行时按任务锚点和近期轮次压缩",
    )
    # Long-term memory is opt-in per request, consistent with the platform's
    # generic agent API. Short-term conversation state is always scoped by the
    # trusted tenant/user/application/conversation identifiers.
    use_longterm_memory: bool = False
    regenerate: bool = Field(
        default=False,
        validation_alias=AliasChoices("regenerate", "refresh", "force_regenerate"),
        exclude=True,
        description=(
            "在原会话范围内替换当前轮并完整重算；跳过旧答案缓存，但保留刷新请求幂等。"
        ),
    )
    # 修改最后一个问题重新提问（revise）场景专用。刷新接口只操作最后一问，
    # 因此调用方无需提交完整 history 或被替换轮 message_id。
    original_question: str | None = Field(
        default=None,
        max_length=4000,
        exclude=True,
        description=(
            "修改最后一个问题重新提问时传修改前的问题，用于区分REVISE与REFRESH；"
            "纯刷新不传。history不是刷新接口必填项"
        ),
    )
    refresh_request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        exclude=True,
        description=(
            "刷新尝试的幂等键。同一次网络重试必须复用；用户再次主动刷新时应生成新值。"
        ),
    )
    replaces_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        exclude=True,
        description=(
            "兼容旧调用方的可选字段；若同时传history，只允许指向其中最后一条"
            "user消息。新调用方无需传此字段"
        ),
    )
    tools: list[ToolConfig] = Field(default_factory=list, max_length=30)
    skills: list[SkillConfig] = Field(default_factory=list, max_length=30)
    mcp: list[McpConfig] = Field(default_factory=list, max_length=10)
    web_search: bool = Field(
        default=False,
        description=(
            "是否启用内置联网搜索；显式传入的tool、MCP和skill仍独立可用"
        ),
    )
    temp_file_paths: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="平台已上传到MinIO的临时对象路径；不接收本地文件系统路径",
    )
    department: str = Field(default="", max_length=100)
    dataset_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="已导入文件或历史查询结果的数据集ID",
    )
    dag_resume_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=256,
        description=(
            "多任务追问的可选兼容令牌；同一受信会话可由服务端自动恢复。"
            "如客户端选择回传，则必须使用最新响应中的原值"
        ),
    )
    task_answers: dict[str, str] = Field(
        default_factory=dict,
        max_length=5,
        description="多个子任务同时待补时，按 task_id 提交补充答案",
    )
    dependency_constraints: list[DependencyConstraint] = Field(
        default_factory=list,
        max_length=5,
        exclude=True,
        description="仅供受控任务DAG内部传递，普通调用方无需提供",
    )

    @field_validator(
        "conversation_id",
        "message_id",
        "question",
        "application_id",
        "dataset_id",
        "dag_resume_token",
        "refresh_request_id",
        "replaces_message_id",
        mode="before",
    )
    @classmethod
    def normalize_required_text(cls, value: Any) -> Any:
        """Make identifiers and questions canonical before length validation."""

        return value.strip() if isinstance(value, str) else value

    @field_validator("task_answers", mode="before")
    @classmethod
    def validate_task_answers(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        if len(value) > 5:
            raise ValueError("task_answers must contain at most 5 items")
        normalized: dict[str, str] = {}
        for task_id, answer in value.items():
            if not isinstance(task_id, str) or not re.fullmatch(r"task-[1-5]", task_id):
                raise ValueError("task_answers keys must use task-1 to task-5")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("task_answers values must be non-empty strings")
            if len(answer.strip()) > 4000:
                raise ValueError("task_answers values must contain at most 4000 characters")
            normalized[task_id] = answer.strip()
        return normalized

    @field_validator("knowledge_base_names", mode="before")
    @classmethod
    def normalize_knowledge_base_names(cls, value: Any) -> Any:
        """Validate and canonicalize the knowledge-base scope.

        Sorting the unique, trimmed names is intentional: knowledge-base scope is
        a set, and equivalent requests must produce the same idempotency
        fingerprint regardless of caller ordering or duplicates.
        """

        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        if len(value) > 50:
            raise ValueError("knowledge_base_names must contain at most 50 items")

        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("knowledge_base_names items must be strings")
            name = item.strip()
            if not name:
                raise ValueError("knowledge_base_names items must not be blank")
            if len(name) > 128:
                raise ValueError(
                    "knowledge_base_names items must contain at most 128 characters"
                )
            normalized.append(name)
        return sorted(set(normalized))

    @field_validator("temp_file_paths", mode="before")
    @classmethod
    def normalize_temp_file_paths(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("temp_file_paths items must be strings")
            path = item.strip().replace("\\", "/")
            if not path or len(path) > 1024 or path.startswith("/") or ".." in path.split("/"):
                raise ValueError("temp_file_paths contains an invalid MinIO object path")
            normalized.append(path)
        return list(dict.fromkeys(normalized))

    @field_validator(
        "business_domain_ids", mode="before"
    )
    @classmethod
    def normalize_business_domain_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        normalized: list[int] = []
        for item in value:
            # bool is an int subclass and must not become a domain id.
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError("business domain ids must be positive integers")
            normalized.append(item)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_history(self) -> "ChatRequest":
        if self.task_answers and self.dag_resume_token is None:
            raise ValueError("task_answers requires dag_resume_token")
        if self.business_domain_id is not None:
            if self.business_domain_ids and self.business_domain_ids != [self.business_domain_id]:
                raise ValueError(
                    "business_domain_id conflicts with business_domain_ids"
                )
            self.business_domain_ids = [self.business_domain_id]
        tool_names = [item.name for item in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tools names must be unique")
        skill_slugs = [item.slug for item in self.skills]
        if len(skill_slugs) != len(set(skill_slugs)):
            raise ValueError("skills slugs must be unique")
        message_ids = [item.message_id for item in self.history if item.message_id]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("history message_id values must be unique")
        if self.message_id in message_ids:
            raise ValueError("history must not include the current message_id")
        timestamps = [item.created_at for item in self.history]
        comparable = [item for item in timestamps if item is not None]
        if comparable and len(comparable) != len(timestamps):
            raise ValueError(
                "history created_at must be provided for every item or omitted for every item"
            )
        if any(item.tzinfo is None or item.utcoffset() is None for item in comparable):
            raise ValueError("history created_at must include a timezone")
        if len(comparable) == len(timestamps) and comparable != sorted(comparable):
            raise ValueError("history must be ordered from oldest to newest")
        if sum(len(item.content) for item in self.history) > 120_000:
            raise ValueError("history content must contain at most 120000 characters")
        return self


class TrustedIdentity(StrictModel):
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)


class QuerySpec(StrictModel):
    schema_version: str = "1.0"
    request_id: UUID = Field(default_factory=uuid4)
    metric_ids: list[str] = Field(default_factory=list)
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: TimeRange | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None
    snapshot_requirement: str = "CONSISTENT"


class Dataset(StrictModel):
    columns: list[str] = Field(max_length=500)
    rows: list[dict[str, Any]] = Field(max_length=10000)
    snapshot_id: str = Field(min_length=1, max_length=256)
    data_as_of: datetime
    # Business-data watermark from a semantic-model registered time field.
    # This differs from ``data_as_of``, which is the database snapshot time.
    source_data_as_of: datetime | date | None = None
    source_watermark_field: str | None = Field(default=None, max_length=257)
    quality_status: str = Field(default="PASS", min_length=1, max_length=50)
    row_count: int = Field(default=0, ge=0)
    # Number of rows in the complete upstream result. It may be larger than
    # row_count when SQL Translator returns only a preview or a MinIO file.
    total_row_count: int | None = Field(default=None, ge=0)
    truncated: bool = False

    @model_validator(mode="after")
    def validate_dataset_contract(self) -> "Dataset":
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("dataset columns must be unique")
        if any(not column.strip() for column in self.columns):
            raise ValueError("dataset columns must not be blank")
        if self.row_count != len(self.rows):
            raise ValueError("dataset row_count must equal rows length")
        if self.total_row_count is None:
            self.total_row_count = self.row_count
        elif self.total_row_count < self.row_count:
            raise ValueError("dataset total_row_count must not be smaller than row_count")
        declared = set(self.columns)
        if any(not set(row).issubset(declared) for row in self.rows):
            raise ValueError("dataset rows contain undeclared columns")
        pending: list[Any] = list(self.rows)
        visited = 0
        while pending:
            value = pending.pop()
            visited += 1
            if visited > 200_000:
                raise ValueError("dataset nested value count exceeds safety limit")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("dataset rows must not contain NaN or Infinity")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("dataset rows must not contain non-finite Decimal values")
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
        if self.data_as_of.tzinfo is None or self.data_as_of.utcoffset() is None:
            raise ValueError("dataset data_as_of must include a timezone")
        has_source_time = self.source_data_as_of is not None
        has_source_field = self.source_watermark_field is not None
        if has_source_time != has_source_field:
            raise ValueError(
                "dataset source_data_as_of and source_watermark_field must be provided together"
            )
        if self.source_watermark_field is not None:
            self.source_watermark_field = self.source_watermark_field.strip()
            if not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
                self.source_watermark_field,
            ):
                raise ValueError("dataset source_watermark_field must be table.column")
        self.quality_status = self.quality_status.strip().upper()
        return self


class DataQueryResult(StrictModel):
    """Validated result of the platform-owned NL -> ASL -> SQL chain."""

    asl: dict[str, Any]
    sql: str
    dataset: Dataset
    data_source_id: str | None = None
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    # Audited post-query transformations are kept separate from ASL because
    # they describe deterministic result processing, not upstream query
    # semantics.  Reliability gates consume these records as provenance.
    execution_transforms: list[dict[str, Any]] = Field(
        default_factory=list, max_length=20
    )
    result_file_url: str | None = Field(
        default=None,
        max_length=4096,
        description="上游对大结果集提供的短期MinIO下载地址",
    )


class EvidenceItem(StrictModel):
    evidence_id: str
    kind: str
    source_ref: str
    payload: dict[str, Any]


class KnowledgeDocument(StrictModel):
    content: str
    source: str | None = None
    block_id: str | None = None
    kb_name: str | None = None
    score: float | None = None
    normalized_relevance: float | None = Field(default=None, ge=0, le=1)
    retrieval_rank: int | None = Field(default=None, ge=1)


class KnowledgeContext(StrictModel):
    query: str
    documents: list[KnowledgeDocument] = Field(default_factory=list)


class ReliabilityReport(StrictModel):
    level: str
    score: float = Field(ge=0, le=1)
    gates: dict[str, bool]
    warnings: list[str] = Field(default_factory=list)


class AnalysisRequirement(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    category: Literal["USER_INPUT", "DATA", "ALGORITHM_CAPABILITY"]
    description: str = Field(min_length=1, max_length=500)
    action: str = Field(min_length=1, max_length=500)


class AnalysisHypothesis(StrictModel):
    statement: str = Field(min_length=1, max_length=500)
    verification: str = Field(min_length=1, max_length=500)
    status: Literal["PLANNED", "NOT_APPLICABLE"] = "PLANNED"


class DataSufficiencyRule(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    minimum_rows: int = Field(default=1, ge=1, le=10000)


class AnalysisDataContractSpec(StrictModel):
    operator: str = Field(min_length=1, max_length=100)
    required_columns: dict[str, list[str]] = Field(default_factory=dict)
    minimum_rows: int = Field(default=1, ge=1, le=10000)
    maximum_rows: int | None = Field(default=None, ge=1, le=10000)
    instructions: list[str] = Field(default_factory=list, max_length=10)


class ExplorationQueryRequirements(StrictModel):
    """Bounded query shape for open-ended deterministic insight discovery."""

    minimum_numeric_metrics: int = Field(default=1, ge=1, le=3)
    maximum_numeric_metrics: int = Field(default=3, ge=1, le=3)
    minimum_dimensions: int = Field(default=1, ge=1, le=3)
    maximum_dimensions: int = Field(default=3, ge=1, le=3)
    prefer_time_dimension: bool = True
    result_grain: Literal["AGGREGATED_ANALYSIS_READY"] = "AGGREGATED_ANALYSIS_READY"

    @model_validator(mode="after")
    def validate_ranges(self) -> "ExplorationQueryRequirements":
        if self.maximum_numeric_metrics < self.minimum_numeric_metrics:
            raise ValueError("maximum_numeric_metrics must not be smaller than minimum_numeric_metrics")
        if self.maximum_dimensions < self.minimum_dimensions:
            raise ValueError("maximum_dimensions must not be smaller than minimum_dimensions")
        return self


class AnalysisPlan(StrictModel):
    """Public, auditable analysis plan; never contains hidden model reasoning."""

    schema_version: str = "1.0"
    objective: str = Field(min_length=1, max_length=500)
    methods: list[str] = Field(min_length=1, max_length=10)
    metrics: list[str] = Field(default_factory=list, max_length=20)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    baseline: str | None = Field(default=None, max_length=300)
    hypotheses: list[AnalysisHypothesis] = Field(default_factory=list, max_length=10)
    sufficiency_rules: list[DataSufficiencyRule] = Field(default_factory=list, max_length=10)
    conclusion_policy: list[str] = Field(min_length=1, max_length=10)
    data_contract: AnalysisDataContractSpec | None = None
    exploration_requirements: ExplorationQueryRequirements | None = None


class AnalysisProcessStep(StrictModel):
    """A safe, user-visible audit step rather than hidden model reasoning."""

    stage: Literal[
        "QUESTION_REWRITE",
        "UNDERSTANDING",
        "ANALYSIS_PLANNING",
        "COMPLETENESS_CHECK",
        "DATA_QUERY",
        "METRIC_VERIFICATION",
        "KNOWLEDGE_RETRIEVAL",
        "DETERMINISTIC_ANALYSIS",
        "MODEL_SYNTHESIS",
        "RELIABILITY_CHECK",
        "SAFE_TERMINATION",
    ]
    status: Literal["COMPLETED", "NEEDS_INPUT", "SKIPPED", "DEGRADED", "FAILED"]
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class ChartSpec(StrictModel):
    """Stable, renderer-neutral visualization contract for web clients."""

    schema_version: str = "1.0"
    chart_type: Literal["LINE", "BAR", "PIE", "SCATTER", "TABLE"]
    title: str = Field(min_length=1, max_length=200)
    x_field: str | None = None
    y_fields: list[str] = Field(default_factory=list, max_length=10)
    series_field: str | None = None
    data: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    point_count: int = Field(default=0, ge=0, le=200)
    data_truncated: bool = False
    horizontal: bool = False
    reason: str = Field(default="", max_length=300)


class AtomicTask(StrictModel):
    task_id: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=2, max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=5)


class TaskPlan(StrictModel):
    schema_version: str = "1.0"
    planner: Literal["STRUCTURED_MODEL", "DETERMINISTIC_RULE"]
    tasks: list[AtomicTask] = Field(min_length=2, max_length=5)
    final_deliverable: Literal["COMBINED_REPORT"] | None = None


class TaskExecutionResult(StrictModel):
    task_id: str
    question: str
    status: str
    intent: PrimaryIntent | None = None
    answer: str
    dataset_id: str | None = None
    result_file_url: str | None = Field(default=None, max_length=4096)
    reliability: ReliabilityReport | None = None
    chart_specs: list[ChartSpec] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class GeneratedFile(StrictModel):
    """A short-lived downloadable artifact returned by the chat workflow."""

    file_id: str = Field(min_length=1, max_length=128)
    dataset_id: str | None = Field(default=None, max_length=128)
    dataset_ids: list[str] = Field(default_factory=list, max_length=5)
    format: Literal["xlsx", "docx", "pdf"]
    object_name: str = Field(min_length=1, max_length=1024)
    download_url: str = Field(min_length=1, max_length=4096)
    byte_size: int = Field(ge=0)
    expires_at: str


class ExtensionExecution(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["HTTP_TOOL", "MCP_TOOL"]
    status: Literal["COMPLETED", "FAILED", "REJECTED"]
    output: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=500)
    status_code: int | None = Field(default=None, ge=0, le=999)
    error_type: str | None = Field(default=None, max_length=100)


class ClarificationItem(StrictModel):
    slot: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=20)
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=10)
    option_details: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "可选项对应的规范语义元数据；向量歧义时包含类型、规范名称、"
            "规范编码、召回分数和记录ID"
        ),
    )
    multi_select: bool = False
    allow_free_text: bool = True

    @model_validator(mode="after")
    def validate_options(self) -> "ClarificationItem":
        normalized = [value.strip() for value in self.options]
        if any(not value for value in normalized):
            raise ValueError("clarification options must be non-empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("clarification options must be unique")
        self.options = normalized
        return self


class AgentResponse(StrictModel):
    request_id: UUID
    conversation_id: str
    status: str
    intent: PrimaryIntent
    intent_source: str = "RULE"
    intent_confidence: float = Field(default=0.6, ge=0, le=1)
    execution_shape: Literal["SINGLE", "COMPOSITE"] = "SINGLE"
    task_intents: list[PrimaryIntent] = Field(default_factory=list, max_length=5)
    answer: str
    clarification_questions: list[str] = Field(default_factory=list)
    clarification_items: list[ClarificationItem] = Field(default_factory=list, max_length=5)
    remaining_question_count: int = Field(default=0, ge=0)
    missing_slots: list[str] = Field(default_factory=list)
    # Normalized values already understood by the agent.  Returning these during
    # clarification makes state visible to the caller and prevents the UI from
    # presenting a misleading, context-free follow-up question.
    understood_slots: dict[str, Any] = Field(default_factory=dict)
    clarification_round: int | None = Field(default=None, ge=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    reliability: ReliabilityReport | None = None
    requirements: list[AnalysisRequirement] = Field(default_factory=list, max_length=20)
    analysis_process: list[AnalysisProcessStep] = Field(default_factory=list, max_length=20)
    analysis_plan: AnalysisPlan | None = None
    chart_specs: list[ChartSpec] = Field(default_factory=list, max_length=10)
    files: list[GeneratedFile] = Field(
        default_factory=list,
        max_length=10,
        description="本轮按需生成的MinIO文件；URL过期后需重新通过对话生成",
    )
    skills_used: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="本轮实际路由到的内置Skill标识",
    )
    extension_executions: list[ExtensionExecution] = Field(
        default_factory=list,
        max_length=10,
        description="按Skill显式绑定后实际调用的HTTP/MCP工具结果；不替代核心确定性结论",
    )
    task_plan: TaskPlan | None = None
    task_results: list[TaskExecutionResult] = Field(default_factory=list, max_length=5)
    dag_resume_token: str | None = Field(
        default=None,
        description=(
            "多任务需要补充信息时返回的可选兼容令牌；普通同会话追问无需回传，"
            "显式回传时服务端会校验其是否为最新值"
        ),
    )
    awaiting_task_ids: list[str] = Field(default_factory=list, max_length=5)
    dataset_id: str | None = Field(
        default=None,
        description="本轮查询或派生结果的数据集ID，供后续追问关联使用",
    )
    dataset_ids: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="复合任务中各独立结果集ID；单任务通常为空",
    )
    result_file_url: str | None = Field(
        default=None,
        max_length=4096,
        description="SQL执行服务对大结果集返回的MinIO地址；小结果通常为空",
    )
    semantic_model_id: int | None = Field(default=None, gt=0)
    database_id: int | None = Field(
        default=None,
        gt=0,
        description="本次请求显式加载的数据库ID；未显式选择时为空",
    )
    requested_business_domain_ids: list[int] = Field(default_factory=list)
    business_domain_selection_mode: Literal["AUTO", "EXPLICIT"] = "AUTO"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PendingState(StrictModel):
    request: CanonicalAnalysisRequest
    clarification_rounds: int = 1
    state_version: int = 1
    remaining_questions: list[str] = Field(default_factory=list, max_length=100)
