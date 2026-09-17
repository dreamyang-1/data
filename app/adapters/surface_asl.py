"""ASL planning from business wording without caller-invented bindings.

This internal client only plans. Execution entry points must separately provide
user-confirmed constraints before replacing the legacy query route.
"""
from copy import deepcopy
import json

from app.adapters.base import AdapterError
from app.services.asl_surface_handoff import build_surface_asl_input
from app.observability.call_timing import track_operation


async def generate_surface_asl(
    client, settings, *, completed_question, mentions, authorized_scope,
    identity, application_id, request_id,
):
    """Return validated Oagnet evidence without rewriting the generated ASL."""
    from app.domain.semantic_scope import AuthorizedSemanticScope

    if not isinstance(authorized_scope, AuthorizedSemanticScope):
        raise AdapterError("SEMANTIC_CONTEXT_MISSING", "trusted semantic scope is required")
    domains = list(authorized_scope.business_domain_ids)
    if len(domains) > 1:
        raise AdapterError("SEMANTIC_SCOPE_INVALID", "multiple explicit domains are unsupported")
    payload = build_surface_asl_input(completed_question, mentions)
    payload.update(
        semantic_model_id=authorized_scope.semantic_model_id,
        business_domain_id=domains[0] if domains else None,
        business_domain_ids=domains,
    )
    with track_operation('UPSTREAM', 'upstream.oagnet.asl_generation',
                         attributes={'input_mode': 'SURFACE_ADVISORY'}) as timing:
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
    )
    if generated.get("asl_validation") != "PASS" or generated.get("asl_contract") is not None:
        raise AdapterError("ASL_INTENT_CONTRACT_UNCONFIRMED", "ASL validation or advisory mode was not confirmed")
    try:
        asl = json.loads(generated["result"]) if isinstance(generated.get("result"), str) else generated["result"]
    except (KeyError, ValueError, TypeError) as exc:
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result is not valid JSON") from exc
    if not isinstance(asl, dict):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result must be an object")
    ambiguities = asl.get("ambiguity", [])
    if not isinstance(ambiguities, list):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL ambiguity must be a list")
    if ambiguities:
        raise AdapterError("ASL_AMBIGUOUS", "ASL requires clarification", details=ambiguities)
    return {"asl": deepcopy(asl), "semantic_evidence": deepcopy(generated["semantic_evidence"])}
