"""ASL planning from business wording without caller-invented bindings.

This internal client only plans. Execution entry points must separately provide
user-confirmed constraints before replacing the legacy query route.
"""
from copy import deepcopy
from datetime import timedelta
import json
import re

from app.adapters.base import AdapterError
from app.services.asl_surface_handoff import build_surface_asl_input
from app.observability.call_timing import track_operation


async def generate_surface_asl(
    client, settings, *, completed_question, mentions, authorized_scope,
    identity, application_id, request_id, time_range=None,
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
    try:
        asl = json.loads(generated["result"]) if isinstance(generated.get("result"), str) else generated["result"]
    except (KeyError, ValueError, TypeError) as exc:
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result is not valid JSON") from exc
    if not isinstance(asl, dict):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL result must be an object")
    ambiguities = asl.get("ambiguity", [])
    if not isinstance(ambiguities, list):
        raise AdapterError("ASL_RESPONSE_INVALID", "ASL ambiguity must be a list")
    if time_range is not None and ambiguities:
        remaining = [
            item for item in ambiguities
            if not (
                isinstance(item, dict)
                and (
                    str(item.get("type") or "") in {"time", "time_anchor"}
                    or re.search(
                        r"时间|日期|time|date",
                        str(item.get("question") or ""),
                        re.IGNORECASE,
                    )
                )
            )
        ]
        anchors = {
            str(item.get("time_anchor") or "").strip()
            for item in generated["semantic_evidence"].get("selected_metrics", [])
            if isinstance(item, dict) and item.get("time_anchor")
        }
        if not remaining and len(anchors) == 1:
            # The completed question remains the ASL model's primary input.
            # A governed business default such as "正在销售=最近一年" is an
            # execution boundary already resolved by the caller; bind only
            # its dates to the selected metric's published time anchor.
            asl["time_context"] = {
                "type": "range",
                "start": time_range.start.isoformat(),
                "end": (time_range.end_exclusive - timedelta(days=1)).isoformat(),
                "value": None,
                "unit": "day",
                "anchor": next(iter(anchors)),
            }
            asl["ambiguity"] = []
            ambiguities = []
    if ambiguities:
        raise AdapterError("ASL_AMBIGUOUS", "ASL requires clarification", details=ambiguities)
    return {"asl": deepcopy(asl), "semantic_evidence": deepcopy(generated["semantic_evidence"])}
