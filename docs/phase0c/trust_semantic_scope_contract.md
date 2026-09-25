# Phase 0C Trust & Semantic Scope Contract

The business backend is the authentication and authorization authority. DataAnalysis enforces the scope on each authenticated backend request; it does not query user permissions, infer role grants, or let a model or history choose authorization.

`semantic_model_id` is required, strictly an integer greater than zero (strings, floats, booleans and null are invalid). Invalid or missing request scope returns HTTP 422 with `detail.code=REQUEST_SCOPE_INVALID`, before workflow execution. The spreadsheet-import endpoint has the same requirement.

The deprecated `business_domain_id` input normalizes to `business_domain_ids`. When both are explicitly provided, their normalized sets must agree, including the conflict between a non-null scalar and an explicit empty list. Duplicates are removed and IDs sorted. Omitted or empty domains (including the existing null-list compatibility input) mean MODEL_WIDE for the supplied model only. Nonempty domains mean EXPLICIT_DOMAINS. Internal state uses these two mode names; response `AUTO`/`EXPLICIT` fields remain legacy presentation aliases.

`AuthorizedSemanticScope` is immutable and contains model ID, a tuple of domain IDs, mode, optional database ID, knowledge-base names and source `TRUSTED_UPSTREAM_BACKEND`. It is constructed from the validated current ChatRequest. Canonical intent fields are rebound to that current scope after interpretation and merging. Scope supplied by a model or restored state cannot grant access.

## Trust and state namespace

All agent API routes verify the existing service-to-service `Authorization: Bearer <authKey>` convention against `DATA_AGENT_TRUSTED_BACKEND_TOKEN`, using constant-time comparison. Missing configuration returns 503 `UPSTREAM_SCOPE_TRUST_UNCONFIGURED`; missing/invalid credentials return 401 `UPSTREAM_SCOPE_TRUST_INVALID`. Health endpoints remain separate.

After service authentication, `X-Tenant-Id` and `X-User-Id` are optional compatibility metadata, not data authorization. Complete stable principals retain existing state keys. Absent/partial or default identities use an application/conversation-derived namespace; explicitly blank or overlong supplied values still return `STATE_NAMESPACE_REQUIRED`. `X-Application-Id`, when supplied, must agree with body `application_id`; it is required in production or when the existing require-header setting is enabled. Roles never add semantic domains.

The 2026-09-08 business contract guarantees globally unique `conversation_id`. Conversation namespaces are deterministic across retries, refreshes and file imports; both internal tenant and user components vary by conversation, also isolating tenant-scoped example recall. They never use history, models or semantic scope as identity sources. Without a stable principal, personal memory recall is skipped and `MemoryScope` rejects personal read/write construction. Changing between conversation-only and complete legacy principal metadata starts a separate state namespace; there is no automatic state migration or merging. The legacy allow-missing setting remains accepted configuration and cannot bypass service authentication.

The backend already sends a Bearer authKey in its Java calling utility. Deployment must provision the matching service secret and supply globally unique conversation IDs; stable tenant/user identifiers are needed only for personal cross-conversation memory. No real secret was added to Git or to a local .env, and no gateway/identity system was rewritten. A caller with the service secret is trusted to have completed authorization; the secret must remain server-side.

## State compatibility

Pending, task frames and last requests compare model, normalized domains, database and knowledge bases exactly. Compatible legacy flat state may retain business context only when all these fields match; it never supplies a missing request model or creates authorization. Mismatches cannot re-enter through the last-request fallback or historical task selection. DAG branch promotion checks the same contract.

An explicitly selected historical task remains the context source even when the most recent completed task has verified ASL. Named historical targets take precedence over a recency word; a following edit does not determine which old task is selected. A leading historical reference that is missing, tied or outside the current scope stops before query execution. A unique compatible historical task may leave an unrelated Pending only after its existing version check succeeds. Embedded references in result export and generic slot continuations retain their existing paths. See the [task recall closure](../phase0c_task_recall/closure_report.md).

A semantic candidate confirmation replaces only the metric or grouping member identified by the catalog's phrase. Other members and their order/metadata remain intact. A missing target is compatible only with an empty or singleton slot; ambiguous or mismatched targets preserve Pending and stop safely. When independent semantic choices remain, they advance through the existing clarification response before retrieval. See the [semantic choice closure](../phase0c_semantic_choice/closure_report.md).

Candidate labels and catalog details normalize as positional pairs. Exact duplicate pairs may collapse; conflicting or unpaired details fail closed without publishing a misleading option. Clarification repetition is identified by the displayed blocking question's target and catalog candidates, not by the union of option labels across different questions. Legacy repetition keys are interpreted against their stored Pending snapshot; traces retain hashes only. Nonblocking notes do not replace the visible choice. See the [candidate integrity closure](../phase0c_candidate_integrity/closure_report.md).

DAG pending fingerprints and completed checkpoints include current scope. Response/repeat cache fingerprints carry `phase0c-v1` and scope fingerprint, invalidating old cache entries. Old message IDs may consequently produce an idempotency conflict; send a new message ID rather than reusing an old cached answer.

Datasets and report artifacts include the complete scope fingerprint in their namespace/provenance. Automatic selection, explicit dataset IDs, imports, derived datasets, joins, DAG presentation and report export cannot bypass it. Old datasets lacking that fingerprint fail closed. ASL and knowledge caches and validated semantic recall include scope, including database/knowledge-base differences. Semantic filter bindings outside the current grant are discarded.

Loading every row of a persisted display slice does not prove coverage of the original result. Global ranking/aggregation requires complete source evidence; automatic recovery may undo only proven presentation slices within the same scope, snapshot and transformation lineage. It must not undo materialized filters, projections or joins. Explicitly selected datasets remain fixed. Joins preserve input incompleteness and use the existing LIMITED/warnings response fields when coverage is partial. See the [dataset lineage closure](../phase0c_dataset_lineage/closure_report.md); public schemas and data-only SSE framing remain unchanged.

## External capability and fail-closed behavior

Complete lists of existing known regions preserve one same-field IN predicate
without implying comparison grouping. Replacement/CLEAR also removes
superseded region entity evidence, while keeping literals owned by retained
filters. Typed entity names, negatives, mixed geographic fields and incomplete
unknown lists are outside this deterministic positive-list grammar. These are
business filters inside the current AuthorizedSemanticScope; they cannot
expand business-domain authorization. See the
[region list closure](../phase0c_region_list/closure_report.md).

Complete product-condition clears such as `不限产品` and `去掉产品条件`
remove product-name predicates and their entity/binding evidence, while keeping
other roles, metrics, period, grouping and projection. The existing restore
boundaries enforce an internal clear marker; an admitted explicit product can
reopen the condition. Clearing a business filter never changes the current
AuthorizedSemanticScope. See the [product clear closure](../phase0c_product_clear/closure_report.md).

Complete metric-only ADD/REMOVE commands cannot contribute operation fragments
as entity constraints, including schema-valid model role mistakes. The guard
uses whole-command recognition and keeps typed filter literals and admitted
inherited entities. It does not infer catalog roles for unknown targets or
compound requests. See the [metric edit grounding closure](../phase0c_metric_edit_grounding/closure_report.md).

Explicit dimension-only additions, removals and clears preserve their operation
through admission, scoped semantic label rebinding and verified-frame merging.
An empty CLEAR cannot become inheritance; ADD/REMOVE apply to the admitted
before-frame, and a changed grouping invalidates the old ASL/result reference.
Later edits remain subject to the current request scope. See the
[dimension mutation closure](../phase0c_dimension_mutation/closure_report.md)
for complete-command boundaries, contrasts and unchanged public I/O.

The Legacy subject guard recognizes a complete supported time expression beside
one existing known region before guessing a product filter or entity mention.
It preserves explicit named literals and leaves their physical binding to the
governed catalog. This does not change AuthorizedSemanticScope or public I/O.
See the [compact structural-scope closure](../phase0c_structural_scope/closure_report.md)
for the bounded grammar, contrast evidence and remaining unknowns.

The cross-service root-cause patch removes Oagnet's implicit shared domain `-1`, constrains evidence and physical metadata repair, and adds candidate provenance. The subsequent single-domain stage adds request-local scoped SQL catalog/planning and execution-time ASL/SQL/source verification. DataAnalysis now permits a single domain only when metadata, translation and execution responses confirm `single-domain-v1`, the exact model/domain and the current authorization fingerprint. Multiple domains still return `EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED`; an old or incompatible service returns a safe system failure, without asking the user to restate a metric.

MODEL_WIDE queries remain within the required model and may use a narrower query restriction inside that grant; this never changes authorization. Oagnet responses must confirm the queried model/domain restriction and their semantic evidence cannot exceed the grant.

Entity-value retrieval remains scoped. DataAnalysis's existing explicit-domain display-discovery opt-out is unchanged. Metric resolution, definition and lineage now carry the current scope through their SQL catalog requests. Dimensions are model-owned and derive domain eligibility from their governed entity bindings, not an invented business-domain column. Only owned bindings can enter scoped planning. SQL Translator retains the model-qualified Redis boundary and uses separate single-domain caches/indexes. See [single-domain closure](../phase0c_single_domain/closure_report.md); prior closure reports remain historical evidence.

The single-domain source and offline integration gate passes. Conversation compatibility is covered by the [conversation trust closure](../phase0c_conversation_trust/closure_report.md); deployed gateway/service versions and catalog publication still require evidence before overall Phase 0C acceptance. V2 routing and Phase 2.5.1 contracts are unchanged. A scoped raw-SQL request without its ASL, an unsupported plan shape, or a catalog binding that cannot prove ownership remains fail closed.

## Request example

The backend sends the following unchanged body with its existing service Bearer token and, in production, the matching application header. The token is intentionally absent from this example.

```json
{"application_id":"business-app","conversation_id":"conversation-123","message_id":"turn-1","semantic_model_id":81,"business_domain_ids":[],"question":"查询本月销售额"}
```

`conversation_id` is the backend's globally unique conversation identifier. It is not a user identity, a model ID or a data permission. All existing public request/response schemas and data-only SSE framing are retained.
