"""Structured errors and payload validation for Intent-to-ASL contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ASLValidationError(ValueError):
    """A stable, machine-classifiable ASL contract failure."""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.field = field
        self.details = details or {}


class IntentASLContract(BaseModel):
    """Caller-owned execution shape; all names remain semantic labels."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^1\.0$")
    intent: str = Field(min_length=1, max_length=64)
    query_object: str | None = Field(default=None, max_length=128)
    metric_required: bool
    required_metrics: list[str] = Field(default_factory=list, max_length=20)
    required_metric_codes: list[str] = Field(default_factory=list, max_length=20)
    required_projections: list[str] = Field(default_factory=list, max_length=50)
    required_groupings: list[str] = Field(default_factory=list, max_length=50)
    projection_mode: Literal["DISTINCT", "ROWS"] | None = None
    relationship_anchor: str | None = Field(default=None, max_length=128)
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    negative_filters: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    forbidden_filters: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    semantic_entity_mentions: list[str] = Field(default_factory=list, max_length=50)
    sorting: dict[str, Any] | None = None
    time_dimension_required: bool = False
    time_policy: Literal["REQUIRED", "OPTIONAL", "FORBIDDEN"] = "OPTIONAL"
    canonical_time_range: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.metric_required and not (self.required_metric_codes or self.required_metrics):
            # Metric IDs can be empty before semantic binding only for callers
            # that do not claim an authoritative metric contract.
            raise ValueError("metric_required contract must include a metric label or code")
        if self.intent == "DETAIL_QUERY":
            if not self.query_object:
                raise ValueError("DETAIL_QUERY contract requires query_object")
            if not self.required_projections:
                raise ValueError("DETAIL_QUERY contract requires required_projections")
            if self.projection_mode is None:
                raise ValueError("DETAIL_QUERY contract requires projection_mode")
        if self.time_dimension_required and self.intent != "TREND_ANALYSIS":
            raise ValueError("time_dimension_required is only valid for TREND_ANALYSIS")
        if self.time_dimension_required and self.time_policy != "REQUIRED":
            raise ValueError("time_dimension_required requires REQUIRED time_policy")
        if self.time_policy == "REQUIRED":
            if not self.canonical_time_range or not all(
                self.canonical_time_range.get(key) for key in ("start", "end")
            ):
                raise ValueError("REQUIRED time_policy requires canonical_time_range")
        elif self.canonical_time_range is not None:
            raise ValueError("canonical_time_range is only valid for REQUIRED time_policy")
        if self.intent == "DETAIL_QUERY" and self.required_groupings:
            raise ValueError("DETAIL_QUERY cannot declare aggregate groupings")
        if self.sorting is not None:
            required = self.sorting.get("required")
            direction = str(self.sorting.get("direction") or "").upper()
            if required is not True or direction not in {"ASC", "DESC"}:
                raise ValueError("sorting contract is invalid")
        if any(not value.strip() or len(value.strip()) > 100 for value in self.semantic_entity_mentions):
            raise ValueError("semantic_entity_mentions contains an invalid literal")
        if any(
            not str(item.get("field") or "").strip()
            or item.get("value") in (None, "")
            for item in self.forbidden_filters
        ):
            raise ValueError("forbidden_filters contains an invalid filter")
        return self
