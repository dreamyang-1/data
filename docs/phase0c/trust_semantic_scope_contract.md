# Phase 0C Trust & Semantic Scope Contract

The business backend is the authentication and authorization authority. DataAnalysis enforces the scope on each authenticated backend request; it does not query user permissions, infer role grants, or let a model or history choose authorization.

`semantic_model_id` is required, strictly an integer greater than zero (strings, floats, booleans and null are invalid). Invalid or missing request scope returns HTTP 422 with `detail.code=REQUEST_SCOPE_INVALID`, before workflow execution. The spreadsheet-import endpoint has the same requirement.

The deprecated `business_domain_id` input normalizes to `business_domain_ids`. When both are explicitly provided, their normalized sets must agree, including the conflict between a non-null scalar and an explicit empty list. Duplicates are removed and IDs sorted. Omitted or empty domains (including the existing null-list compatibility input) mean MODEL_WIDE for the supplied model only. Nonempty domains mean EXPLICIT_DOMAINS. Internal state uses these two mode names; response `AUTO`/`EXPLICIT` fields remain legacy presentation aliases.

`AuthorizedSemanticScope` is immutable and contains model ID, a tuple of domain IDs, mode, optional database ID, knowledge-base names and source `TRUSTED_UPSTREAM_BACKEND`. It is constructed from the validated current ChatRequest. Canonical intent fields are rebound to that current scope after interpretation and merging. Scope supplied by a model or restored state cannot grant access.

## Trust and state namespace

All agent API routes verify the existing service-to-service `Authorization: Bearer <authKey>` convention against `DATA_AGENT_TRUSTED_BACKEND_TOKEN`, using constant-time comparison. Missing configuration returns 503 `UPSTREAM_SCOPE_TRUST_UNCONFIGURED`; missing/invalid credentials return 401 `UPSTREAM_SCOPE_TRUST_INVALID`. Health endpoints remain separate.

After service authentication, `X-Tenant-Id` and `X-User-Id` are mandatory state namespace identifiers. Missing values return `STATE_NAMESPACE_REQUIRED`, even if the old development fallback flag is enabled. They do not determine data authorization. `X-Application-Id`, when supplied, must agree with body `application_id`; it is required in production or when the existing require-header setting is enabled. Roles never add semantic domains.

The subsequent 2026-09-08 business contract guarantees globally unique `conversation_id` and treats tenant/user as compatibility metadata. The existing mandatory-header implementation above is a known integration constraint, not a new data-authorization requirement. Adapting that boundary must preserve conversation isolation and disable personal cross-conversation memory without a stable user principal; no identity-system reconstruction is required.

The backend already sends a Bearer authKey in its Java calling utility. Deployment must provision the matching service secret and forward stable tenant/user identifiers. No real secret was added to Git or to a local .env, and no gateway/identity system was rewritten. A caller with the service secret is trusted to have completed authorization; the secret must remain server-side.

## State compatibility

Pending, task frames and last requests compare model, normalized domains, database and knowledge bases exactly. Compatible legacy flat state may retain business context only when all these fields match; it never supplies a missing request model or creates authorization. Mismatches cannot re-enter through the last-request fallback or historical task selection. DAG branch promotion checks the same contract.

DAG pending fingerprints and completed checkpoints include current scope. Response/repeat cache fingerprints carry `phase0c-v1` and scope fingerprint, invalidating old cache entries. Old message IDs may consequently produce an idempotency conflict; send a new message ID rather than reusing an old cached answer.

Datasets and report artifacts include the complete scope fingerprint in their namespace/provenance. Automatic selection, explicit dataset IDs, imports, derived datasets, joins, DAG presentation and report export cannot bypass it. Old datasets lacking that fingerprint fail closed. ASL and knowledge caches and validated semantic recall include scope, including database/knowledge-base differences. Semantic filter bindings outside the current grant are discarded.

## External capability and fail-closed behavior

The cross-service root-cause patch removes Oagnet's implicit shared domain `-1`, constrains evidence and physical metadata repair, and adds candidate provenance. The subsequent single-domain stage adds request-local scoped SQL catalog/planning and execution-time ASL/SQL/source verification. DataAnalysis now permits a single domain only when metadata, translation and execution responses confirm `single-domain-v1`, the exact model/domain and the current authorization fingerprint. Multiple domains still return `EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED`; an old or incompatible service returns a safe system failure, without asking the user to restate a metric.

MODEL_WIDE queries remain within the required model and may use a narrower query restriction inside that grant; this never changes authorization. Oagnet responses must confirm the queried model/domain restriction and their semantic evidence cannot exceed the grant.

Entity-value retrieval remains scoped. DataAnalysis's existing explicit-domain display-discovery opt-out is unchanged. Metric resolution, definition and lineage now carry the current scope through their SQL catalog requests. Dimensions are model-owned and derive domain eligibility from their governed entity bindings, not an invented business-domain column. Only owned bindings can enter scoped planning. SQL Translator retains the model-qualified Redis boundary and uses separate single-domain caches/indexes. See [single-domain closure](../phase0c_single_domain/closure_report.md); prior closure reports remain historical evidence.

The single-domain source and offline integration gate passes; deployed gateway/service versions and the conversation namespace compatibility boundary still require closure before overall Phase 0C acceptance. V2 routing and Phase 2.5.1 contracts are unchanged. A scoped raw-SQL request without its ASL, an unsupported plan shape, or a catalog binding that cannot prove ownership remains fail closed.

## Request example

The backend sends the following body with its existing service Bearer token and trusted namespace headers. The token is intentionally absent from this example.

```json
{"application_id":"business-app","conversation_id":"conversation-123","message_id":"turn-1","semantic_model_id":81,"business_domain_ids":[],"question":"查询本月销售额"}
```

`conversation_id` identifies one conversation within tenant/user/application state. It is not a user identity, a model ID or a data permission.
