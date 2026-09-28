"""Bind planner parameters through Oagnet; preserve every clarification."""
from copy import deepcopy
import json

from app.adapters.base import AdapterError
from app.services.asl_surface_handoff import build_surface_asl_input
from app.observability.call_timing import track_operation


async def generate_surface_asl(
    client, settings, *, completed_question, mentions, authorized_scope,
    identity, application_id, request_id, time_range=None, confirmed_metrics=(),
    resolved_business_domain_ids=(), structured_reference=None,
    structured_extraction=None,
):
    """Return validated Oagnet evidence without rewriting the generated ASL."""
    from app.domain.semantic_scope import AuthorizedSemanticScope

    if not isinstance(authorized_scope, AuthorizedSemanticScope):
        raise AdapterError("SEMANTIC_CONTEXT_MISSING", "trusted semantic scope is required")
    domains = list(authorized_scope.business_domain_ids)
    if len(domains) > 1:
        raise AdapterError("SEMANTIC_SCOPE_INVALID", "multiple explicit domains are unsupported")
    resolved_domains = list(dict.fromkeys(resolved_business_domain_ids or ()))
    if any(not authorized_scope.contains_domain(value) for value in resolved_domains):
        raise AdapterError("SEMANTIC_SCOPE_INVALID", "resolved domain exceeds authorization")
    execution_domains = domains or (resolved_domains if len(resolved_domains) == 1 else [])
    payload = build_surface_asl_input(
        completed_question, mentions, structured_extraction=structured_extraction
    )
    payload.update(
        completed_question=completed_question,
        structured_reference=structured_reference,
        semantic_model_id=authorized_scope.semantic_model_id,
        business_domain_id=execution_domains[0] if execution_domains else None,
        business_domain_ids=execution_domains,
    )
    with track_operation('UPSTREAM', 'upstream.oagnet.asl_generation',
                         attributes={'input_mode': 'STRUCTURED_BINDING'}) as timing:
        generated = await client.post(
            settings.asl_generator_base_url, settings.asl_generator_path, payload,
            identity=identity, application_id=application_id,
            idempotency_key=f"{request_id}:surface-asl", retryable=True,
            timeout=settings.asl_generation_timeout_seconds,
        )
        timing.mark_first_result()
    if not isinstance(generated, dict) or generated.get("success") is not True:
        raise AdapterError("ASL_GENERATION_FAILED", "ASL generator rejected request")
    # Reuse the existing scope checks, including metric provenance. Do not make
    # authorization depend on model-extracted mentions or returned business text.
    from app.adapters.http import HttpDataRetrievalAdapter
    from types import SimpleNamespace
    HttpDataRetrievalAdapter._confirm_generated_scope(
        SimpleNamespace(authorized_semantic_scope=authorized_scope), generated,
        execution_domains[0] if execution_domains else None,
    )
    try:
        asl = json.loads(generated["result"]) if isinstance(generated.get("result"), str) else generated["result"]
    except (KeyError, ValueError, TypeError) as exc:
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result is not valid JSON") from exc
    if not isinstance(asl, dict):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result must be an object")
    ambiguities = asl.get("ambiguity", [])
    if not isinstance(ambiguities, list):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL ambiguity must be a list")
    # The ASL boundary owns completeness. Do not erase metric/time issues or
    # fabricate a successful plan from caller defaults after it requested input.
    if ambiguities:
        raise AdapterError("ASL_AMBIGUOUS", "ASL requires clarification", details=ambiguities)
    return {"asl": deepcopy(asl), "semantic_evidence": deepcopy(generated["semantic_evidence"]),
            "asl_repair": deepcopy(generated.get("asl_repair") or [])}
