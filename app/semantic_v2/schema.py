"""JSON Schema exporters for semantic V2 Pydantic models."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)


def draft_2020_12_schema(model: type[ModelT]) -> dict[str, Any]:
    """Generate a Draft 2020-12 schema from the sole Pydantic source."""

    schema = model.model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
    }
