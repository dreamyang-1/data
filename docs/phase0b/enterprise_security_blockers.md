# SECURITY_P0 — trusted identity boundary

`app/api.py:trusted_identity` returns fixed `default-tenant` / `default-user`; the public interface does not establish a caller-specific trusted tenant/user boundary. `pytest-09` and `pytest-10` are two assertions associated with this one boundary issue, not two independently repaired bugs. Existing role/header handling does not establish tenant isolation for real callers.

Owner: identity/API platform owner. Severity: SECURITY_P0. Status: OWNER_BLOCKER, intentionally unmodified under user section 32. Required next work: authenticated identity derivation and tenant/user/application/role trust contract; cross-tenant, unauthorized-candidate and spoofing regressions before production exposure. Passing in-memory scoped-state tests verifies the lower-layer keys only.

This phase does not rewrite production identity, introduce V2 production routing, event store, long-term memory or CAS architecture. Highest readiness is Phase 0C Gold preparation, and closure here is conditional on explicit blockers.
