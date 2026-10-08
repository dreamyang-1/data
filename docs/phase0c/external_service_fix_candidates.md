# External contracts requiring owner action

No files in Oagnet, sql-translator or the business backend were modified. Local source evidence is recorded with SHA-256 in `source_contract_audit.json`; no live model request or production write was used.

## EXT-SCOPE-01 — Oagnet strict explicit domains (blocking)

- Source: `E:/YouoAgent/Oagnet/prompt_build.py`, `PromptBuilder._build_where`, lines 446–475. `api.py` QueryRequest/QueryResponse accept domain lists and `agent.py` forwards them, but the actual filter is `$in: [*business_domain_ids, -1]`.
- Expected: every semantic candidate is in the supplied model AND the exact explicit domain set. Shared domain `-1` is not authorized by an explicit positive-ID set.
- Actual: the main retrieval also allows `-1`. Checking only the HTTP schema would incorrectly conclude strict multi-domain support.
- DataAnalysis disposition: reject all current-backend explicit-domain Oagnet query/discovery paths before retrieval. Single-domain error `EXPLICIT_DOMAIN_NOT_SUPPORTED`; multi-domain error `EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED`. MODEL_WIDE remains usable.
- Recommended external fix: remove the implicit extra domain from explicit retrieval and audit all fallback/entity/metric/attribute/SQL-verified lookup paths for the same scope rule. Preserve shared-domain access under MODEL_WIDE only. Add single/set/negative and cross-model tests, plus candidate-domain provenance. After that evidence exists, update DataAnalysis's explicit-capability guard in a separately reviewed change. Do not simply toggle a configuration flag against the current server.

## EXT-SCOPE-02 — Oagnet display provenance (blocking that optional route)

- Source: `E:/YouoAgent/Oagnet/api.py`, display resolver filter around lines 402–424 and SemanticDisplayMatch schema.
- Actual: explicit domain filters add `-1`; matches omit domain provenance.
- Expected: exact-set filtering and model/domain provenance for every candidate, or a documented model-wide-only endpoint.
- DataAnalysis disposition: do not call this endpoint for explicit domains. This hides unverified display labels; it does not silently grant broader access or invent fields.

## EXT-SCOPE-03 — sql-translator metadata resolution

- Source: `E:/YouoAgent/sql-translator/api_server_prod.py`, `_handle_metric_resolve`, lines 220–233.
- Actual: the resolver accepts only model and metric names. It cannot constrain metric definition/lineage discovery by domain.
- Expected: an enforced domain-set contract and matching candidate provenance before returning metadata.
- DataAnalysis disposition: `EXPLICIT_DOMAIN_METADATA_NOT_SUPPORTED` before any model-wide metadata request. No local post-filtering is presented as equivalent to scoped retrieval.

## INTEGRATION-SCOPE-04 — backend service credential and namespace

- Source: `ProjectAgentFluxUtil.java` in `youoagent-api`, existing `Authorization: Bearer authKey` call sites.
- Reuse the existing service secret in `DATA_AGENT_TRUSTED_BACKEND_TOKEN`. Forward stable tenant/user headers (and matching application header in production). Do not expose this token to an untrusted browser.
- Runtime integration was not changed or deployed. Calls without this configuration are deliberately rejected. The business backend remains the sole authentication/authorization authority.

## Security reclassification of the Phase 0B finding

The earlier fixed `default-tenant/default-user` behavior is a STATE_ISOLATION_P0 because different users can share state keys. It is not evidence that DataAnalysis should implement data authorization. Phase 0C removes those endpoint defaults and requires namespace values from an authenticated backend. Arbitrary direct scope input without verified service trust would be UPSTREAM_SCOPE_TRUST_P0; the new token guard rejects that path. Existing frozen Phase 0B reports remain historical evidence and were not rewritten.
