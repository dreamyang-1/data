"""Sanitized Trace V2 contract, isolated from the current event system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from .models import Identifier, StrictModel


class TraceStage(StrEnum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    STATE_LOADED = "STATE_LOADED"
    CURRENT_TURN_PARSED = "CURRENT_TURN_PARSED"
    DIALOGUE_CANDIDATES_CREATED = "DIALOGUE_CANDIDATES_CREATED"
    DIALOGUE_RESOLVED = "DIALOGUE_RESOLVED"
    INTENT_AXES_PARSED = "INTENT_AXES_PARSED"
    MENTIONS_EXTRACTED = "MENTIONS_EXTRACTED"
    SEMANTIC_CANDIDATES_RETRIEVED = "SEMANTIC_CANDIDATES_RETRIEVED"
    SEMANTIC_CANDIDATES_FILTERED = "SEMANTIC_CANDIDATES_FILTERED"
    PLAN_CANDIDATES_CREATED = "PLAN_CANDIDATES_CREATED"
    PLAN_SELECTED = "PLAN_SELECTED"
    SLOT_PATCH_APPLIED = "SLOT_PATCH_APPLIED"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    CLARIFICATION_DECIDED = "CLARIFICATION_DECIDED"
    LEGACY_ADAPTER_EXECUTED = "LEGACY_ADAPTER_EXECUTED"
    ASL_CONTRACT_VALIDATED = "ASL_CONTRACT_VALIDATED"
    SQL_PLAN_VALIDATED = "SQL_PLAN_VALIDATED"
    QUERY_EXECUTED = "QUERY_EXECUTED"
    RESULT_CONTRACT_VALIDATED = "RESULT_CONTRACT_VALIDATED"
    STATE_COMMITTED = "STATE_COMMITTED"
    RESPONSE_FINALIZED = "RESPONSE_FINALIZED"
    ERROR = "ERROR"


_FORBIDDEN_KEYS = {
    "api_key", "token", "password", "database_password", "cookie",
    "authorization", "auth_url", "raw_rows",
}


class TraceV2(StrictModel):
    """One bounded, sanitized phase event."""

    schema_version: str = "2.0"
    trace_id: Identifier
    span_id: Identifier
    parent_span_id: Identifier | None = None
    conversation_id: Identifier
    message_id: Identifier
    topic_id: Identifier | None = None
    task_id: Identifier | None = None
    task_version: int | None = Field(default=None, ge=1)
    stage: TraceStage
    timestamp: datetime
    duration_ms: float = Field(ge=0)
    input_digest: str = Field(min_length=64, max_length=64)
    output_digest: str = Field(min_length=64, max_length=64)
    model_name: str | None = Field(default=None, max_length=200)
    model_snapshot: str | None = Field(default=None, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=200)
    catalog_version: str | None = Field(default=None, max_length=200)
    semantic_model_version: str | None = Field(default=None, max_length=200)
    policy_version: str | None = Field(default=None, max_length=200)
    candidate_summary: dict[str, Any] = Field(default_factory=dict)
    rejection_summary: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = Field(default=None, max_length=200)
    sanitized_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sanitized_payload")
    @classmethod
    def reject_sensitive_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject forbidden keys recursively instead of silently logging them."""

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if str(key).casefold() in _FORBIDDEN_KEYS:
                        raise ValueError(f"sensitive trace field is forbidden: {key}")
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
        return value
