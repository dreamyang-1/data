"""State namespace helpers; these values never determine data authorization."""
import hashlib
import json


CONVERSATION_NAMESPACE_PREFIX = 'conversation:'


def has_stable_user_principal(tenant_id: str | None, user_id: str | None) -> bool:
    """A complete upstream principal can use personal memory; placeholders cannot."""
    return bool(
        tenant_id and user_id
        and tenant_id != 'default-tenant' and user_id != 'default-user'
        and not tenant_id.startswith(CONVERSATION_NAMESPACE_PREFIX)
        and not user_id.startswith(CONVERSATION_NAMESPACE_PREFIX)
    )


def conversation_namespace(application_id: str, conversation_id: str) -> tuple[str, str]:
    """Versioned, delimiter-safe namespace for a globally unique trusted conversation.

    Both components vary by conversation: even tenant-scoped example caches must
    not become shared personal state for callers without an upstream principal.
    This namespace is stable across messages/refreshes and contains no raw IDs.
    """
    canonical = json.dumps([application_id, conversation_id], ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return CONVERSATION_NAMESPACE_PREFIX + digest[:48], CONVERSATION_NAMESPACE_PREFIX + digest
