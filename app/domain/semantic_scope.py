"""Immutable authorization supplied by the business backend, never inferred."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveScopeId = Annotated[int, Field(strict=True, gt=0)]


class AuthorizedSemanticScope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    semantic_model_id: PositiveScopeId
    business_domain_ids: tuple[PositiveScopeId, ...] = ()
    scope_mode: Literal['MODEL_WIDE', 'EXPLICIT_DOMAINS']
    database_id: PositiveScopeId | None = None
    knowledge_base_names: tuple[str, ...] = ()
    source: Literal['TRUSTED_UPSTREAM_BACKEND'] = 'TRUSTED_UPSTREAM_BACKEND'

    @field_validator('business_domain_ids')
    @classmethod
    def domain_set(cls, values):
        return tuple(sorted(set(values)))

    @field_validator('knowledge_base_names')
    @classmethod
    def knowledge_set(cls, values):
        if any(not value.strip() or len(value.strip()) > 128 for value in values):
            raise ValueError('REQUEST_SCOPE_INVALID: invalid knowledge base name')
        return tuple(sorted({value.strip() for value in values}))

    @model_validator(mode='after')
    def coherent_mode(self):
        expected = 'EXPLICIT_DOMAINS' if self.business_domain_ids else 'MODEL_WIDE'
        if self.scope_mode != expected:
            raise ValueError('REQUEST_SCOPE_INVALID: domain set and mode disagree')
        return self

    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(mode='json'), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def contains_domain(self, domain_id: int | None) -> bool:
        return not self.business_domain_ids or type(domain_id) is int and domain_id in self.business_domain_ids
