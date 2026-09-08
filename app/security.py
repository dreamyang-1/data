"""Verify a backend service credential and its state namespace headers."""
import hmac

from fastapi import Header, HTTPException, Request

from app.domain.models import TrustedIdentity


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
    if any(not value or not value.strip() or len(value.strip()) > 128 for value in (x_tenant_id, x_user_id)):
        raise HTTPException(401, detail={'code': 'STATE_NAMESPACE_REQUIRED'})
    if (settings.env == 'production' or settings.require_trusted_application_header) and not x_application_id:
        raise HTTPException(401, detail={'code': 'STATE_APPLICATION_REQUIRED'})
    request.state.trusted_identity = TrustedIdentity(
        tenant_id=x_tenant_id.strip(), user_id=x_user_id.strip(),
        roles=[r.strip() for r in (x_roles or '').split(',') if r.strip()],
    )


def require_application_namespace(request: Request, application_id: str) -> None:
    supplied = request.headers.get('x-application-id')
    if supplied is not None and supplied != application_id:
        raise HTTPException(403, detail={'code': 'STATE_NAMESPACE_MISMATCH'})
