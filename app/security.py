"""Verify the backend credential before resolving trusted conversation state."""
import hmac

from fastapi import Header, HTTPException, Request

from app.domain.models import TrustedIdentity
from app.domain.state_identity import conversation_namespace, has_stable_user_principal


async def trusted_backend(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_application_id: str | None = Header(default=None),
    x_roles: str | None = Header(default=None),
) -> None:
    settings = request.app.state.container.settings
    credential = settings.trusted_backend_token
    if credential is None or not credential.get_secret_value():
        raise HTTPException(503, detail={'code': 'UPSTREAM_SCOPE_TRUST_UNCONFIGURED'})
    scheme, _, supplied = (authorization or '').partition(' ')
    if scheme.lower() != 'bearer' or not hmac.compare_digest(supplied.encode(), credential.get_secret_value().encode()):
        raise HTTPException(401, detail={'code': 'UPSTREAM_SCOPE_TRUST_INVALID'})
    if any(value is not None and (not value.strip() or len(value.strip()) > 128)
           for value in (x_tenant_id, x_user_id)):
        raise HTTPException(401, detail={'code': 'STATE_NAMESPACE_REQUIRED'})
    if (settings.env == 'production' or settings.require_trusted_application_header) and not x_application_id:
        raise HTTPException(401, detail={'code': 'STATE_APPLICATION_REQUIRED'})
    tenant_id = x_tenant_id.strip() if x_tenant_id is not None else None
    user_id = x_user_id.strip() if x_user_id is not None else None
    request.state.trusted_roles = [r.strip() for r in (x_roles or '').split(',') if r.strip()]
    request.state.trusted_identity = (
        TrustedIdentity(tenant_id=tenant_id, user_id=user_id, roles=request.state.trusted_roles)
        if has_stable_user_principal(tenant_id, user_id) else None
    )


def resolve_conversation_identity(request: Request, application_id: str, conversation_id: str) -> TrustedIdentity:
    """Keep complete legacy principals; bind all other callers to this conversation.

    Access request.state directly so this cannot be used before trusted_backend.
    Never look up a user, accept identity from history, or infer semantic scope.
    """
    require_application_namespace(request, application_id)
    if request.state.trusted_identity is not None:
        return request.state.trusted_identity
    tenant_id, user_id = conversation_namespace(application_id, conversation_id)
    return TrustedIdentity(tenant_id=tenant_id, user_id=user_id, roles=request.state.trusted_roles)


def require_application_namespace(request: Request, application_id: str) -> None:
    supplied = request.headers.get('x-application-id')
    if supplied is not None and supplied != application_id:
        raise HTTPException(403, detail={'code': 'STATE_NAMESPACE_MISMATCH'})
