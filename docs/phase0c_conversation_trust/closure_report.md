# Conversation trust and personal-memory boundary closure

**Stage gate: PASS (source and offline acceptance). V1 replacement: NOT_READY.**

This stage accepts authenticated backend calls that provide the existing globally
unique `conversation_id` without requiring tenant/user metadata. It closes the
associated shared-personal-memory path. It preserves the current public input,
output and data-only SSE formats. It does not replace V1, deploy services, publish
catalog indexes or claim overall Phase 0C acceptance.

## Baseline, choice of stage and audit

- Baseline: `5acfa99140f5dfede8f7c10ab3e0fd09db9c59f3`.
- Branch: `phase0c-conversation-trust-20260908-01`.
- Draft PR base: `phase0c-oagnet-scope-closure-20260908-01` (PR #7).
- All 757 starting tracked files matched development sources after the documented
  CRLF/LF normalization, and their committed Git blobs matched the checkout.
  The version working tree was clean. The separate Oagnet index was preserved.
- Read the preceding Oagnet closure, current scope contract, failure triage,
  AGENTS and runtime graph. Inventory covers all 99 Agent `app` Python files and
  1,236 functions. Manual review follows API trust, streaming pre-cache checks,
  refresh, spreadsheet registration, canonical identity, session keys, Pending,
  last request/task frame, DAG, dataset ownership and personal memory.
  `audit_inventory.json` records source hashes and the public schema comparison.
  AST coverage is an inventory, not a claim of behavioral proof for every legacy
  function or of newly reviewing all external-service heuristics.
- The previous stage's catalog publication blocker remains real. It prevents
  automatic entry into Phase 0C-2 Gold/model benchmarking under the prior gate.
  The current request authorizes choosing a useful independent next stage. The
  already documented conversation compatibility gap is concrete, reproducible,
  within Agent ownership and needs neither a new business decision nor a
  production write. This is why it was selected before further V2 infrastructure.

## Findings and implementation

| Finding | Evidence / first divergence | Disposition |
| --- | --- | --- |
| Trusted conversation rejected without tenant/user | PROVEN: `security.trusted_backend` returned 401 before any workflow. Current backend contract explicitly makes these headers optional compatibility metadata. | Fixed at API trust admission. The Bearer guard remains mandatory; absent metadata resolves from the current application/conversation only. |
| Default-user personal memory shared across conversations | PROVEN: with `use_longterm_memory`, default principal IDs reached `memories.list_active` using a scope without conversation ID; `MemoryScope` also accepted that shared namespace for writes. This is STATE_ISOLATION_P0, not a data-authorization finding. | Fixed: personal recall is skipped without a stable principal. Personal read/write scope construction rejects placeholders and the reserved synthetic namespace. Existing stable-principal memory behavior remains. |
| Spreadsheet/chat whitespace handling could split one namespace | PROVEN: `_scope_chat` normalized identifiers but import retained the original values when constructing DatasetScope. | Reuse the existing ChatRequest normalization for the import identifiers. No request schema change. |

Complete stable tenant/user pairs keep their existing session/cache/dataset keys;
existing different-user isolation with the same conversation string still passes.
Missing, partial, default or reserved synthetic identity metadata resolves to a
SHA-256-derived tuple from the current application and conversation. Both tenant
and user components vary by conversation, so tenant-scoped validated examples do
not become a shared pool for callers without a principal. The internal
`conversation:` prefix is reserved and never grants personal-memory access.

The same resolver runs before workflow invocation, streaming cache lookup and
spreadsheet import. Message ID and authorized scope do not choose the namespace;
message fingerprints and every existing scope-bound restore check still apply.
Refresh stays within the existing conversation and uses the existing refresh
message-ID logic. Synthetic IDs do not contain raw conversation text.

Malformed supplied metadata retains the existing error code. Production still
requires the existing application header and matching body application. The
legacy allow-missing setting remains parseable but cannot disable authentication.
Roles and identities never determine semantic data authorization.

Changing between conversation-only and complete legacy principal metadata starts
a separate namespace. There is no automatic migration/alias of old shared-default
state, cache, dataset or memory records; existing expiry/retention still applies.
The backend should keep its chosen metadata convention consistent within a
conversation. No records or indexes were deleted or rewritten.

## Existing input/output format

The baseline and final OpenAPI documents compare equal, as do JSON schemas for
ChatRequest, AgentResponse, SpreadsheetImportRequest and SpreadsheetImportResponse.
The SHA-256 receipts are in `audit_inventory.json` and `test_delta.json`.
No public fields, endpoint paths, required body fields, response fields or SSE
event shapes were added. Compatibility `AUTO`/`EXPLICIT` presentation fields are
unchanged. Stream tests parse the existing `data: {JSON}\n\n` records and assert
the existing `complete` terminal status; no `event:` records are introduced.

The deliberate behavior change is acceptance of a previously rejected,
authenticated conversation-only call. The personal-memory rejection code is an
internal MemoryScope validation error, not a new public response format.

## Verification and test expectation decisions

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 1,876 passed / 27 failed | **1,919 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multiturn/scope/single-domain suite | 160 passed | **160 passed** |
| New conversation trust contract | Initial 28 cases: 22 failed / 6 passed | **43 / 43 passed** |

All existing node IDs are present. Old pass -> new fail = **0**; old fail -> new
pass = **0**; collection errors = **0**. All 27 Agent and 10 Oagnet failure
signatures remain unchanged; their exact node IDs are retained in `test_delta.json`.
Before implementation, the affected old modules were additionally rerun:
155 passed / 1 already-known memory-event failure. The first new fixture attempt
was corrected to use the actual container layout and offline production guard
seam; the subsequent reproduction produced 22 failures from the contract gaps.
During implementation, SSE assertions were corrected to the existing protocol,
not by modifying the product format.

Full Agent testing uses the 14 established offline module batches plus the new
module, with complete `tests/test_*.py` coverage verified by the runner. The
existing clock seam fixes the classifier date. Oagnet and SQL use their guarded
offline full-suite runners. The new acceptance includes both application-header
guard modes, authentication negatives, stable retries, streaming cache conflicts,
current model/domain/database/KB changes, Pending/task/last-request restore,
same/foreign Dataset ownership, import/chat normalization and personal-memory
negative/positive contrasts. Redis key/serialization tests use installed
fakeredis with a write transport double because Lua support is absent; these
tests do not claim to verify distributed CAS or a production Redis deployment.

Two existing test expectations/fixtures are formally superseded:

| Test | Old expectation / fixture | Authoritative decision |
| --- | --- | --- |
| `test_api.test_development_can_temporarily_use_fallback_identity` | Valid Bearer, globally unique conversation, no tenant/user returned 401. | STALE_TEST under the current globally-unique-conversation contract. Expect 200 and existing COMPLETED status. |
| Missing-header row of `test_phase0c_scope_contract.test_backend_trust_and_namespace_fail_closed` | Authenticated missing metadata treated as invalid. | STALE_TEST fixture. The negative row now supplies an explicitly blank tenant header and retains 401/STATE_NAMESPACE_REQUIRED. New tests independently cover accepted omission and partial metadata. |

No failing legacy business expectation was weakened to reduce the failure count.
The platform tool/MCP acceptance fixture's two private-address literals were
replaced with reserved `.invalid` example hosts. Its schema, assertions and
no-network behavior are unchanged; the modified API tests were rerun afterward.
In particular the memory write/recall event test remains a historical failure:
this stage does not claim to introduce runtime personal-memory writing.

## Review and boundaries

- Reviewed every changed runtime hunk, compared public schemas, checked all API
  identity call sites and both memory boundary call sites, and inspected the
  complete baseline/final node-ID delta. No unresolved P0 remains in this stage.
- Conversation trust is authenticated upstream input, not proof that a deployed
  gateway actually protects its token or guarantees global uniqueness. That
  deployment evidence remains external. The Agent does not implement user
  permissions, OAuth, RBAC, SSO or role-to-domain mappings.
- Prompt changes: **0**. Regex changes: **0**. Cross-service code changes: **0**.
- Real model calls: **0**. External production writes: **0**. Index publications:
  **0**. Production V2 routing changes: **NO**.
- The new memory validator prevents constructing a personal scope with reserved
  identities; manually bypassing Pydantic validation in trusted internal code is
  not an API capability and is not supported.
- Explicit per-file sync and secret scan precede commit. `change_manifest.json`
  records source/published hashes and `rollback_manifest.json` records safe
  reversal. Final committed blobs, development sources and a clean Git checkout
  are checked after commit; the commit containing this report is its final version.
- Preflight also found three preexisting private-address defaults in tracked
  `app/config.py` (the same count in the baseline). No credentials were found by
  that check. The attempted comment-only edit was removed, so this file is not
  republished by this stage. Record this as repository hygiene P1: moving those
  defaults to environment/template configuration needs a separate runtime
  compatibility check; historical removal must not use an unapproved force push.
  No private addresses are included in these reports.

## V1 replacement and next gate

**Do not replace V1.** The executable graph still delegates to the existing
orchestrator. There are no production imports of `app.semantic_v2`; its contracts
remain frozen at their existing boundary. Passing deterministic fixtures is not
evidence of V2 matching V1 on real multi-turn traffic.

The highest remaining gate is **BLOCKED_CATALOG_PUBLICATION** from PR #7:
the historical local snapshot contains no owned domain projections, and opaque
dimension rules/hierarchies still need governed publication and review. A local
historical snapshot is not evidence for deployed Milvus/MySQL. No publication
target or production operation has been inferred from the continuation request.
Gold/model benchmark and real shadow/canary evidence must follow their agreed
gates. When those results support replacing V1, obtain the user's confirmation
before any switch; do not infer approval from this stage's code/PR authorization.
