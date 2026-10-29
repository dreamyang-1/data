"""Request-owned semantic scope. Scope is never inferred from retrieved data."""
from __future__ import annotations

CONTRACT_VERSION = '1.0'


def require_model_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError('REQUEST_SCOPE_INVALID: semantic_model_id must be a positive integer')
    return value


def normalize_domains(business_domain_id=None, business_domain_ids=None) -> list[int]:
    if business_domain_ids is not None and not isinstance(business_domain_ids, (list, tuple)):
        raise ValueError('REQUEST_SCOPE_INVALID: business_domain_ids must be an array')
    values = list(business_domain_ids or [])
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError('REQUEST_SCOPE_INVALID: business_domain_ids must contain positive integers')
    values = sorted(set(values))
    if business_domain_id is not None:
        if type(business_domain_id) is not int or business_domain_id <= 0:
            raise ValueError('REQUEST_SCOPE_INVALID: business_domain_id must be a positive integer')
        if business_domain_ids is not None and values != [business_domain_id]:
            raise ValueError('REQUEST_SCOPE_INVALID: business_domain_id conflicts with business_domain_ids')
        values = [business_domain_id]
    if len(values) > 1:
        raise ValueError('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED')
    return values


def scope_filter(semantic_model_id: int, business_domain_id=None, business_domain_ids=None,
                 *, record_type: str | None = None) -> dict:
    model = require_model_id(semantic_model_id)
    domains = normalize_domains(business_domain_id, business_domain_ids)
    clauses = [{'semantic_model_id': model}]
    if record_type is not None:
        clauses.insert(0, {'type': semantic_record_types(record_type, domains)})
    if domains:
        clauses.append({'business_domain_id': {'$in': domains}})
    return clauses[0] if len(clauses) == 1 else {'$and': clauses}


def semantic_record_types(record_type, domains):
    # Separate kinds keep projected copies from displacing model-wide vectors.
    if domains and record_type in {'dimension', 'enum'}:
        return {'$in': [record_type, 'scoped_' + record_type]}
    return record_type


def require_candidate_scope(metadata: dict, semantic_model_id: int, domains: list[int]) -> None:
    if not isinstance(metadata, dict):
        raise ValueError('SEMANTIC_SCOPE_MISMATCH: candidate metadata is missing')
    if type(metadata.get('semantic_model_id')) is not int or metadata['semantic_model_id'] != semantic_model_id:
        raise ValueError('SEMANTIC_SCOPE_MISMATCH: candidate model is unproven or incompatible')
    if domains and (type(metadata.get('business_domain_id')) is not int
                    or metadata['business_domain_id'] not in domains):
        raise ValueError('SEMANTIC_SCOPE_MISMATCH: candidate domain is unproven or incompatible')
