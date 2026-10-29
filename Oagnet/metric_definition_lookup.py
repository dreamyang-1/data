"""Read metric explanations from vectors without generating ASL or SQL."""
from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import BaseModel, Field, StrictInt, field_validator

from scope_contract import normalize_domains, require_candidate_scope, scope_filter


class MetricDefinitionLookupRequest(BaseModel):
    semantic_model_id: Annotated[StrictInt, Field(gt=0)]
    business_domain_ids: list[Annotated[StrictInt, Field(gt=0)]] = Field(default_factory=list)
    query: str = Field(min_length=1, max_length=4000)
    metric_names: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=20)

    @field_validator('business_domain_ids')
    @classmethod
    def supported_domains(cls, values):
        return normalize_domains(business_domain_ids=values)


def _key(value):
    return re.sub(r"[\s,，.。;；:：()（）\[\]{}'\"“”‘’_\-]+", '', str(value)).casefold()


def _terms(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        value = decoded if isinstance(decoded, list) else re.split(r'[,，;/；|]', value)
    return value if isinstance(value, list) else []


def lookup_metric_definitions(request, store, embed_query):
    where = scope_filter(request.semantic_model_id, business_domain_ids=request.business_domain_ids, record_type='metric')
    # Exact records keep a low-ranked canonical name from disappearing behind
    # approximate hits. This is a read; no catalog refresh or business query.
    exact_records = list(store.find_exact(where)) if callable(getattr(store, 'find_exact', None)) else []
    results = []
    for name in dict.fromkeys(request.metric_names):
        candidates = [*exact_records, *store.search(embed_query(name), top_k=20, where=where)]
        matched = {}
        for row in candidates:
            meta = row.metadata or {}
            require_candidate_scope(meta, request.semantic_model_id, request.business_domain_ids)
            if meta.get('type') != 'metric':
                continue
            names = [meta.get('metric_name'), meta.get('metric_code'), *_terms(meta.get('synonyms'))]
            if _key(name) not in {_key(term) for term in names if term}:
                continue
            if not meta.get('metric_code') or not meta.get('metric_name'):
                continue
            definition = {key: meta.get(key) for key in (
                'metric_name', 'metric_code', 'business_definition', 'calculation_rule',
                'unit', 'format_rule', 'time_caliber', 'synonyms',
            )}
            identity = (meta.get('business_domain_id'), meta['metric_code'],
                        json.dumps(definition, ensure_ascii=False, sort_keys=True))
            matched[identity] = {'record_id': row.id, 'semantic_model_id': request.semantic_model_id,
                'business_domain_id': meta.get('business_domain_id'), **definition}
        if len(matched) == 1:
            results.append({'input': name, 'status': 'RESOLVED', 'definition': next(iter(matched.values()))})
        else:
            results.append({'input': name, 'status': 'AMBIGUOUS' if matched else 'NOT_FOUND'})
    return {'success': True, 'semantic_model_id': request.semantic_model_id,
            'business_domain_ids': request.business_domain_ids, 'results': results}
