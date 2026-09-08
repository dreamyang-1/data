# V2 TypedPlan to ASL 2.0: partial lowering evidence

Baseline: `abd6e82bfffb683d829e408581dda583633ec8d8` (Draft PR #31). Branch: `cutover-v2-asl2-lowering-20260909-01`.

**Implemented common-query path: verified offline. Full V2 lowering: PARTIAL.**
V1 replacement remains **NOT_READY**; this delivery does not close S-01/C-03.

The new internal lowering consumes an AuthorizedLogicalPlan in its original
live ScopedPlanSession. It maps supported typed fields/operators/values into
the actual ASL 2.0 format, preserves projection identity and display labels,
and returns bounded unsupported reasons when a required semantic feature has
no proven representation. It does not edit the frozen ASL 1.0 legacy adapter.

The opt-in `compile_asl2` path calls the real pinned SQL consumer with this same
pin. Before any SQL leaves, it checks the returned full scope fingerprint,
publication identity and ordered SELECT aliases. The SQL consumer performs
the original full finish. Normal `compile` and RawTurnPlanner/public routes
retain their behavior; no production route invokes the new method.

Output bindings remain UNKNOWN until actual column metadata is supplied.
The structural result helper then invokes the existing ResultContract prover.
An implicit 10,000-row ceiling is explicit and reaching it cannot prove a
complete result. These fixture proofs do not establish real execution receipts,
source snapshots, business labels or model accuracy. Execution stays disabled.

## Actual metadata

Fresh read-only 81/[205] capture at 2026-09-08 17:55 UTC retained catalog version
`3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`. It still contains 290 static records and
89/89 verified attribute/source mappings. No embedding service or native writes.

Typed-plan replay through the actual V2/compiler/pin/SQL source implementations
with in-memory persistence: **32 supported / 36 plans**. Four plans are explicitly
unsupported, affecting two metric definitions with two different bound entity
base tables each. Primary subject ownership is not established by the current
typed plan/catalog evidence. Their hashes and reasons are retained; no subject
was selected by list order. This is one governance gap, not four independent bugs.

Storage/session timezone is also unproven. The owner has been asked for the
81/[205] date-time convention; no answer has arrived. Bounded time and timezone
values do not silently become unqualified date strings. Full time/ranking/Boolean/
relationship/comparison capabilities remain listed in `capability_matrix.json`.

## Validation and review

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2615 / 27 | 2645 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 251 / 0 | 261 / 0 |

New tests **40** (30 Agent, 10 SQL). Critical **160/160**; trace **89/89**.
Old-pass to new-fail **0**; full-suite collection errors **0**. Historical
failure expectations were not edited. A separate temporary cross-drive replay
harness encountered two collection errors before direct offline replay; those
attempts and the resulting partial capability evidence are not hidden in a PASS.

Self-review checked source/role membership, immutable artifacts, exact decimal
conversion, typed filter values, current scope and pin mismatch, mid-plan source/
index drift, real SELECT aliases, null-group preservation, projection labels,
implicit row caps and unsupported ranking/time/Boolean semantics. The SELECT
alias scanner excludes quoted literals/functions and refuses comments or
unrecognized projection syntax. It is not a complete SQL grammar validator.

Public I/O changed: NO. Production V2 routing changed: NO. Prompt changes: 0.
New Regex patterns: 0. Real model calls, business reads, SQL executions and
production external writes: **0**. Review was self-review, not independent review.

## Cutover readiness

Current Stage: partial V2-to-ASL2 lowering and output binding.
Cutover Blocker P0: **8 open**. Cutover Blocker P1: **4 open**.
Catalog Blocker: native publication/Redis durability/deployed trust still unproven;
primary subject and temporal storage conventions need authoritative evidence.
Evaluation Gap: Gold and model benchmark remain gated.
Shadow Gap: no actual plan-only shadow evidence.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: establish Redis deployment/storage ownership and
recovery, then authorize the current guarded 81/[205] native publication and
verify full native read-back. In parallel source work must complete the explicit
lowering gaps and obtain the missing time/subject facts before Gold evaluation.
The publication candidate remains `../pinned_sql_catalog/publication_candidate.md`;
this stage changed no Oagnet capture semantics or native state.
