# Dataset lineage and completeness P0 closure

**Stage: PASS (source and offline acceptance). V1 replacement: NOT_READY.**

Three reproducible P0 paths in existing dataset follow-up are fixed: a display
slice no longer supplies false global ranking evidence; expanding a display no
longer removes a materialized filter; a join no longer loses input incompleteness.
Normal three-turn reuse recovers the correct complete result without issuing a
second query. Input/output formats follow the existing code.

## Baseline and audit decision

- Baseline commit: `119d84d70d46867f655e8de2bf44838f44d2cd46` (Draft PR #8).
- Branch: `phase0c-dataset-lineage-20260908-01`; Draft PR base:
  `phase0c-conversation-trust-20260908-01`.
- All 767 baseline tracked files and committed Git blobs matched their
  development workspaces after the already-documented CRLF/LF normalization.
  The version checkout was clean and the separate Oagnet index was preserved.
- Reviewed the preceding closure and outstanding scope/catalog gates, current
  failure triage, request/state boundaries and the actual dataset call chain.
  Inventory covers all 99 `app` Python files plus the standalone MinIO module:
  100 files / 1,290 final functions. The hashes are in `audit_inventory.json`.
  AST coverage is an inventory; manual review focused on state restore,
  follow-up planning, ancestor lookup, JSON/Parquet persistence, join coverage,
  result reliability and public IO. It is not a new proof of every legacy
  heuristic or a deployed-service audit.
- The 27 remaining Agent failures still include obsolete mandatory-time test
  premises and documented unknowns/debt. No expectations were changed to reduce
  their count. Oagnet publication still needs external evidence, so the prior
  gate does not yet permit Phase 0C-2 Gold/model benchmarking.
- The current request authorizes choosing a sensible independent next stage.
  These new deterministic dataset P0 proofs justify the narrow exception to
  frozen V1 behavior. No TurnAdmission, Pending parser, slot rules, Prompt,
  Regex, authorization policy or V2 production routing was changed.

## Proven failures and fixes

All examples below are synthetic. Each original failing node was independently
run twice before runtime edits (six failed runs, zero collection errors). The
same failures also occurred in the initial combined reproduction. They are three
verified implementation paths within existing dataset/inheritance failure
families, not a fabricated recount of the historical Failure Universe.

| ID | First divergence / code | Before | After |
| --- | --- | --- | --- |
| DL-01 | DATASET_FOLLOWUP: `_try_dataset_followup` completeness check | Original values include 10, 20 and 999. After “只看前2条”, “全局销售额最高1名” returned **20**. It equated loading two stored rows with complete original coverage. | Detect the slice from transformation history; recover an available, scope-compatible complete parent and re-plan the operation there. Result is **999**, using the existing dataset path. |
| DL-02 | DATASET_FOLLOWUP: `_expandable_dataset_reference` unrestricted ancestor search | Filtering to region 甲, then requesting five rows, returned region 乙 rows from the unfiltered parent. | Traverse only proven slice edges. Filter, projection, aggregate, join, changed scope/snapshot or inconsistent lineage form barriers. A limit after a filter may restore the filtered parent, never the unfiltered grandparent. |
| DL-03 | DATASET_FOLLOWUP: standalone join persistence | Joining a truncated input discarded its source flags; subsequent global ranking succeeded on incomplete evidence. | Persist input coverage in the existing transformation log, propagate it through further transforms/joins, reject unsupported global reuse and report existing LIMITED/warnings semantics for incomplete joined results. |

P0 paths before: **3**; fixed: **3**; unresolved in this stage: **0**.
Root cause class: **REAL_PRODUCTION_BUG**. Cross-service patches: **NO**.

## Deterministic reuse contract

`dataset_source_complete` distinguishes successful materialization from source
population coverage. Known truncation, row limits/rank slices, partial reads,
unknown transformations, missing derived lineage and old joins without coverage
proof cannot establish completeness. Subsequent projection, filtering, sorting,
aggregation or joins cannot remove prior evidence of incompleteness. Complete
filtered/aggregated populations remain valid within their materialized scope.

`presentation_ancestors` requires a single parent, matching source reference,
scope, model/domains, snapshot, columns and data timestamp, an exact transformation
log prefix, and row counts consistent with the recorded limit. It detects cycles.
Display expansion may undo only pure limits, preserving original ordering. A new
global operation may also undo a sort-limit, because it is planned afresh on the
complete parent. Scope-invalid references are excluded before parent object IO.

If “show ten” reaches a complete six-row parent, all six available rows are
returned instead of silently keeping the previous two-row slice. If a complete
parent is unavailable, expired or unreadable, unsupported automatic global reuse
returns through the existing database-query path. Explicit dataset selection
never silently changes to another snapshot; incomplete global operations use the
existing explicit-dataset failure path. Display limits and already-supported
projection/local-sort operations remain usable on incomplete sources.

Recovery intentionally does not replay arbitrary transformation graphs. A slice
followed by a filter/projection/sort cannot be expanded by skipping those steps;
it must retain the selected population or use the existing safe re-query/fallback
behavior. Unknown lineage remains fail closed. A global operation's correctness
is relative to the materialized query/filter population; no user authorization or
domain is expanded. No index, state store or stored dataset was migrated/deleted.

## Public interface and compatibility

Baseline/final OpenAPI documents and JSON schemas for ChatRequest, AgentResponse,
SpreadsheetImportRequest and SpreadsheetImportResponse compare equal. The
DatasetReference field names/types also compare equal. Hash receipts are in
`audit_inventory.json` and `test_delta.json`.

No endpoint, input field, response envelope, DatasetReference field, parser rule
or SSE framing was redesigned. The join uses `source_truncated` within its
existing transformation-log metadata and existing reliability level/warnings to
describe incomplete coverage. Data-only SSE and existing error/fallback formats
are retained. These metadata/status values correctly change for affected cases;
this is not a claim that incorrect old answer values are preserved.

Old immutable joins lacking input-coverage evidence are conservatively unavailable
for global ranking/aggregation. They can still support allowed display operations.
There is no automatic migration or guessed completeness. Old complete root
snapshots retain the existing persisted-row-count contract.

## Verification

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 1,919 passed / 27 failed | **1,955 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multiturn/scope/single-domain | 160 passed | **160 passed** |
| Conversation trust contract | 43 passed | **43 passed** |
| New dataset lineage contract | Three failing proofs | **36 / 36 passed** |

Old pass -> new fail: **0**. Missing baseline node IDs: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old test expectation changes: **0**. Prompt changes: **0**. Regex changes: **0**.

Affected existing modules were freshly run before implementation: **212 passed**.
The full Agent run covers all test modules using the 14 established offline
batches plus the conversation/lineage modules. After the full run, two further
join-reliability contrast cases were added and the complete lineage module was
rerun: **36 passed**. No runtime code changed after the full run; final node
results combine the full run and that final module run. Oagnet/SQL full guarded
offline suites were also rerun. The existing classifier clock fixture was used.

Tests cover persisted JSON and Parquet flows, exact numeric counterexamples,
multi-turn reuse with only one retrieval call, repeated slice expansion, available
row limits, filters as ancestry barriers, explicit dataset selection, missing and
expired parents, foreign model/scope without parent reads, mutation after slices,
legacy/unknown lineage, metadata inconsistencies, cycles and complete/incomplete
join reliability contrasts. Fixtures use FakeMinio and synthetic rows; no model
or production database is used. One new fixture initially omitted required
`scope_mode`; it was corrected to the formal MODEL_WIDE schema before acceptance.

Real external model calls: **0**. External production writes: **0**.
Index publications: **0**. Production V2 routing changes: **NO**.

## Review, Git and remaining gates

Reviewed all changed runtime hunks, source/metadata authority, alternate stores,
expiry/failure paths and the baseline/final test delta. New mutations are limited
to existing derived-result persistence; testing used local in-memory fixtures.
The explicit per-file manifest, secret scan, committed-blob verification and safe
rollback instructions accompany the report. The commit containing this report
is the stage's final commit; the post-commit verification receipt confirms exact
workspace/Git agreement and a clean version checkout. No unrelated source or
separate Oagnet index changes are included. No automatic merge is authorized.

**V1 is not ready to be replaced.** The production graph still calls the existing
orchestrator; the audit found no production imports of `app.semantic_v2`. The
highest remaining overall gate is PR #7's **BLOCKED_CATALOG_PUBLICATION**, including
owned domain projections and opaque catalog rules/hierarchies. There is still no
deployed publication proof or completed Gold/model/shadow/canary evaluation.
Those gates remain in force. The previously recorded configuration-hygiene P1
and unrelated legacy failures remain recorded rather than silently fixed here.
When evidence supports V1 replacement, obtain user confirmation before switching.
