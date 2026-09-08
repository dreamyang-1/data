"""Current-request scope binding and state compatibility checks."""
from app.domain.models import CanonicalAnalysisRequest, ChatRequest
from app.domain.semantic_scope import AuthorizedSemanticScope


def bind_authorized_scope(request: CanonicalAnalysisRequest, scope: AuthorizedSemanticScope) -> None:
    request.authorized_semantic_scope = scope
    request.semantic_model_id = scope.semantic_model_id
    request.business_domain_ids = list(scope.business_domain_ids)
    request.business_domain_selection_mode = scope.scope_mode
    request.database_id = scope.database_id
    request.knowledge_base_names = list(scope.knowledge_base_names)
    request.resolved_business_domain_ids = [d for d in request.resolved_business_domain_ids if scope.contains_domain(d)]
    request.semantic_filter_bindings = [b for b in request.semantic_filter_bindings if scope.contains_domain(b.business_domain_id)]


def state_scope_matches(request: CanonicalAnalysisRequest, chat: ChatRequest) -> bool:
    scope = chat.authorized_semantic_scope
    stored = request.authorized_semantic_scope
    # Legacy flat state may only be reused if every scope field is an exact
    # match. It never creates authorization or supplies a missing model ID.
    return (stored is None or stored == scope) and (
        request.semantic_model_id == scope.semantic_model_id
        and request.database_id == scope.database_id
        and tuple(sorted(request.business_domain_ids)) == scope.business_domain_ids
        and tuple(sorted(request.knowledge_base_names)) == scope.knowledge_base_names
    )
