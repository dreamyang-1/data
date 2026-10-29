# V2 Boolean filter tree lowering

Baseline `ed35bc37c8c9685e1b1fec265c183862bf418488` (Draft PR #33). Branch `cutover-v2-filter-tree-20260909-01`.
**Bounded Boolean/null filter lowering: verified offline. V1 replacement: NOT_READY.**

First divergence is PROVEN at V2_BACKEND_LOWERING / SQL_TRANSLATOR. Public ASL
has an AND-only filter list. It cannot represent a nested OR or unary NOT, and
its current Oagnet operator subset omits the existing V2 null/NOT LIKE operators.

The internal lowerer preserves these expressions in immutable
`pinned-filter-tree-v1`. It passes the policy separately to the SQL consumer and
requires its hash alongside current scope, catalog and output receipts. The
public ASL object alone is not the complete plan for this path. HTTP request,
response and SSE formats remain unchanged; RawTurnPlanner/V1 routing is unchanged.

SQL validates all leaf fields against the current pin, including OR alternatives.
Flattened leaves exist only in a private planning copy for field/JOIN/source
checks. SQL WHERE is rendered from the original parenthesized tree; governed
metric filters are conjoined outside it. Ranking and filter policies must have
the same semantic fingerprint and both receipts. Caller ASL is never mutated.

The existing literal writer cannot prove backslash interpretation independently
of native SQL mode. V2 now sends even a simple condition containing backslash or
NUL through the private guard and refuses it. This is a remaining transport
contract gap, not a request for the user to restate a metric. Public legacy SQL
execution behavior is unchanged. The tree is bounded and aliases/unknown fields,
operators, policy scopes, mismatched plans and malformed values fail closed.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| Agent | 2661 / 27 | 2681 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 282 / 0 | 319 / 0 |

57 new passing tests: Agent 20, SQL 37. Critical 160/160; trace 89/89;
old-pass to new-fail 0; collection errors 0. No old failing expectation changed.
Initial integration exposed an attempted mutation of frozen ASL. A private copy
fixed it; targeted and complete regressions then passed.

Generated SQL was executed against synthetic SQLite in-memory tables for OR,
nested AND/OR, NOT/double NOT, IS NULL/IS NOT NULL, NOT LIKE, IN containing NULL,
quotes, grouped NULL retention, global filters and combined TopN. These are
SQL semantics fixtures, not production data or deployed MySQL evidence.

Reused timestamped 81/[205] metadata replay: **55 supported / 61 plans**.
The 14 new per-entity filter checks all pass. They check field binding and tree
composition with synthetic null predicates, not business accuracy. Six rejected
plans still share the two unresolved multi-subject metric definitions. No fresh
native observation, business source read, embedding or real model call occurred.

Prompt changes: 0. New Regex patterns: 0. Public I/O / production V2 routing
changed: NO. External SQL / production writes: 0. Oagnet source/index untouched.
This was self-review, not independent review. Mode remains SHADOW_ONLY.

## Cutover readiness

Current Stage: V2 filter-tree/SQL contract and offline execution contrasts.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: native publication, Redis durability/recovery and deployed trust
remain unproven; metric subjects and storage timezone need authoritative facts.
Evaluation Gap: Gold/model benchmark still gated. Shadow Gap: no actual shadow.
V1 Replacement Readiness: **NOT_READY**.
Next shortest blocking path: establish Redis deployment/durability and native
publication authorization, then current 81/[205] native read-back. Finish only
actual backend gaps in parallel, including temporal/relationship/INCLUDE_TIES
and mode-safe literal transport. Publication candidate remains
`../pinned_sql_catalog/publication_candidate.md`; nothing was published natively.
