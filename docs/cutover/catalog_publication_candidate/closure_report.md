# Reviewed catalog operation and current target evidence

Baseline: `df09b52ea4ab76254f5005619aefa8e4cb87ac5f` (Draft PR #29).
Branch: `cutover-catalog-publication-candidate-20260909-01`. Date: 2026-09-09.

**Operator source acceptance: PASS. Native publication: BLOCKED.**
V1 replacement readiness remains **NOT_READY**.

The previous publication candidate referred to a catalog version from before
the relationship-type and entity-value-source contracts. Fresh read-only
inspection now binds the candidate to the current 81/[205] snapshot, Milvus
target and configured embedding contract. It still contains 290 static records
and verifies 89 active governed attribute/source mappings. Exact hashed
identities and timestamps are in `live_scope_evidence.json`.

Both isolated catalog collections and the scoped active marker are absent.
The legacy semantic collection contains 142 exact-scope rows with no release
stamps; the exact-scope legacy physical query contains zero rows. This is not a
verified native publication. No embeddings, collection creation, registry write,
business-source lookup or production route change was performed.

## Concrete operational blocker

Current Redis reports AOF disabled and an empty scheduled RDB save policy.
Those facts establish that automatic persistence is not configured through
either mechanism. Manual snapshots, host backups, persistent-volume topology
and durable recovery are unknown. The source registry's no-TTL keys and CAS
behavior do not substitute for deployment persistence. This finding is recorded
under existing C-02/X-01 rather than counted as a duplicate independent bug.

The project identifies only the configured Redis connection, not its deployment
or volume-management location. That information has been requested from the
user. No policy, global Redis setting or restart is chosen without the actual
deployment evidence and required operational authorization. The earlier native
publication authorization request is still unanswered; the stale candidate is
explicitly superseded by the current reviewable artifact.

## Operator code change

First divergence: `CATALOG_PUBLICATION_OPERATOR_BOUNDARY`, proven by source.
Previously the CLI accepted a producer revision but did not require the reviewed
target, catalog version or embedding identity. A separate preflight could become
stale before collection initialization or the actual publisher capture.

Oagnet now checks expected target identity before constructing a Milvus client.
Operator writes require that target precondition. Publish additionally checks
embedding identity before opening a store and passes the reviewed source version
into the publisher, which checks the exact capture before embeddings,
reservations or index writes. Existing full native inventory verification,
authority freshness checks, immutable generations and CAS activation are retained.

These are operator preconditions, not user permissions. Internal callers retain
their existing behavior without an optional reviewed-source constraint. Public
HTTP/SSE formats, Agent behavior, V1 routes, model defaults and V2 shadow policy
are unchanged. No Prompt, Regex or business test expectation changes were made.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2615 / 27 | 2615 / 27 |
| Oagnet | 633 / 8 | 649 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

New passing tests: **16**. Focused operator/publication suite: **103/103**.
Critical: **160/160**. Public clarification trace: **89/89**.
Old-pass to new-fail: **0**. Collection errors: **0**.
Complete final service reports and exact unchanged failure node IDs are retained.

The initial SQL run had a loopback `WinError 10053` in
`SplitAPIContractTests.test_route_matching_is_exact`. Two independent node runs
and a complete SQL rerun passed unchanged. The precise host/transport cause is
unknown; the transient remains in `test_delta.json`. No SQL source or expectation
was changed, and repeated runs are not added together.

Review checked refusal before effects, successful guarded publication/read-back,
scope rejection before opening clients, initialization without embeddings and
existing reader compatibility. This was a self-review; no independent reviewer
or native write/restart validation is claimed.

## Cutover readiness

Current Stage: current publication candidate, operator guards and native target audit.
Cutover Blocker P0: **8 open**. Cutover Blocker P1: **4 open**.
Catalog Blocker: native publication absent; automatic Redis persistence disabled
and durable deployment recovery unproven.
Evaluation Gap: Gold/metrics and real-model benchmark remain gated.
Shadow Gap: no actual plan-only shadow evidence.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: obtain the Redis deployment/volume-management
location, prepare and authorize the appropriate durability/recovery operation,
then obtain native publication authorization and execute the refreshed guarded
81/[205] candidate. Verify full native read-back, marker durability and actual
scoped source use before ending catalog gates and starting Gold evaluation.
The exact proposed publication commands are in `publication_candidate.md`.
