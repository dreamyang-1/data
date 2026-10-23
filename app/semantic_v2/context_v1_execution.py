"""Opt-in V2 context resolution followed by the existing V1 workflow.

V2 owns only relation, TaskState and completed-question resolution here.  The
existing V1 workflow remains the sole business query execution path.  This
module never creates an ASL, lowers SQL, or calls a database transport.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field
from redis.asyncio import Redis

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    AnalysisProcessStep,
    ChatRequest,
    DependencyConstraint,
    PrimaryIntent,
    ReliabilityReport,
    TrustedIdentity,
)
from app.services.progress import emit_progress, progress_scope
from app.stores import MessageIdReuseConflictError

from .authorized_contract import (
    AuthorizedScopeContext,
    CatalogPinIdentity,
    ScopedArtifact,
    contract_digest,
)
from .catalog_bridge import ScopedPlanSession
from .completed_question import CompletedQuestionDisplay, build_completed_question_display
from .context_proposal import ContextProposalFailure
from .explicit_time import normalize_range
from .limited_scalar_runtime import (
    LiveReadOnlyCatalogGeneration,
    OAGNET_RUNTIME_FILES,
    _build_live_read_only_catalog_generation,
    _import_runtime_module,
    _open_live_read_only_catalog,
    _prepend_runtime_roots,
    source_bundle_digest,
)
from .pending_recognition import RecognizedClarification
from .pipeline import CurrentTurnParser, CurrentTurnSemanticParse
from .persisted_scalar_api import RedisScalarSessionStore
from .pipeline import AuthorizedLogicalPlan
from .recognition import RawTurnPlanner, RecognizedStandaloneNewTask
from .recognition_client import RecognitionFailure, RecognitionModelClient
from .recognition_repairs import repair_model_parse
from . import models as semantic_models
from .state_machine import ConversationState, StateTransitionError


logger = logging.getLogger(__name__)


_DEMO_RETRY_WITHOUT_TIME_CODES = frozenset({"ASL_TIME_ANCHOR_MISSING"})


class ExecutionAnchorEditFailure(RecognitionFailure):
    """Public-compatible rejection with content-free internal diagnostics."""

    def __init__(
        self,
        reason_code: str,
        diagnostic: dict[str, Any],
        *,
        public_code: str = "V2_EXECUTION_ANCHOR_EDIT_UNSUPPORTED",
    ):
        self.reason_code = reason_code
        self.diagnostic = {**diagnostic, "reason_code": reason_code}
        super().__init__(public_code)


_TIME_EDIT_AUXILIARY = re.compile(
    r"(?:请|麻烦)?(?:帮我)?(?:再)?"
    r"(?:换|改|调整|设置|设|看|查|查询|查看)"
    r"(?:成|为|到)?(?:一下|看看)?(?:呢|吧)?"
)
_TIME_EDIT_PUNCTUATION = re.compile(r"[\s，,。.!！?？;；：:]+")
_MAX_TIME_EDIT_QUESTION_LENGTH = 120
_MAX_TIME_EXPRESSION_LENGTH = 40


@dataclass(frozen=True)
class ContextV1ExternalDependencies:
    publication: Any
    model: Any
    redis: Any
    module_origins: dict[str, str] = field(default_factory=dict)
    catalog_runtime: Any | None = None


@dataclass(frozen=True)
class CatalogRuntimeDecision:
    catalog_version_used: str
    refreshed: bool = False
    demo_catalog_fallback: bool = False
    catalog_refresh_failure: str | None = None


class DemoCatalogAutoRefresh:
    """Single-flight, process-local Catalog refresh for the demo bridge.

    Every request first captures the current authority. A content change with
    the same source identity and exact model/domain scope builds a complete new
    process-memory generation before the publication reference is swapped.
    Strict deployments never construct this coordinator.
    """

    def __init__(
        self,
        *,
        initial: LiveReadOnlyCatalogGeneration,
        capture: Callable[[], dict[str, Any]],
        refresh: Callable[[], LiveReadOnlyCatalogGeneration],
        expected_scope: dict[str, Any],
    ):
        self._capture = capture
        self._refresh = refresh
        self._expected_scope = deepcopy(expected_scope)
        self._source_identity_hash = initial.snapshot.get("source_identity_hash")
        self._target_identity_hash = initial.identity.get("target_identity_hash")
        self._generation = initial
        # Request pins use an immutable, fully verified snapshot. Authority is
        # compared immediately before request handling by ``ensure_current``.
        self._active_publication = initial.frozen_publication
        self._lock = asyncio.Lock()
        self._refresh_count = 0
        self._retired_identities: list[dict[str, Any]] = []
        self._failed_authority_version: str | None = None
        self._failed_at = 0.0
        self._failure_retry_seconds = 5.0
        self._last_decision = CatalogRuntimeDecision(
            catalog_version_used=initial.identity["catalog_version"]
        )

    def pin(self, *args, **kwargs):
        return self._active_publication.pin(*args, **kwargs)

    @property
    def current_identity(self) -> dict[str, Any]:
        return deepcopy(self._generation.identity)

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def retired_identities(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(item) for item in reversed(self._retired_identities))

    def status(self) -> dict[str, Any]:
        decision = self._last_decision
        return {
            "catalog_runtime_policy": "AUTO_REFRESH",
            "catalog_version_used": decision.catalog_version_used,
            "catalog_refresh_count": self._refresh_count,
            "demo_catalog_fallback": decision.demo_catalog_fallback,
            "catalog_refresh_failure": decision.catalog_refresh_failure,
        }

    def _validate_request(
        self,
        chat: ChatRequest | None,
        identity: TrustedIdentity | None,
    ) -> None:
        if chat is None:
            return
        allowed_domains = self._expected_scope["business_domain_ids"]
        requested = list(chat.business_domain_ids)
        if (
            chat.semantic_model_id != self._expected_scope["semantic_model_id"]
            or len(requested) > 1
            or any(domain not in allowed_domains for domain in requested)
        ):
            raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")
        if identity is None or not identity.tenant_id or not identity.user_id:
            raise ValueError("TRUSTED_IDENTITY_REQUIRED")

    def _validate_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("scope") != self._expected_scope:
            raise RuntimeError("DEMO_CATALOG_SCOPE_CHANGED")
        if snapshot.get("source_identity_hash") != self._source_identity_hash:
            raise RuntimeError("DEMO_CATALOG_SOURCE_IDENTITY_CHANGED")
        version = snapshot.get("catalog_version")
        if not isinstance(version, str) or not version:
            raise RuntimeError("DEMO_CATALOG_VERSION_MISSING")

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        value = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return value[:160]

    async def ensure_current(
        self,
        chat: ChatRequest | None = None,
        identity: TrustedIdentity | None = None,
    ) -> CatalogRuntimeDecision:
        self._validate_request(chat, identity)
        snapshot = await asyncio.to_thread(self._capture)
        self._validate_snapshot(snapshot)
        current_version = snapshot["catalog_version"]
        if current_version == self._generation.identity["catalog_version"]:
            decision = CatalogRuntimeDecision(catalog_version_used=current_version)
            self._last_decision = decision
            return decision
        if (
            current_version == self._failed_authority_version
            and time.monotonic() - self._failed_at < self._failure_retry_seconds
            and self._last_decision.demo_catalog_fallback
        ):
            return self._last_decision

        logger.warning(
            "CATALOG_DRIFT_DETECTED previous=%s current=%s policy=AUTO_REFRESH",
            self._generation.identity["catalog_version"],
            current_version,
            extra={
                "catalog_version_previous": self._generation.identity[
                    "catalog_version"
                ],
                "catalog_version_current": current_version,
                "catalog_runtime_policy": "AUTO_REFRESH",
            },
        )
        async with self._lock:
            # Another waiter may already have installed this generation.
            snapshot = await asyncio.to_thread(self._capture)
            self._validate_snapshot(snapshot)
            current_version = snapshot["catalog_version"]
            if current_version == self._generation.identity["catalog_version"]:
                decision = CatalogRuntimeDecision(
                    catalog_version_used=current_version
                )
                self._last_decision = decision
                return decision
            if (
                current_version == self._failed_authority_version
                and time.monotonic() - self._failed_at
                < self._failure_retry_seconds
                and self._last_decision.demo_catalog_fallback
            ):
                return self._last_decision
            try:
                generation = await asyncio.to_thread(self._refresh)
                self._validate_snapshot(generation.snapshot)
                if (
                    generation.identity.get("catalog_version") != current_version
                    or generation.identity.get("target_identity_hash")
                    != self._target_identity_hash
                ):
                    raise RuntimeError("DEMO_CATALOG_REFRESH_IDENTITY_MISMATCH")
            except Exception as exc:
                # The initial generation is the last known good Catalog. It is
                # safe only because the fresh capture above proved unchanged
                # source identity and unchanged exact scope.
                reason = self._failure_reason(exc)
                decision = CatalogRuntimeDecision(
                    catalog_version_used=self._generation.identity[
                        "catalog_version"
                    ],
                    demo_catalog_fallback=True,
                    catalog_refresh_failure=reason,
                )
                self._last_decision = decision
                self._failed_authority_version = current_version
                self._failed_at = time.monotonic()
                logger.warning(
                    "AUTO_REFRESH_FAILED demo_catalog_fallback=true "
                    "reason=%s catalog_version_used=%s",
                    reason,
                    decision.catalog_version_used,
                    extra={
                        "demo_catalog_fallback": True,
                        "catalog_refresh_failure": reason,
                        "catalog_version_used": decision.catalog_version_used,
                    },
                )
                return decision

            # Assignment happens only after publish, complete inventory
            # verification and pin.finish all succeeded inside the factory.
            retired = deepcopy(self._generation.identity)
            if retired not in self._retired_identities:
                self._retired_identities.append(retired)
                self._retired_identities = self._retired_identities[-8:]
            self._generation = generation
            self._active_publication = generation.frozen_publication
            self._refresh_count += 1
            self._failed_authority_version = None
            self._failed_at = 0.0
            decision = CatalogRuntimeDecision(
                catalog_version_used=generation.identity["catalog_version"],
                refreshed=True,
            )
            self._last_decision = decision
            logger.warning(
                "AUTO_REFRESH_SUCCEEDED catalog_version_used=%s refresh_count=%s",
                decision.catalog_version_used,
                self._refresh_count,
                extra={
                    "catalog_auto_refresh": True,
                    "catalog_version_used": decision.catalog_version_used,
                    "catalog_refresh_count": self._refresh_count,
                },
            )
            return decision


class ExecutionAnchorTimeContext(semantic_models.StrictModel):
    """A current-turn calendar value applied to an opaque execution base."""

    surface: str = Field(min_length=1, max_length=1000)
    applied_text: str = Field(min_length=1, max_length=100)
    start: AwareDatetime
    end_exclusive: AwareDatetime
    operation: Literal["SET", "REPLACE"]
    source: Literal["CURRENT_EXPLICIT_TIME"] = "CURRENT_EXPLICIT_TIME"


class ExecutionBackedContextAnchor(semantic_models.StrictModel):
    """Minimal context continuity after a successful V1-only execution.

    The opaque question remains authoritative for unstructured clauses. V1
    output only proves that the exact execution question ran successfully; it
    never becomes a replacement semantic truth source.
    """

    schema_version: Literal["v1-execution-anchor-v1"] = "v1-execution-anchor-v1"
    anchor_id: semantic_models.Identifier
    parent_anchor_id: semantic_models.Identifier | None = None
    source_message_id: semantic_models.Identifier
    provenance: Literal["V1_EXECUTION_ANCHOR"] = "V1_EXECUTION_ANCHOR"
    completeness: Literal["PARTIAL"] = "PARTIAL"
    evidence_mode: Literal["EXECUTION_BACKED"] = "EXECUTION_BACKED"
    original_question: str = Field(min_length=1, max_length=10000)
    opaque_base_question: str = Field(min_length=1, max_length=10000)
    actual_execution_question: str = Field(min_length=1, max_length=10000)
    semantic_model_id: int = Field(strict=True, gt=0)
    requested_business_domain_ids: tuple[int, ...] = ()
    resolved_business_domain_ids: tuple[int, ...] = Field(min_length=1)
    scope_fingerprint: semantic_models.Identifier
    state_version: int = Field(strict=True, ge=1)
    revision: int = Field(strict=True, ge=1)
    time_context: ExecutionAnchorTimeContext | None = None
    confirmed_semantics: dict[str, list[str]] = Field(default_factory=dict)
    current_turn_evidence_digest: semantic_models.Identifier
    v1_response_request_id: semantic_models.Identifier
    execution_evidence_refs: tuple[semantic_models.Identifier, ...] = Field(min_length=1)
    created_at: AwareDatetime


class DemoExecutionEnvelope(semantic_models.StrictModel):
    """Execution environment proven by one successful V1 query.

    This is deliberately separate from the semantic anchor.  It carries no
    reconstructed business meaning and cannot be supplied through the public
    request model.  Tool declarations are represented only by a digest so
    secrets in tool headers are never copied into the persisted envelope.
    """

    schema_version: Literal["demo-execution-envelope-v1"] = (
        "demo-execution-envelope-v1"
    )
    envelope_id: semantic_models.Identifier
    source_message_id: semantic_models.Identifier
    provenance: Literal["V1_SUCCESSFUL_EXECUTION"] = "V1_SUCCESSFUL_EXECUTION"
    semantic_model_id: int = Field(strict=True, gt=0)
    requested_business_domain_ids: tuple[int, ...] = ()
    resolved_business_domain_ids: tuple[int, ...] = Field(min_length=1)
    database_id: int | None = Field(default=None, strict=True, gt=0)
    knowledge_base_names: tuple[str, ...] = ()
    dataset_id: str | None = Field(default=None, min_length=1, max_length=128)
    dependency_constraints: tuple[DependencyConstraint, ...] = ()
    extension_contract_digest: semantic_models.Identifier
    scope_fingerprint: semantic_models.Identifier
    identity_fingerprint: semantic_models.Identifier
    execution_evidence_refs: tuple[semantic_models.Identifier, ...] = Field(
        min_length=1
    )
    created_at: AwareDatetime


def _reseal_execution_anchor(
    anchor: ExecutionBackedContextAnchor,
    *,
    context: AuthorizedScopeContext,
    parent_anchor_id: str | None,
) -> ExecutionBackedContextAnchor:
    data = anchor.model_dump(mode="json")
    data["parent_anchor_id"] = parent_anchor_id
    data["scope_fingerprint"] = context.fingerprint()
    material = {
        key: value
        for key, value in data.items()
        if key not in {
            "schema_version",
            "anchor_id",
            "provenance",
            "completeness",
            "evidence_mode",
        }
    }
    data["anchor_id"] = "execution-anchor:" + contract_digest(material)[:32]
    return ExecutionBackedContextAnchor.model_validate(data)


def _reseal_demo_execution_envelope(
    envelope: DemoExecutionEnvelope,
    *,
    context: AuthorizedScopeContext,
    state_identity: dict[str, str],
) -> DemoExecutionEnvelope:
    data = envelope.model_dump(mode="json")
    data["scope_fingerprint"] = context.fingerprint()
    data["identity_fingerprint"] = _execution_identity_fingerprint(
        context, state_identity
    )
    material = {
        key: value
        for key, value in data.items()
        if key not in {"schema_version", "envelope_id", "provenance"}
    }
    data["envelope_id"] = (
        "demo-execution-envelope:" + contract_digest(material)[:32]
    )
    return DemoExecutionEnvelope.model_validate(data)


def _reseal_opaque_catalog_context(
    snapshot,
    *,
    previous_context: AuthorizedScopeContext,
    context: AuthorizedScopeContext,
    state_identity: dict[str, str],
) -> dict[str, Any] | None:
    """Re-seal only opaque, execution-backed state after a safe refresh.

    Structured tasks, plans, pending work, datasets, attempts and source-value
    evidence remain pinned to their original Catalog generation and are never
    migrated by this demo continuity path.
    """

    if snapshot.state is None or snapshot.plans:
        return None
    state_artifact = ScopedArtifact.model_validate(snapshot.state)
    if (
        state_artifact.context != previous_context
        or state_artifact.source_value_bindings
    ):
        return None
    state = ConversationState.model_validate(state_artifact.payload)
    if (
        state.active_topic_id is not None
        or state.topic_stack
        or state.topics
        or state.tasks
        or state.pending_records
        or state.datasets
        or state.execution_attempts
    ):
        return None

    value = deepcopy(snapshot.envelope)
    messages = value.get("messages", {})
    if not isinstance(messages, dict):
        return None
    current_old: ExecutionBackedContextAnchor | None = None
    current_new: ExecutionBackedContextAnchor | None = None
    for _message_id, record in sorted(
        messages.items(),
        key=lambda item: (
            item[1].get("context_sequence", -1)
            if isinstance(item[1], dict)
            else -1
        ),
    ):
        if not isinstance(record, dict):
            return None
        route = record.get("bridge_route")
        raw_anchor = record.get("execution_anchor")
        if route == "V1_EXECUTION_FALLBACK_NEW_TASK":
            if raw_anchor is None:
                current_old = None
                current_new = None
                continue
            old_anchor = ExecutionBackedContextAnchor.model_validate(raw_anchor)
            if (
                old_anchor.scope_fingerprint != previous_context.fingerprint()
                or old_anchor.parent_anchor_id is not None
            ):
                return None
            new_anchor = _reseal_execution_anchor(
                old_anchor,
                context=context,
                parent_anchor_id=None,
            )
            current_old, current_new = old_anchor, new_anchor
            record["execution_anchor"] = new_anchor.model_dump(mode="json")
        elif route == "V1_EXECUTION_ANCHOR_FOLLOWUP" and raw_anchor is not None:
            old_anchor = ExecutionBackedContextAnchor.model_validate(raw_anchor)
            if (
                current_old is None
                or current_new is None
                or old_anchor.scope_fingerprint != previous_context.fingerprint()
                or old_anchor.parent_anchor_id != current_old.anchor_id
            ):
                return None
            new_anchor = _reseal_execution_anchor(
                old_anchor,
                context=context,
                parent_anchor_id=current_new.anchor_id,
            )
            current_old, current_new = old_anchor, new_anchor
            record["execution_anchor"] = new_anchor.model_dump(mode="json")

        raw_execution = record.get("demo_execution_envelope")
        if raw_execution is not None:
            execution = DemoExecutionEnvelope.model_validate(raw_execution)
            if execution.scope_fingerprint != previous_context.fingerprint():
                return None
            record["demo_execution_envelope"] = _reseal_demo_execution_envelope(
                execution,
                context=context,
                state_identity=state_identity,
            ).model_dump(mode="json")

    if current_new is None:
        return None
    payload = state.model_dump(mode="json")
    resealed_state = ScopedArtifact(
        kind="CONVERSATION",
        context=context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )
    value["context_fingerprint"] = context.fingerprint()
    value["state_identity"] = dict(state_identity)
    value["state"] = resealed_state.model_dump(mode="json")
    return value


@dataclass(frozen=True)
class ExecutionAnchorUpdate:
    previous: ExecutionBackedContextAnchor | None
    current_parse: CurrentTurnSemanticParse
    time_context: ExecutionAnchorTimeContext | None
    resolved_business_domain_ids: tuple[int, ...]


@dataclass(frozen=True)
class ResolvedContextTurn:
    completed_question: str | None
    next_state: ScopedArtifact | None
    plan_state: ScopedArtifact | None
    pending_state: ScopedArtifact | None = None
    clarification_question: str | None = None
    clarification_trace: Any = None
    display: CompletedQuestionDisplay | None = None
    bridge_route: str = "V2_RESOLVED_COMPLETED_QUESTION"
    anchor_update: ExecutionAnchorUpdate | None = None


def _standalone_execution_display(
    question: str,
    display: CompletedQuestionDisplay,
) -> CompletedQuestionDisplay:
    """Keep a resolved standalone request byte-for-byte compatible with V1."""
    if display.relation != "NEW_TASK":
        return display
    material = display.model_dump(mode="python", exclude={"display_digest"})
    material["completed_question"] = question
    updated = CompletedQuestionDisplay(
        **material,
        display_digest=contract_digest(material),
    )
    if len(updated.public_message) > 500:
        raise ValueError("V2_COMPLETED_QUESTION_DISPLAY_TOO_LONG")
    return updated


def _completed_question_step(resolved: ResolvedContextTurn) -> AnalysisProcessStep | None:
    if not resolved.completed_question:
        return None
    summary = (
        resolved.display.public_message
        if resolved.display is not None
        else "补全后的完整问题：" + resolved.completed_question
    )
    if len(summary) > 500:
        return None
    return AnalysisProcessStep(
        stage="QUESTION_REWRITE",
        status="COMPLETED",
        title="补全后的完整问题",
        summary=summary,
    )


def _attach_completed_question(
    response: AgentResponse,
    resolved: ResolvedContextTurn,
) -> AgentResponse:
    """Expose exactly the question already sent to V1 in the original response."""
    step = _completed_question_step(resolved)
    if step is None:
        return response
    steps = list(response.analysis_process)
    index = next(
        (position for position, item in enumerate(steps)
         if item.stage == "QUESTION_REWRITE"),
        None,
    )
    if index is None:
        steps.insert(0, step)
    else:
        steps[index] = step
    response.analysis_process = steps[:20]
    return response


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _anchor_edit_diagnostic(
    parsed,
    *,
    context_relation: str | None,
) -> dict[str, Any]:
    explicit_slots = sorted(
        slot
        for slot, mention_ids in parsed.explicit_slot_mentions.items()
        if mention_ids
    )
    marker_slots = sorted({marker.slot_name for marker in parsed.operation_markers})
    return {
        "context_relation": context_relation,
        "mention_roles": [
            sorted({_enum_text(role) for role in mention.candidate_roles})
            for mention in parsed.mentions
        ],
        "marker_slots": marker_slots,
        "operation_hints": sorted({
            _enum_text(marker.operation_hint)
            for marker in parsed.operation_markers
        }),
        "explicit_slots": explicit_slots,
        "effective_edited_slots": sorted(set(explicit_slots) | set(marker_slots)),
        "dialogue_acts": sorted({
            _enum_text(value) for value in parsed.dialogue_act_candidates
        }),
        "topic_shift": bool(parsed.topic_shift_signals),
        "time_mention_count": 0,
        "time_evidence_source": None,
        "resolved_time_kind": None,
        "resolved_time_value": None,
    }


def _reject_anchor_edit(
    reason_code: str,
    diagnostic: dict[str, Any],
    *,
    public_code: str = "V2_EXECUTION_ANCHOR_EDIT_UNSUPPORTED",
) -> None:
    raise ExecutionAnchorEditFailure(
        reason_code,
        diagnostic,
        public_code=public_code,
    )


def _surface_time_only_context(
    question: str,
    now: datetime,
) -> tuple[str, Any, str] | None:
    """Recover one bounded natural-calendar expression from a time-only edit.

    This is a representation fallback for an execution-backed opaque anchor.
    It reuses the governed whole-expression calendar parser and accepts only a
    short utterance whose remaining text is generic edit/query grammar.  No
    metric, entity, dimension, filter, relation or catalog vocabulary is
    classified here.
    """

    text = question.strip()
    if not text or len(text) > _MAX_TIME_EDIT_QUESTION_LENGTH:
        return None
    candidates: list[tuple[int, int, str, Any]] = []
    for start in range(len(text)):
        stop = min(len(text), start + _MAX_TIME_EXPRESSION_LENGTH)
        for end in range(start + 1, stop + 1):
            surface = text[start:end].strip()
            if not surface or _TIME_EDIT_PUNCTUATION.search(surface):
                continue
            try:
                normalized = normalize_range(surface, now)
            except (RecognitionFailure, ValueError):
                continue
            candidates.append((start, end, surface, normalized))
    if not candidates:
        return None
    maximal = [
        candidate
        for candidate in candidates
        if not any(
            other[0] <= candidate[0]
            and candidate[1] <= other[1]
            and (other[0], other[1]) != (candidate[0], candidate[1])
            for other in candidates
        )
    ]
    spans = {(start, end) for start, end, _surface, _value in maximal}
    if len(spans) != 1:
        return None
    start, end = next(iter(spans))
    surface, normalized = next(
        (surface, value)
        for candidate_start, candidate_end, surface, value in maximal
        if (candidate_start, candidate_end) == (start, end)
    )
    auxiliary = _TIME_EDIT_PUNCTUATION.sub("", text[:start] + text[end:])
    if auxiliary and _TIME_EDIT_AUXILIARY.fullmatch(auxiliary) is None:
        return None
    return surface, normalized, auxiliary


def _validated_time_context(
    raw_parse: CurrentTurnSemanticParse,
    *,
    question: str,
    message_id: str,
    now: datetime,
    require_followup: bool,
    context_relation: str | None = None,
) -> tuple[CurrentTurnSemanticParse, ExecutionAnchorTimeContext]:
    """Accept a time-only effective delta without relying on raw object count."""

    parsed = CurrentTurnSemanticParse.model_validate(
        raw_parse.model_dump(mode="json")
    )
    parsed, _repairs = repair_model_parse(
        parsed,
        text=question,
        turn_id=message_id,
    )
    validated = CurrentTurnParser.parse(
        text=question,
        turn_id=message_id,
        text_ref=message_id,
        parsed=parsed,
    )
    diagnostic = _anchor_edit_diagnostic(
        validated,
        context_relation=context_relation,
    )
    effective_slots = set(diagnostic["effective_edited_slots"])
    non_time_slots = effective_slots - {"time_spec"}
    if validated.topic_shift_signals:
        _reject_anchor_edit("TOPIC_SHIFT", diagnostic)
    acts = set(diagnostic["dialogue_acts"])
    if require_followup and acts & {"NEW_TASK", "SWITCH_TOPIC", "RETURN_TO_TOPIC"}:
        _reject_anchor_edit("TOPIC_SHIFT", diagnostic)
    if non_time_slots:
        reason = (
            "MULTIPLE_EFFECTIVE_SLOT_EDITS"
            if "time_spec" in effective_slots else "NON_TIME_SLOT_EDIT"
        )
        _reject_anchor_edit(reason, diagnostic)

    time_slot_ids = set(validated.explicit_slot_mentions.get("time_spec", []))
    temporal_ids = set(validated.temporal_expressions)
    time_marker_ids = {
        marker.mention_id
        for marker in validated.operation_markers
        if marker.slot_name == "time_spec"
    }
    time_ids = time_slot_ids | temporal_ids | time_marker_ids
    time_mentions = [
        mention
        for mention in validated.mentions
        if mention.mention_id in time_ids
        and {_enum_text(role) for role in mention.candidate_roles}
            & {"TIME_RANGE", "TIME_FIELD"}
    ]
    diagnostic["time_mention_count"] = len(time_mentions)
    resolved_time_mentions = []
    unresolved_time_mentions = []
    for mention in time_mentions:
        try:
            resolved_time_mentions.append(
                (mention, normalize_range(mention.surface, now))
            )
        except (RecognitionFailure, ValueError):
            unresolved_time_mentions.append(mention)
    time_operations = {
        _enum_text(marker.operation_hint)
        for marker in validated.operation_markers
        if marker.slot_name == "time_spec"
    }
    if time_operations - {"SET", "REPLACE"}:
        _reject_anchor_edit(
            "UNSUPPORTED_OPERATION",
            diagnostic,
            public_code="V2_EXECUTION_ANCHOR_TIME_OPERATION_REQUIRED",
        )

    surface_evidence = _surface_time_only_context(question, now) if require_followup else None
    if require_followup and surface_evidence is None:
        reason = "TIME_MENTION_MISSING" if not time_mentions else "TIME_VALUE_UNRESOLVED"
        if time_mentions and not (time_slot_ids or time_marker_ids):
            reason = "TIME_SLOT_MISSING"
        _reject_anchor_edit(reason, diagnostic)

    if surface_evidence is not None:
        surface, normalized, auxiliary = surface_evidence
        diagnostic["time_evidence_source"] = (
            "MODEL_PARSE_AND_TIME_ONLY_SURFACE"
            if time_mentions else "DETERMINISTIC_TIME_ONLY_SURFACE_RECOVERY"
        )
        if auxiliary and not time_mentions:
            diagnostic["auxiliary_parse_signal"] = "AUXILIARY_PARSE_SIGNAL_ONLY"
        elif unresolved_time_mentions:
            diagnostic["auxiliary_parse_signal"] = "AUXILIARY_PARSE_SIGNAL_ONLY"
    else:
        if not time_mentions:
            _reject_anchor_edit("TIME_MENTION_MISSING", diagnostic)
        if unresolved_time_mentions:
            _reject_anchor_edit("TIME_VALUE_UNRESOLVED", diagnostic)
        normalized_values = resolved_time_mentions
        ranges = {
            (value.start, value.end_exclusive)
            for _mention, value in normalized_values
        }
        if len(ranges) != 1:
            _reject_anchor_edit("TIME_MENTION_AMBIGUOUS", diagnostic)
        mention, normalized = max(
            normalized_values,
            key=lambda pair: len(pair[0].surface),
        )
        surface = mention.surface
        diagnostic["time_evidence_source"] = "MODEL_PARSE"

    if resolved_time_mentions:
        parsed_ranges = {
            (parsed_value.start, parsed_value.end_exclusive)
            for _mention, parsed_value in resolved_time_mentions
        }
        parsed_ranges.add((normalized.start, normalized.end_exclusive))
        if len(parsed_ranges) != 1:
            _reject_anchor_edit("TIME_MENTION_AMBIGUOUS", diagnostic)

    operation = (
        "REPLACE"
        if require_followup or "REPLACE" in time_operations
        else "SET"
    )
    local_start = normalized.start.astimezone(now.tzinfo)
    local_end = normalized.end_exclusive.astimezone(now.tzinfo)
    if (
        local_start.month == local_start.day == 1
        and local_end.month == local_end.day == 1
        and local_end.year == local_start.year + 1
    ):
        applied_text = f"{local_start.year}年"
    else:
        applied_text = surface
    diagnostic.update({
        "effective_edited_slots": ["time_spec"],
        "effective_operation": operation,
        "resolved_time_kind": "RANGE",
        "resolved_time_value": {
            "start": normalized.start.isoformat(),
            "end_exclusive": normalized.end_exclusive.isoformat(),
        },
        "decision": "ACCEPT",
    })
    if require_followup:
        logger.info(
            "V2 execution-backed anchor time edit accepted",
            extra={"anchor_edit_diagnostic": diagnostic},
        )
    return parsed, ExecutionAnchorTimeContext(
        surface=surface,
        applied_text=applied_text,
        start=normalized.start,
        end_exclusive=normalized.end_exclusive,
        operation=operation,
    )


def _initial_anchor_time_context(
    raw_parse: CurrentTurnSemanticParse,
    *,
    question: str,
    message_id: str,
    now: datetime,
) -> ExecutionAnchorTimeContext | None:
    if not raw_parse.temporal_expressions:
        return None
    try:
        _parsed, context = _validated_time_context(
            raw_parse,
            question=question,
            message_id=message_id,
            now=now,
            require_followup=False,
        )
    except (RecognitionFailure, ValueError):
        # A successful V1 execution still proves the opaque base. Uncertain V2
        # slot hypotheses are retained only by digest and never promoted.
        return None
    return context


def _apply_time_to_opaque_question(
    anchor: ExecutionBackedContextAnchor,
    time_context: ExecutionAnchorTimeContext,
) -> str:
    question = anchor.actual_execution_question
    prior = anchor.time_context
    if prior is not None:
        for old in (prior.applied_text, prior.surface):
            if old and old in question:
                return question.replace(old, time_context.applied_text, 1)
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_PRIOR_TIME_NOT_LOCATABLE")
    if question.startswith("查询"):
        return "查询" + time_context.applied_text + question[len("查询"):]
    return time_context.applied_text + question


def _advance_opaque_anchor_state(
    state: ScopedArtifact,
    *,
    message_id: str,
    context: AuthorizedScopeContext,
) -> ScopedArtifact:
    current = ConversationState.model_validate(state.payload)
    if current.active_topic_id is not None:
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_NOT_CURRENT")
    data = current.model_dump(mode="python")
    data["state_version"] = current.state_version + 1
    data["recent_turn_ids"] = [
        *current.recent_turn_ids,
        message_id,
    ][-100:]
    updated = ConversationState.model_validate(data)
    payload = updated.model_dump(mode="json")
    return ScopedArtifact(
        kind="CONVERSATION",
        context=context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )


def _resolve_execution_anchor_followup(
    chat: ChatRequest,
    state: ScopedArtifact,
    anchor: ExecutionBackedContextAnchor,
    raw_parse: CurrentTurnSemanticParse,
    context: AuthorizedScopeContext,
    now: datetime,
    *,
    context_relation: str | None = None,
) -> ResolvedContextTurn:
    if (
        anchor.scope_fingerprint != context.fingerprint()
        or anchor.semantic_model_id != chat.semantic_model_id
        or anchor.requested_business_domain_ids != tuple(chat.business_domain_ids)
    ):
        raise RecognitionFailure("V2_EXECUTION_ANCHOR_SCOPE_MISMATCH")
    parsed, time_context = _validated_time_context(
        raw_parse,
        question=chat.question,
        message_id=chat.message_id,
        now=now,
        require_followup=True,
        context_relation=context_relation,
    )
    completed = _apply_time_to_opaque_question(anchor, time_context)
    next_state = _advance_opaque_anchor_state(
        state,
        message_id=chat.message_id,
        context=context,
    )
    semantic_fingerprint = contract_digest({
        "anchor_id": anchor.anchor_id,
        "operation": time_context.operation,
        "slot": "time_spec",
        "time": time_context.model_dump(mode="json"),
        "completed_question": completed,
    })
    display_material = {
        "source": "V1_EXECUTION_ANCHOR_PARTIAL",
        "message_id": chat.message_id,
        "task_id": anchor.anchor_id,
        "task_version": anchor.revision + 1,
        "plan_id": "execution-anchor:" + semantic_fingerprint[:24],
        "semantic_fingerprint": semantic_fingerprint,
        "relation": "MODIFY",
        "understanding": "基于上一轮成功执行的问题，仅替换当前明确指定的时间。",
        "completed_question": completed,
    }
    display = CompletedQuestionDisplay(
        **display_material,
        display_digest=contract_digest(display_material),
    )
    return ResolvedContextTurn(
        completed_question=completed,
        next_state=next_state,
        plan_state=None,
        display=display,
        bridge_route="V1_EXECUTION_ANCHOR_FOLLOWUP",
        anchor_update=ExecutionAnchorUpdate(
            previous=anchor,
            current_parse=parsed,
            time_context=time_context,
            resolved_business_domain_ids=anchor.resolved_business_domain_ids,
        ),
    )


def _execution_evidence_refs(response: AgentResponse) -> tuple[str, ...]:
    refs = tuple(sorted({
        f"{item.kind}:{item.source_ref}"
        for item in response.evidence
        if item.kind and item.source_ref
    }))
    if not any(item.kind == "QUERY_RESULT" for item in response.evidence):
        return ()
    return refs


def _execution_extension_digest(chat: ChatRequest) -> str:
    """Hash extension inputs without persisting tool or MCP credentials."""

    return contract_digest({
        "tools": chat.model_dump(mode="json", include={"tools"})["tools"],
        "skills": chat.model_dump(mode="json", include={"skills"})["skills"],
        "mcp": chat.model_dump(mode="json", include={"mcp"})["mcp"],
        "web_search": chat.web_search,
    })


def _execution_identity_fingerprint(
    context: AuthorizedScopeContext,
    state_identity: dict[str, str],
) -> str:
    return contract_digest({
        "context": context.fingerprint(),
        "identity": state_identity,
    })


def _finalize_demo_execution_envelope(
    update: ExecutionAnchorUpdate,
    *,
    chat: ChatRequest,
    response: AgentResponse,
    context: AuthorizedScopeContext,
    state_identity: dict[str, str],
    now: datetime,
) -> DemoExecutionEnvelope | None:
    evidence_refs = _execution_evidence_refs(response)
    if response.status != "COMPLETED" or response.error_code or not evidence_refs:
        return None
    material = {
        "source_message_id": chat.message_id,
        "semantic_model_id": chat.semantic_model_id,
        "requested_business_domain_ids": list(chat.business_domain_ids),
        "resolved_business_domain_ids": list(update.resolved_business_domain_ids),
        "database_id": chat.database_id,
        "knowledge_base_names": list(chat.knowledge_base_names),
        "dataset_id": chat.dataset_id,
        "dependency_constraints": [
            item.model_dump(mode="json") for item in chat.dependency_constraints
        ],
        "extension_contract_digest": _execution_extension_digest(chat),
        "scope_fingerprint": context.fingerprint(),
        "identity_fingerprint": _execution_identity_fingerprint(
            context, state_identity
        ),
        "execution_evidence_refs": list(evidence_refs),
        "created_at": now.isoformat(),
    }
    return DemoExecutionEnvelope(
        **material,
        envelope_id="demo-execution-envelope:" + contract_digest(material)[:32],
    )


def _finalize_execution_anchor(
    update: ExecutionAnchorUpdate,
    *,
    chat: ChatRequest,
    completed_question: str,
    response: AgentResponse,
    context: AuthorizedScopeContext,
    state_version: int,
    now: datetime,
) -> ExecutionBackedContextAnchor | None:
    evidence_refs = _execution_evidence_refs(response)
    if response.status != "COMPLETED" or response.error_code or not evidence_refs:
        return None
    previous = update.previous
    original_question = previous.original_question if previous else chat.question
    opaque_base = previous.opaque_base_question if previous else chat.question
    revision = previous.revision + 1 if previous else 1
    parent_anchor_id = previous.anchor_id if previous else None
    time_context = (
        update.time_context
        if update.time_context is not None
        else _initial_anchor_time_context(
            update.current_parse,
            question=chat.question,
            message_id=chat.message_id,
            now=now,
        )
    )
    material = {
        "parent_anchor_id": parent_anchor_id,
        "source_message_id": chat.message_id,
        "original_question": original_question,
        "opaque_base_question": opaque_base,
        "actual_execution_question": completed_question,
        "semantic_model_id": chat.semantic_model_id,
        "requested_business_domain_ids": list(chat.business_domain_ids),
        "resolved_business_domain_ids": list(update.resolved_business_domain_ids),
        "scope_fingerprint": context.fingerprint(),
        "state_version": state_version,
        "revision": revision,
        "time_context": time_context.model_dump(mode="json") if time_context else None,
        "confirmed_semantics": {
            "time": [time_context.applied_text] if time_context else [],
        },
        "current_turn_evidence_digest": contract_digest(
            update.current_parse.model_dump(mode="json")
        ),
        "v1_response_request_id": str(response.request_id),
        "execution_evidence_refs": list(evidence_refs),
        "created_at": now.isoformat(),
    }
    return ExecutionBackedContextAnchor(
        **material,
        anchor_id="execution-anchor:" + contract_digest(material)[:32],
    )


def validate_context_v1_settings(settings: Settings) -> dict[str, Any]:
    if settings.runtime_mode != "V2_CONTEXT_V1_EXECUTION":
        raise RuntimeError("V2_CONTEXT_V1_RUNTIME_NOT_SELECTED")
    if settings.session_store_mode != "redis" or not settings.effective_redis_url():
        raise RuntimeError("V2_CONTEXT_V1_REDIS_REQUIRED")
    if not settings.intent_model_enabled or not settings.intent_model_api_key:
        raise RuntimeError("V2_CONTEXT_V1_MODEL_REQUIRED")
    if settings.intent_model_enable_thinking is not False:
        raise RuntimeError("V2_CONTEXT_V1_THINKING_MUST_BE_DISABLED")
    if settings.intent_model_max_retries != 0:
        raise RuntimeError("V2_CONTEXT_V1_MODEL_RETRY_MUST_BE_ZERO")
    domains = settings.limited_scalar_business_domain_ids
    if (
        not domains
        or any(type(value) is not int or value <= 0 for value in domains)
        or len(domains) != len(set(domains))
        or len(domains) != 1
    ):
        raise RuntimeError("V2_CONTEXT_V1_EXPLICIT_SCOPE_REQUIRED")
    required = {
        "store_namespace": settings.limited_scalar_store_namespace,
        "deployment_id": settings.limited_scalar_deployment_id,
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "catalog_target_identity_hash": settings.limited_scalar_catalog_target_identity_hash,
        "oagnet_source_digest": settings.limited_scalar_oagnet_source_digest,
    }
    missing = sorted(name for name, value in required.items() if not str(value).strip())
    if missing:
        raise RuntimeError("V2_CONTEXT_V1_CONFIGURATION_MISSING:" + ",".join(missing))
    if ":v2-limited-scalar:" not in settings.limited_scalar_store_namespace:
        raise RuntimeError("V2_CONTEXT_V1_SOURCE_NAMESPACE_INVALID")
    actual_digest = source_bundle_digest(
        settings.limited_scalar_oagnet_root, OAGNET_RUNTIME_FILES
    )
    if actual_digest != settings.limited_scalar_oagnet_source_digest:
        raise RuntimeError("V2_CONTEXT_V1_OAGNET_SOURCE_DRIFT")
    return {
        "runtime_mode": settings.runtime_mode,
        "scope": {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": list(domains),
        },
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "oagnet_source_digest": actual_digest,
    }


def _resolve_context_v1_request_scope(
    chat: ChatRequest,
    deployment_scope: dict[str, Any],
) -> dict[str, Any]:
    """Resolve ingress scope without changing the caller's selection mode.

    The deployment domain list is the reviewed set currently owned by the
    semantic model. An omitted request list therefore selects MODEL_WIDE and
    resolves to that set; it is not rewritten into an explicit-domain grant.
    Legacy transport metadata such as ``department`` is deliberately absent
    from this decision.
    """
    expected_model = deployment_scope["semantic_model_id"]
    model_domains = list(deployment_scope["business_domain_ids"])
    requested = list(chat.business_domain_ids)
    if (
        chat.semantic_model_id != expected_model
        or len(requested) > 1
        or any(domain not in model_domains for domain in requested)
    ):
        raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")
    return {
        "semantic_model_id": expected_model,
        "requested_business_domain_ids": requested,
        "resolved_business_domain_ids": requested or model_domains,
        "selection_mode": (
            "EXPLICIT_DOMAINS" if requested else "MODEL_WIDE"
        ),
    }


def _build_context_dependencies(settings: Settings) -> ContextV1ExternalDependencies:
    _prepend_runtime_roots(settings)
    roots = {
        "catalog_publication": settings.limited_scalar_oagnet_root,
        "catalog_registry": settings.limited_scalar_oagnet_root,
        "catalog_store": settings.limited_scalar_oagnet_root,
        "vector_store": settings.limited_scalar_oagnet_root,
    }
    modules = {
        name: _import_runtime_module(name, root) for name, root in roots.items()
    }
    catalog_runtime = None
    if (
        settings.limited_scalar_catalog_access == "LIVE_READ_ONLY_SNAPSHOT"
        and settings.demo_mode
    ):
        initial = _build_live_read_only_catalog_generation(
            settings,
            modules,
            enforce_config_versions=False,
        )
        expected_scope = {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": list(
                settings.limited_scalar_business_domain_ids
            ),
            "scope_mode": "EXPLICIT_DOMAINS",
        }
        capture_catalog = modules["catalog_publication"].capture_catalog

        def capture_current():
            return capture_catalog(
                expected_scope["semantic_model_id"],
                expected_scope["business_domain_ids"],
            )

        def refresh_current():
            return _build_live_read_only_catalog_generation(
                settings,
                modules,
                enforce_config_versions=False,
            )

        catalog_runtime = DemoCatalogAutoRefresh(
            initial=initial,
            capture=capture_current,
            refresh=refresh_current,
            expected_scope=expected_scope,
        )
        publication = catalog_runtime
    elif settings.limited_scalar_catalog_access == "LIVE_READ_ONLY_SNAPSHOT":
        publication = _open_live_read_only_catalog(settings, modules)
    else:
        store = modules["catalog_store"].open_catalog_store(
            initialize=False,
            expected_target_identity_hash=(
                settings.limited_scalar_catalog_target_identity_hash
            ),
        )
        publication = modules["catalog_publication"].CatalogPublication(
            store,
            modules["catalog_registry"].RedisCatalogReleaseRegistry(
                store.catalog_target_identity
            ),
        )
    return ContextV1ExternalDependencies(
        publication=publication,
        model=RecognitionModelClient(settings),
        redis=Redis.from_url(
            settings.effective_redis_url(),
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=35,
        ),
        module_origins={
            name: str(Path(module.__file__).resolve())
            for name, module in modules.items()
        },
        catalog_runtime=catalog_runtime,
    )


class V2ContextV1ExecutionBridge:
    """Resolve one turn with V2, then invoke the original V1 workflow once."""

    def __init__(
        self,
        *,
        store: RedisScalarSessionStore,
        context_resolver: Callable[
            [ChatRequest, TrustedIdentity], AuthorizedScopeContext
        ],
        context_planner: Callable[
            [ChatRequest, TrustedIdentity, ScopedArtifact | None,
             tuple[ScopedArtifact, ...], ScopedArtifact | None],
            Awaitable[ResolvedContextTurn],
        ],
        v1_executor: Callable[
            [ChatRequest, TrustedIdentity], Awaitable[AgentResponse]
        ],
        clock: Callable[[], datetime],
        startup_receipt: dict[str, Any],
        readiness_probe: Callable[[], Awaitable[dict[str, bool]]],
        demo_mode: bool = False,
        catalog_runtime: DemoCatalogAutoRefresh | None = None,
    ):
        self.store = store
        self.context_resolver = context_resolver
        self.context_planner = context_planner
        self.v1_executor = v1_executor
        self.clock = clock
        self.startup_receipt = startup_receipt
        self.demo_mode = demo_mode
        self.catalog_runtime = catalog_runtime
        self._readiness_probe = readiness_probe
        self._locks: dict[str, asyncio.Lock] = {}
        self._readiness_lock = asyncio.Lock()
        self._readiness_cache: dict[str, bool] | None = None
        self._readiness_cached_at = 0.0

    async def _ensure_catalog_runtime(
        self,
        chat: ChatRequest,
        identity: TrustedIdentity,
    ) -> CatalogRuntimeDecision | None:
        if self.catalog_runtime is None:
            return None
        return await self.catalog_runtime.ensure_current(chat, identity)

    async def _load_context_snapshot(
        self,
        context: AuthorizedScopeContext,
        state_identity: dict[str, str],
    ):
        snapshot = await self.store.load(context, state_identity)
        if (
            self.catalog_runtime is None
            or snapshot.state is not None
            or snapshot.plans
            or snapshot.envelope.get("messages")
        ):
            return snapshot
        for identity in self.catalog_runtime.retired_identities:
            previous_context = context.model_copy(
                update={
                    "catalog_pin": CatalogPinIdentity.model_validate(identity)
                }
            )
            previous = await self.store.load(previous_context, state_identity)
            resealed = _reseal_opaque_catalog_context(
                previous,
                previous_context=previous_context,
                context=context,
                state_identity=state_identity,
            )
            if resealed is None:
                continue
            installed = await self.store.install_catalog_resealed_context(
                envelope=resealed,
                context=context,
                state_identity=state_identity,
            )
            if installed.state is not None:
                logger.warning(
                    "CATALOG_CONTEXT_RESEALED previous=%s current=%s",
                    previous_context.catalog_pin.catalog_version,
                    context.catalog_pin.catalog_version,
                    extra={
                        "catalog_context_resealed": True,
                        "catalog_version_previous": (
                            previous_context.catalog_pin.catalog_version
                        ),
                        "catalog_version_current": (
                            context.catalog_pin.catalog_version
                        ),
                    },
                )
                return installed
        return snapshot

    @staticmethod
    def _identity(chat: ChatRequest, identity: TrustedIdentity) -> dict[str, str]:
        return {
            "conversation_id": chat.conversation_id,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "application_id": chat.application_id,
        }

    @staticmethod
    def _fingerprint(chat: ChatRequest, identity: TrustedIdentity) -> str:
        return contract_digest({
            "chat": chat.model_dump(mode="json", exclude={"history"}),
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
        })

    @staticmethod
    def _pending(snapshot, context: AuthorizedScopeContext) -> ScopedArtifact | None:
        if snapshot.state is None:
            return None
        state = ConversationState.model_validate(snapshot.state.payload)
        active = state.pending
        if active is None or active.status != "ACTIVE":
            return None
        for record in reversed(list(snapshot.envelope.get("messages", {}).values())):
            raw = record.get("pending_state") if isinstance(record, dict) else None
            if raw is None:
                continue
            artifact = ScopedArtifact.model_validate(raw)
            if artifact.kind != "PENDING" or artifact.context != context:
                raise ValueError("PERSISTED_PENDING_SCOPE_MISMATCH")
            if isinstance(artifact.payload, dict) and artifact.payload.get("pending_id") == active.pending_id:
                return artifact
        raise ValueError("PERSISTED_PENDING_RESUME_MISSING")

    @staticmethod
    def _execution_anchor(
        snapshot,
        context: AuthorizedScopeContext,
    ) -> ExecutionBackedContextAnchor | None:
        """Return only the newest successful opaque-task anchor.

        A newer fallback NEW_TASK without a successful execution anchor clears
        the candidate. This preserves the existing barrier and prevents a
        short turn from attaching to an older task.
        """

        if snapshot.state is None:
            return None
        state = ConversationState.model_validate(snapshot.state.payload)
        if state.active_topic_id is not None:
            return None
        current: ExecutionBackedContextAnchor | None = None
        records = sorted(
            snapshot.envelope.get("messages", {}).values(),
            key=lambda item: (
                item.get("context_sequence", -1)
                if isinstance(item, dict) else -1
            ),
        )
        for record in records:
            if not isinstance(record, dict):
                continue
            route = record.get("bridge_route")
            raw = record.get("execution_anchor")
            if route == "V1_EXECUTION_FALLBACK_NEW_TASK":
                current = (
                    ExecutionBackedContextAnchor.model_validate(raw)
                    if raw is not None else None
                )
            elif route == "V1_EXECUTION_ANCHOR_FOLLOWUP" and raw is not None:
                candidate = ExecutionBackedContextAnchor.model_validate(raw)
                if current is None or candidate.parent_anchor_id != current.anchor_id:
                    raise ValueError("V1_EXECUTION_ANCHOR_CHAIN_MISMATCH")
                current = candidate
        if current is not None and current.scope_fingerprint != context.fingerprint():
            raise ValueError("V1_EXECUTION_ANCHOR_SCOPE_MISMATCH")
        return current

    @staticmethod
    def _demo_execution_envelope(
        snapshot,
        context: AuthorizedScopeContext,
        state_identity: dict[str, str],
        anchor: ExecutionBackedContextAnchor,
    ) -> DemoExecutionEnvelope | None:
        """Return the envelope paired with the newest execution-backed anchor."""

        current: DemoExecutionEnvelope | None = None
        records = sorted(
            snapshot.envelope.get("messages", {}).values(),
            key=lambda item: (
                item.get("context_sequence", -1)
                if isinstance(item, dict) else -1
            ),
        )
        for record in records:
            if not isinstance(record, dict):
                continue
            route = record.get("bridge_route")
            raw = record.get("demo_execution_envelope")
            if route == "V1_EXECUTION_FALLBACK_NEW_TASK":
                current = (
                    DemoExecutionEnvelope.model_validate(raw)
                    if raw is not None else None
                )
            elif route == "V1_EXECUTION_ANCHOR_FOLLOWUP" and raw is not None:
                current = DemoExecutionEnvelope.model_validate(raw)
        if current is None:
            return None
        expected_identity = _execution_identity_fingerprint(context, state_identity)
        if (
            current.scope_fingerprint != context.fingerprint()
            or current.identity_fingerprint != expected_identity
            or current.semantic_model_id != context.authorized_scope.semantic_model_id
            or current.source_message_id != anchor.source_message_id
        ):
            raise ValueError("DEMO_EXECUTION_ENVELOPE_SCOPE_MISMATCH")
        return current

    @staticmethod
    def _apply_demo_execution_envelope(
        chat: ChatRequest,
        envelope: DemoExecutionEnvelope,
    ) -> ChatRequest:
        if tuple(chat.business_domain_ids) != envelope.requested_business_domain_ids:
            raise ValueError("DEMO_EXECUTION_ENVELOPE_REQUEST_SCOPE_MISMATCH")
        if (
            chat.database_id != envelope.database_id
            or tuple(chat.knowledge_base_names) != envelope.knowledge_base_names
        ):
            raise ValueError("DEMO_EXECUTION_ENVELOPE_REQUEST_SCOPE_MISMATCH")
        if chat.dataset_id not in {None, envelope.dataset_id}:
            raise ValueError("DEMO_EXECUTION_ENVELOPE_DATASET_MISMATCH")
        current_constraint_values = tuple(
            item.model_dump(mode="json") for item in chat.dependency_constraints
        )
        envelope_constraint_values = tuple(
            item.model_dump(mode="json")
            for item in envelope.dependency_constraints
        )
        if (
            current_constraint_values
            and current_constraint_values != envelope_constraint_values
        ):
            raise ValueError("DEMO_EXECUTION_ENVELOPE_DEPENDENCY_MISMATCH")
        if _execution_extension_digest(chat) != envelope.extension_contract_digest:
            raise ValueError("DEMO_EXECUTION_ENVELOPE_EXTENSION_MISMATCH")

        execution_chat = chat.model_copy(update={
            "database_id": envelope.database_id,
            "knowledge_base_names": list(envelope.knowledge_base_names),
            "dataset_id": envelope.dataset_id,
            "dependency_constraints": list(envelope.dependency_constraints),
        })
        execution_chat._demo_execution_resolved_business_domain_ids = (
            envelope.resolved_business_domain_ids
        )
        return execution_chat

    @staticmethod
    def _demo_fallback_response(
        *,
        chat: ChatRequest,
        resolved: ResolvedContextTurn,
        previous: AgentResponse,
        now: datetime,
    ) -> AgentResponse:
        fallback = previous.model_copy(deep=True, update={
            "request_id": uuid4(),
            "conversation_id": chat.conversation_id,
            "status": "COMPLETED",
            "error_code": None,
            "answer": previous.answer,
            "evidence": [],
            "dataset_id": None,
            "dataset_ids": [],
            "result_file_url": None,
            "chart_specs": [],
            "semantic_model_id": chat.semantic_model_id,
            "database_id": chat.database_id,
            "requested_business_domain_ids": list(chat.business_domain_ids),
            "business_domain_selection_mode": (
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
            "created_at": now,
            "reliability": ReliabilityReport(
                level="DEGRADED",
                score=0,
                gates={"demo_fallback": True, "new_query_result": False},
                warnings=[
                    "DEMO_FALLBACK_PREVIOUS_RESULT; no current QUERY_RESULT evidence"
                ],
            ),
            "analysis_process": [],
        })
        fallback = _attach_completed_question(fallback, resolved)
        return fallback

    @staticmethod
    def _demo_failure_code(response: AgentResponse) -> str | None:
        return response._upstream_error_code or response.error_code

    @staticmethod
    def _demo_question_without_time(
        resolved: ResolvedContextTurn,
    ) -> str | None:
        update = resolved.anchor_update
        if update is None or update.time_context is None:
            return None
        question = resolved.completed_question or ""
        for value in dict.fromkeys((
            update.time_context.applied_text,
            update.time_context.surface,
        )):
            if value and value in question:
                candidate = question.replace(value, "", 1)
                candidate = re.sub(r"^查询[，,、\s]+", "查询", candidate)
                candidate = re.sub(r"[，,、\s]+([。！？])", r"\1", candidate)
                candidate = re.sub(r"[，,、]{2,}", "，", candidate).strip()
                if candidate and candidate != question:
                    return candidate
        return None

    @staticmethod
    def _demo_retry_request(chat: ChatRequest, question: str) -> ChatRequest:
        retry = chat.model_copy(update={
            "message_id": (
                "demo-no-time-"
                + contract_digest({
                    "message_id": chat.message_id,
                    "question": question,
                })[:32]
            ),
            "question": question,
            "history": [],
        })
        retry._completed_question_execution = True
        retry._demo_execution_resolved_business_domain_ids = (
            chat._demo_execution_resolved_business_domain_ids
        )
        return retry

    @staticmethod
    def _demo_retry_response(response: AgentResponse) -> AgentResponse:
        reliability = response.reliability or ReliabilityReport(
            level="DEGRADED", score=0, gates={}, warnings=[]
        )
        response.reliability = reliability.model_copy(update={
            "level": "DEGRADED",
            "gates": {
                **reliability.gates,
                "demo_fallback": True,
                "demo_retry_without_time": True,
            },
        })
        return response

    @staticmethod
    def _envelope_source_response(
        snapshot,
        envelope: DemoExecutionEnvelope,
    ) -> AgentResponse | None:
        record = snapshot.message(envelope.source_message_id)
        if not isinstance(record, dict) or record.get("status") != "SUCCEEDED":
            return None
        raw = record.get("response")
        if raw is None:
            return None
        response = AgentResponse.model_validate(raw)
        return response if _execution_evidence_refs(response) else None

    @staticmethod
    def _terminal_response(
        chat: ChatRequest,
        *,
        status: str,
        answer: str,
        error_code: str | None = None,
        clarification_trace=None,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status=status,
            error_code=error_code,
            intent=PrimaryIntent.METRIC_QUERY,
            intent_source="V2_CONTEXT_ENGINE",
            intent_confidence=1.0,
            answer=answer,
            clarification_questions=(
                [answer] if status == "NEEDS_CLARIFICATION" else []
            ),
            clarification_decision_traces=(
                [clarification_trace] if clarification_trace is not None else []
            ),
            semantic_model_id=chat.semantic_model_id,
            database_id=chat.database_id,
            requested_business_domain_ids=list(chat.business_domain_ids),
            business_domain_selection_mode=(
                "EXPLICIT" if chat.business_domain_ids else "AUTO"
            ),
        )

    async def readiness(self) -> dict[str, bool]:
        now = time.monotonic()
        if self._readiness_cache is not None and now - self._readiness_cached_at < 5:
            return dict(self._readiness_cache)
        async with self._readiness_lock:
            checks = await self._readiness_probe()
            self._readiness_cache = {name: bool(value) for name, value in checks.items()}
            self._readiness_cached_at = time.monotonic()
            return dict(self._readiness_cache)

    async def _cached_or_conflict(self, snapshot, chat, fingerprint):
        prior = snapshot.message(chat.message_id)
        if prior is None:
            return None
        if prior.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        if prior.get("status") == "SUCCEEDED" and prior.get("response"):
            return AgentResponse.model_validate(prior["response"])
        return self._terminal_response(
            chat,
            status="SAFE_FALLBACK",
            error_code="EXECUTION_OUTCOME_PENDING_REVIEW",
            answer="该请求已被受理，不会重复调用理解模型或查询链。",
        )

    async def check_message_conflict(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> None:
        await self._ensure_catalog_runtime(chat, identity)
        context = self.context_resolver(chat, identity)
        snapshot = await self._load_context_snapshot(
            context, self._identity(chat, identity)
        )
        fingerprint = self._fingerprint(chat, identity)
        prior = snapshot.message(chat.message_id)
        if prior is not None and prior.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)
        guard = await self.store.idempotency_record(snapshot, chat.message_id)
        if guard is not None and guard.get("request_fingerprint") != fingerprint:
            raise MessageIdReuseConflictError(chat.message_id)

    async def handle(
        self, chat: ChatRequest, identity: TrustedIdentity
    ) -> AgentResponse:
        await self._ensure_catalog_runtime(chat, identity)
        context = self.context_resolver(chat, identity)
        state_identity = self._identity(chat, identity)
        fingerprint = self._fingerprint(chat, identity)
        lock = self._locks.setdefault(
            contract_digest([context.fingerprint(), *state_identity.values()]),
            asyncio.Lock(),
        )
        async with lock:
            snapshot = await self._load_context_snapshot(context, state_identity)
            cached = await self._cached_or_conflict(snapshot, chat, fingerprint)
            if cached is not None:
                return cached
            guard = await self.store.idempotency_record(snapshot, chat.message_id)
            if guard is not None:
                if guard.get("request_fingerprint") != fingerprint:
                    raise MessageIdReuseConflictError(chat.message_id)
                return self._terminal_response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code="EXECUTION_SESSION_EXPIRED_REUSE_REJECTED",
                    answer="原会话状态已过期，请使用新的消息标识重新提出完整问题。",
                )
            pending = self._pending(snapshot, context)
            context_chat = chat.model_copy(update={"history": []})
            try:
                resolved = await self.context_planner(
                    context_chat,
                    identity,
                    snapshot.state,
                    snapshot.plans,
                    pending,
                )
            except ContextProposalFailure as exc:
                anchor = self._execution_anchor(snapshot, context)
                raw_parse = getattr(exc, "current_turn_parse", None)
                resolved = None
                anchor_failure = None
                if (
                    anchor is not None
                    and snapshot.state is not None
                    and raw_parse is not None
                    and exc.context_status != "AMBIGUOUS"
                ):
                    try:
                        resolved = _resolve_execution_anchor_followup(
                            context_chat,
                            snapshot.state,
                            anchor,
                            CurrentTurnSemanticParse.model_validate(raw_parse),
                            context,
                            self.clock(),
                            context_relation=(
                                exc.context_trace.get("FINAL_RELATION")
                                or exc.context_status
                            ),
                        )
                    except (RecognitionFailure, ValueError) as anchor_exc:
                        anchor_failure = anchor_exc
                        diagnostic = getattr(anchor_exc, "diagnostic", None)
                        if diagnostic is not None:
                            logger.info(
                                "V2 execution-backed anchor edit rejected",
                                extra={
                                    "message_id": chat.message_id,
                                    "anchor_edit_diagnostic": diagnostic,
                                },
                            )
                if resolved is not None:
                    logger.info(
                        "V2 context resolved a constrained execution-backed followup",
                        extra={
                            "message_id": chat.message_id,
                            "anchor_id": anchor.anchor_id if anchor else None,
                            "bridge_route": resolved.bridge_route,
                        },
                    )
                elif anchor is not None and anchor_failure is not None:
                    response = self._terminal_response(
                        chat,
                        status="NEEDS_CLARIFICATION",
                        error_code=str(anchor_failure),
                        answer="当前修改无法在上一轮执行问题上安全补全，请提供完整问题。",
                    )
                elif anchor is not None and exc.context_status != "AMBIGUOUS":
                    response = self._terminal_response(
                        chat,
                        status="SAFE_FALLBACK",
                        error_code="CONTEXT_UNSUPPORTED_FOR_FALLBACK_TASK",
                        answer="上一任务仅由原执行链处理，当前短追问缺少可安全继承的 V2 任务状态。",
                    )
                elif exc.context_status == "AMBIGUOUS":
                    response = self._terminal_response(
                        chat,
                        status="NEEDS_CLARIFICATION",
                        error_code="V2_CONTEXT_AMBIGUOUS",
                        answer="请明确您要继续哪个已有任务。",
                    )
                else:
                    response = self._terminal_response(
                        chat,
                        status="SAFE_FALLBACK",
                        error_code="V2_CONTEXT_UNRESOLVED",
                        answer="当前追问无法安全确定所引用的任务，请补充完整问题。",
                    )
                if resolved is None:
                    return await self._save_context_response(
                        snapshot, chat, identity, context, fingerprint,
                        ResolvedContextTurn(None, None, None), response,
                        v1_execution_called=False,
                    )
            except RecognitionFailure as exc:
                response = self._terminal_response(
                    chat,
                    status="SAFE_FALLBACK",
                    error_code=str(exc),
                    answer="V2 上下文理解未能形成可验证的完整问题。",
                )
                return await self._save_context_response(
                    snapshot, chat, identity, context, fingerprint,
                    ResolvedContextTurn(None, None, None), response,
                    v1_execution_called=False,
                )

            if resolved.clarification_question is not None:
                response = self._terminal_response(
                    chat,
                    status="NEEDS_CLARIFICATION",
                    answer=resolved.clarification_question,
                    clarification_trace=resolved.clarification_trace,
                )
                return await self._save_context_response(
                    snapshot, chat, identity, context, fingerprint,
                    resolved, response, v1_execution_called=False,
                )
            if not resolved.completed_question:
                raise ValueError("V2_COMPLETED_QUESTION_REQUIRED")
            progress = _completed_question_step(resolved)
            if progress is not None:
                await emit_progress(
                    "INTENT_RECOGNITION",
                    "COMPLETED",
                    progress.summary,
                    completed_question_digest=(
                        resolved.display.display_digest
                        if resolved.display is not None else None
                    ),
                )
            running = await self.store.begin_context(
                snapshot,
                planned_state=resolved.next_state,
                plan_state=resolved.plan_state,
                pending_state=resolved.pending_state,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                context=context,
                state_identity=state_identity,
                bridge_route=resolved.bridge_route,
                started_at=self.clock(),
            )
            execution_chat = chat.model_copy(update={
                "question": resolved.completed_question,
                "history": [],
            })
            execution_chat._completed_question_execution = True
            reused_envelope = None
            previous_demo_response = None
            if (
                self.demo_mode
                and resolved.bridge_route == "V1_EXECUTION_ANCHOR_FOLLOWUP"
                and resolved.anchor_update is not None
                and resolved.anchor_update.previous is not None
            ):
                reused_envelope = self._demo_execution_envelope(
                    snapshot,
                    context,
                    state_identity,
                    resolved.anchor_update.previous,
                )
                if reused_envelope is not None:
                    execution_chat = self._apply_demo_execution_envelope(
                        execution_chat, reused_envelope
                    )
                    execution_chat.question = resolved.completed_question
                    execution_chat.history = []
                    execution_chat._completed_question_execution = True
                    previous_demo_response = self._envelope_source_response(
                        snapshot, reused_envelope
                    )
                    logger.info(
                        "demo execution envelope reused",
                        extra={
                            "message_id": chat.message_id,
                            "source_message_id": reused_envelope.source_message_id,
                            "resolved_business_domain_ids": list(
                                reused_envelope.resolved_business_domain_ids
                            ),
                        },
                    )
            demo_fallback = False
            demo_fallback_reason = None
            demo_fallback_source = None
            hide_demo_execution_progress = bool(
                self.demo_mode
                and reused_envelope is not None
                and previous_demo_response is not None
            )
            try:
                if hide_demo_execution_progress:
                    with progress_scope(lambda _event: None):
                        response = await self.v1_executor(execution_chat, identity)
                else:
                    response = await self.v1_executor(execution_chat, identity)
                response = _attach_completed_question(response, resolved)
                if (
                    hide_demo_execution_progress
                    and (response.status != "COMPLETED" or response.error_code)
                ):
                    failure_code = self._demo_failure_code(response)
                    retry_question = self._demo_question_without_time(resolved)
                    if (
                        failure_code in _DEMO_RETRY_WITHOUT_TIME_CODES
                        and retry_question is not None
                    ):
                        demo_fallback_reason = failure_code
                        retry_chat = self._demo_retry_request(
                            execution_chat, retry_question
                        )
                        try:
                            with progress_scope(lambda _event: None):
                                retry_response = await self.v1_executor(
                                    retry_chat, identity
                                )
                        except Exception as retry_exc:
                            logger.warning(
                                "demo retry without time raised",
                                extra={
                                    "message_id": chat.message_id,
                                    "demo_fallback_reason": failure_code,
                                    "retry_error": type(retry_exc).__name__,
                                },
                            )
                            retry_response = None
                        if (
                            retry_response is not None
                            and retry_response.status == "COMPLETED"
                            and not retry_response.error_code
                            and _execution_evidence_refs(retry_response)
                        ):
                            response = self._demo_retry_response(retry_response)
                            response = _attach_completed_question(response, resolved)
                            demo_fallback_source = "RETRY_WITHOUT_TIME"
                        else:
                            response = self._demo_fallback_response(
                                chat=chat,
                                resolved=resolved,
                                previous=previous_demo_response,
                                now=self.clock(),
                            )
                            demo_fallback_source = "PRIOR_SUCCESSFUL_RESULT"
                        demo_fallback = True
            except Exception as exc:
                try:
                    await self.store.mark_unknown(
                        running,
                        message_id=chat.message_id,
                        request_fingerprint=fingerprint,
                        reason_code="V1_EXECUTION_OUTCOME_UNKNOWN",
                        context=context,
                        state_identity=state_identity,
                        observed_at=self.clock(),
                    )
                except (StateTransitionError, ValueError):
                    pass
                raise
            if demo_fallback:
                logger.warning(
                    "demo query fallback completed",
                    extra={
                        "message_id": chat.message_id,
                        "source_message_id": reused_envelope.source_message_id,
                        "demo_fallback": True,
                        "demo_fallback_reason": demo_fallback_reason,
                        "demo_fallback_source": demo_fallback_source,
                    },
                )
            execution_anchor = (
                _finalize_execution_anchor(
                    resolved.anchor_update,
                    chat=chat,
                    completed_question=resolved.completed_question,
                    response=response,
                    context=context,
                    state_version=running.state_version,
                    now=self.clock(),
                )
                if resolved.anchor_update is not None and not demo_fallback else None
            )
            demo_execution_envelope = (
                _finalize_demo_execution_envelope(
                    resolved.anchor_update,
                    chat=execution_chat,
                    response=response,
                    context=context,
                    state_identity=state_identity,
                    now=self.clock(),
                )
                if (
                    self.demo_mode
                    and resolved.anchor_update is not None
                    and not demo_fallback
                ) else None
            )
            await self.store.finish_context(
                running,
                message_id=chat.message_id,
                request_fingerprint=fingerprint,
                response=response,
                context=context,
                state_identity=state_identity,
                v1_execution_called=True,
                execution_anchor=(
                    execution_anchor.model_dump(mode="json")
                    if execution_anchor is not None else None
                ),
                demo_execution_envelope=(
                    demo_execution_envelope.model_dump(mode="json")
                    if demo_execution_envelope is not None else None
                ),
                reused_demo_execution_envelope_id=(
                    reused_envelope.envelope_id
                    if reused_envelope is not None else None
                ),
                demo_fallback=demo_fallback,
                demo_fallback_reason=demo_fallback_reason,
                demo_fallback_source=demo_fallback_source,
            )
            return response

    async def _save_context_response(
        self,
        snapshot,
        chat,
        identity,
        context,
        fingerprint,
        resolved,
        response,
        *,
        v1_execution_called,
    ):
        running = await self.store.begin_context(
            snapshot,
            planned_state=resolved.next_state,
            plan_state=resolved.plan_state,
            pending_state=resolved.pending_state,
            message_id=chat.message_id,
            request_fingerprint=fingerprint,
            context=context,
            state_identity=self._identity(chat, identity),
            bridge_route=resolved.bridge_route,
            started_at=self.clock(),
        )
        await self.store.finish_context(
            running,
            message_id=chat.message_id,
            request_fingerprint=fingerprint,
            response=response,
            context=context,
            state_identity=self._identity(chat, identity),
            v1_execution_called=v1_execution_called,
        )
        return response

    async def aclose(self):
        await self.store.aclose()


def build_context_v1_execution_handler(
    settings: Settings,
    *,
    v1_workflow,
    external: ContextV1ExternalDependencies | None = None,
) -> V2ContextV1ExecutionBridge:
    receipt = validate_context_v1_settings(settings)
    dependencies = external or _build_context_dependencies(settings)
    expected_scope = receipt["scope"]

    pin = dependencies.publication.pin(
        expected_scope["semantic_model_id"],
        expected_scope["business_domain_ids"],
    )
    try:
        catalog_identity = pin.identity
        expected_catalog_identity = (
            dependencies.catalog_runtime.current_identity
            if dependencies.catalog_runtime is not None
            else {
                "catalog_version": receipt["catalog_version"],
                "vector_index_version": receipt["vector_index_version"],
                "target_identity_hash": (
                    settings.limited_scalar_catalog_target_identity_hash
                ),
            }
        )
        if any(
            catalog_identity.get(field) != expected_catalog_identity.get(field)
            for field in (
                "catalog_version",
                "vector_index_version",
                "target_identity_hash",
            )
        ):
            raise RuntimeError("V2_CONTEXT_V1_CATALOG_PIN_MISMATCH")
    finally:
        pin.finish()
    model_wide_domains = list(expected_scope["business_domain_ids"])

    def require_scope(chat: ChatRequest) -> dict[str, Any]:
        return _resolve_context_v1_request_scope(chat, expected_scope)

    def context_resolver(chat: ChatRequest, trusted: TrustedIdentity):
        resolved_scope = require_scope(chat)
        session = ScopedPlanSession(
            chat,
            trusted,
            dependencies.publication,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        context = session.context
        active_identity = (
            dependencies.catalog_runtime.current_identity
            if dependencies.catalog_runtime is not None
            else catalog_identity
        )
        if (
            context.catalog_pin.catalog_version
                != active_identity["catalog_version"]
            or context.catalog_pin.vector_index_version
                != active_identity["vector_index_version"]
            or context.catalog_pin.target_identity_hash
                != active_identity["target_identity_hash"]
        ):
            raise ValueError("V2_CONTEXT_V1_SCOPE_PIN_MISMATCH")
        session.accept_catalog()
        return context

    engine = RawTurnPlanner(
        dependencies.model,
        dependencies.publication,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
    )

    async def context_planner(chat, trusted, state, plans, pending):
        resolved_scope = require_scope(chat)
        previous = ConversationState.model_validate(state.payload) if state else None
        result = await engine.run(
            chat,
            trusted,
            state=state,
            plans=plans,
            pending=pending,
            allow_standalone_new_task_passthrough=True,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        if isinstance(result, RecognizedStandaloneNewTask):
            logger.info(
                "V2 context handed standalone new task to V1 execution",
                extra={
                    "message_id": chat.message_id,
                    "bridge_route": result.execution_route,
                    "v2_best_effort_failure": result.fallback_reason,
                },
            )
            return ResolvedContextTurn(
                completed_question=result.completed_question,
                next_state=result.next_state,
                plan_state=None,
                bridge_route=result.execution_route,
                anchor_update=ExecutionAnchorUpdate(
                    previous=None,
                    current_parse=result.parse,
                    time_context=None,
                    resolved_business_domain_ids=tuple(
                        resolved_scope["resolved_business_domain_ids"]
                    ),
                ),
            )
        if isinstance(result, RecognizedClarification):
            return ResolvedContextTurn(
                completed_question=None,
                next_state=result.next_state,
                plan_state=None,
                pending_state=result.pending_state,
                clarification_question=result.question,
                clarification_trace=result.trace,
                bridge_route="V2_USER_AMBIGUITY",
            )
        session = ScopedPlanSession(
            chat,
            trusted,
            dependencies.publication,
            resolved_business_domain_ids=resolved_scope[
                "resolved_business_domain_ids"
            ],
        )
        session.restore(result.next_state, kind="CONVERSATION")
        plan = AuthorizedLogicalPlan.model_validate(
            session.restore(result.plan_state, kind="LAST_REQUEST")
        )
        session.accept_catalog()
        next_state = ConversationState.model_validate(result.next_state.payload)
        display = build_completed_question_display(
            message_id=chat.message_id,
            plan=plan,
            previous_state=previous,
            next_state=next_state,
            context_trace=result.context_trace,
        )
        display = _standalone_execution_display(chat.question, display)
        return ResolvedContextTurn(
            completed_question=display.completed_question,
            next_state=result.next_state,
            plan_state=result.plan_state,
            display=display,
        )

    async def v1_executor(chat: ChatRequest, trusted: TrustedIdentity):
        outcome = await v1_workflow.ainvoke({"chat": chat, "identity": trusted})
        return AgentResponse.model_validate(outcome["response"])

    async def readiness_probe():
        try:
            redis_ready = bool(await dependencies.redis.ping())
        except Exception:
            redis_ready = False

        def verify_catalog():
            expected_identity = (
                dependencies.catalog_runtime.current_identity
                if dependencies.catalog_runtime is not None
                else catalog_identity
            )
            current = dependencies.publication.pin(
                expected_scope["semantic_model_id"],
                expected_scope["business_domain_ids"],
            )
            try:
                current_identity = current.identity
                return all(
                    current_identity.get(field) == expected_identity.get(field)
                    for field in (
                        "catalog_version",
                        "vector_index_version",
                        "target_identity_hash",
                    )
                )
            finally:
                current.finish()

        try:
            if dependencies.catalog_runtime is not None:
                await dependencies.catalog_runtime.ensure_current()
            catalog_ready = bool(await asyncio.to_thread(verify_catalog))
        except Exception:
            catalog_ready = False
        return {
            "v2_context_v1_redis": redis_ready,
            "v2_context_v1_catalog_pin": catalog_ready,
            "v2_context_v1_catalog_auto_refresh": bool(
                dependencies.catalog_runtime is not None
            ) if settings.demo_mode else True,
            "v1_execution_bridge": True,
            "v2_execution_transport_disabled": True,
        }

    namespace = settings.limited_scalar_store_namespace.replace(
        ":v2-limited-scalar:", ":v2-context-v1-execution:", 1
    )
    store = RedisScalarSessionStore(
        dependencies.redis,
        prefix=namespace,
        deployment_id=settings.limited_scalar_deployment_id,
        ttl_seconds=settings.limited_scalar_session_ttl_seconds,
        idempotency_ttl_seconds=settings.limited_scalar_idempotency_ttl_seconds,
        max_messages=settings.limited_scalar_max_messages_per_session,
        max_envelope_bytes=settings.limited_scalar_max_envelope_bytes,
        production_prefix=settings.session_key_prefix,
    )
    startup_receipt = {
        **receipt,
        "demo_mode": settings.demo_mode,
        "catalog_runtime_policy": (
            "AUTO_REFRESH"
            if dependencies.catalog_runtime is not None
            else "STRICT_PIN"
        ),
        "active_catalog_version": catalog_identity["catalog_version"],
        "active_vector_index_version": catalog_identity["vector_index_version"],
        "catalog_pin_verified": True,
        "v1_execution_bridge_enabled": True,
        "v2_limited_scalar_used_for_execution": False,
        "store_schema": store.schema_version,
        "store_namespace_mode": store.namespace_mode,
        "runtime_module_origins_verified": external is None,
        "scope_contract": {
            "model_wide": {
                "selection_mode": "MODEL_WIDE",
                "requested_business_domain_ids": [],
                "resolved_business_domain_ids": model_wide_domains,
            },
            "explicit": {
                "selection_mode": "EXPLICIT_DOMAINS",
                "requested_business_domain_ids": list(
                    expected_scope["business_domain_ids"]
                ),
                "resolved_business_domain_ids": list(
                    expected_scope["business_domain_ids"]
                ),
            },
        },
    }
    return V2ContextV1ExecutionBridge(
        store=store,
        context_resolver=context_resolver,
        context_planner=context_planner,
        v1_executor=v1_executor,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
        startup_receipt=startup_receipt,
        readiness_probe=readiness_probe,
        demo_mode=settings.demo_mode,
        catalog_runtime=dependencies.catalog_runtime,
    )
