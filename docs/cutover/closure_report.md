# Current cutover status after the 2026-09-09 gate decision

Current Stage: multi-turn state Gold / component evaluator.
V1 Replacement Readiness: **NOT_READY**; production acceptance has **8 P0 / 4 P1** open.
Offline evaluation is now **ELIGIBLE**, independently of production Redis recovery
and native catalog publication. Entity identity is checked per query shape.

See [current state-evaluation evidence](state_evaluation/closure_report.md),
[parser closure evidence](parser_repairs/closure_report.md),
[identity and gate closure evidence](evaluation_gates/closure_report.md),
[14-entity identity matrix](evaluation_gates/identity_matrix.csv), and
[stage gate matrix](evaluation_gates/gate_matrix.json).

Fourteen provisional identities are supported by current-source aggregate and
physical evidence. 100 reviewed axis cases and deterministic scoring are established.
The initial parser accepted 65/100 outputs. Exact-evidence repair and generation-schema
constraints now produce 94/100 accepted outputs in the final run (two reference failures,
four authentication failures). Ten captured representation failures pass deterministic
replay. Semantic quality and full V1/V2/result acceptance remain open; real shadow has
not begun. Further live requests currently return HTTP401; offline evaluation continues.
Twenty additional transition records now measure actual V1 rule/dataset service
outputs; V2 observation adapters retain real state/patch and Pending decisions.
These service/control results are not full V1/V2 accuracy. See the explicit
evaluation coverage matrix for every remaining required metric.

The historical report below predates this explicit user decision. Its global
catalog-first evaluation gates are superseded; its source/test evidence is historical.

---

# V2 production replacement readiness — blocker audit and catalog contract

Date: 2026-09-08. Baseline: `c4c00d131f53848d279cd7353cd22dc4ea4be4e0`
(region-list Draft PR #17). Branch: `cutover-readiness-catalog-20260908-01`.
Final source/evidence commit is the commit containing this report; independent
implementation commit hashes are recorded in `git_commit_manifest.json`.

**V1 Replacement Readiness: NOT_READY. Phase0C is not closed.** This delivery
establishes the cutover blocker inventory, implements a minimum catalog evidence
contract and fixes one proven canonical-binding defect. It does not declare
V2 accuracy, live catalog consistency, real Shadow or replacement readiness.
Ordinary Legacy optimization stages stop here; remaining work follows the
explicit cutover blockers.

## Why V2 cannot replace V1 now

The missing evidence is not just a catalog check. Current V2 parser/resolver
entry points consume supplied parses, patches and resolutions; no autonomous
raw-turn → binding → TaskPatch path exists. Its frozen permission/snapshot
contract is not bridged to the current trusted MODEL_WIDE/explicit-domain
contract. The adapter does not implement the actual analytical execution path,
and describes ASL 1.0 while the present Oagnet validator/prompt use ASL 2.0.
Deterministic tests of supplied structures cannot prove end-to-end turn accuracy.

Catalog loading had no consistent capture/version identity; index publication
has no proven active-release boundary; query evidence and SQL metadata caches
omit release identity. Real MySQL/Milvus ownership/coverage and deployed caller
trust are unverified. The new capture and supplied-evidence checker closes only
part of the source contract. See `catalog_version_contract.md` for exact source
dependencies, implemented guarantees and remaining publication work.

`v2_cutover_readiness_matrix.json/.csv/.md` covers all 21 requested capabilities
with PASS/FAIL/BLOCKED/NOT_EVALUATED, evidence, severity and next action.
`cutover_blockers.json/.csv` contains 14 distinct blocker groups: 9 open P0,
4 open P1, and 1 P0 fixed in source/offline tests. These groups are not a count
of failed pytest nodes or historical business cases.

## Audit and classification

`source_audit.json` records 130 Python source files in the app/store and two
downstream root-module inventories, plus 18 historical phase closure reports.
Manual review followed current scope/state, V2 parse/binding/adapter, MySQL
capture, vector generation/publication, query evidence, SQL caches and all
remaining-failure paths. This is source audit evidence, not a deployed runtime
audit or a claim that every function was exhaustively verified.

All 37 PR #17 failed nodeids are classified in `remaining_failure_inventory`:

| Classification | Baseline failed nodes | Disposition |
| --- | ---: | --- |
| STALE_TEST | 17 | Assertions retained; production behavior not changed for counts |
| EVALUATION_GAP | 7 | Forecast/history, report-period and partial-result policy labels remain unresolved |
| NON_BLOCKING_LEGACY_FAILURE | 3 | Memory/event/trace wrapper debt retained in backlog |
| ENVIRONMENT_DEPENDENCY | 8 | Incomplete exact-lookup/MySQL doubles; no live credentials introduced |
| CUTOVER_BLOCKER_P0 | 2 | Same exact-binding root cause; fixed and both now pass |

No current node is left with an unexplained UNKNOWN label. This does **not**
mean missing business ground truth is known: the seven policy-label gaps and
30 historical observations with missing replay/catalog/state labels explicitly
remain EVALUATION_GAP, block their acceptance, and cannot be labeled COMPLETE
Gold. `historical_evidence_gaps.json` retains those references without counting
them as 30 independent production bugs. Historical classifications are preserved.
No test expectation was changed.

Newly resolved stale Pending fixtures: `pytest-13` begins with “帮我查销售额”
and expects only one retrieval after the next turn; `pytest-14` begins with
“查询销售额” and expects time clarification. Under the established auditable
default-time contract, the first request already reaches the mocked failing
retrieval and returns SAFE_FALLBACK. The former therefore counts two calls;
the latter fails before its correction assertion. Source fixture inspection,
the unchanged full-run failures, and passing
`test_unqualified_sales_metric_uses_auditable_default_time_range` establish
invalid setup assumptions. This does not certify all later assertions in those
tests or define new business policy.

Two Oagnet API failures were probed with only their missing `find_exact` double
supplied; both original assertions passed. This temporary isolation probe does
not modify official test counts. The other six environment failures reach an
unmocked authoritative catalog lookup in the deliberately offline environment.

## Proven binding P0 and review

First divergence: **OAGNET_ASL_GENERATION**,
`Oagnet/agent.py::_normalize_relation_name_filters`.
After an authorized exact source lookup uniquely confirmed a natural-language
name and display field, the branch changed the predicate to `LIKE '%name%'`.
That silently accepts other names containing the confirmed name. The function's
exact-resolution contract and adjacent exact branches support equality.

The two existing failing nodeids were each run independently twice before the
fix. The minimal fix preserves `=` with the exact value. IN, NOT IN and negative
operators keep their existing behavior; ambiguous/no-match paths still require
resolution. Six new contrast regressions cover exact/equivalent LIKE, negative,
list and no-match behavior. No prompt or production regex changed. This shared
canonical-binding defect affects wrong silent auto-accept, so it is within
cutover scope rather than an ordinary Legacy cleanup.

Self-review checked scope rejection, shared/foreign record exclusion, duplicate
and incomplete inventory rejection, metadata/version drift, missing/changed
markers, transaction/context cleanup, existing loader behavior and bounded
public receipts. New catalog functions are opt-in and not imported into a new
production route. Limitations found during review remain C-01–C-04/X-01 rather
than being hidden by green fixture tests. No separate reviewer/agent was used.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | ---: | ---: |
| DataAnalysis Agent | 2258 / 27 | 2258 / 27 |
| Oagnet | 343 / 10 | 401 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

- New tests: 56 (50 catalog, 6 binding); focused Oagnet: 175 passed.
- Critical existing scope/multi-turn suite: 160/160; this is Legacy/source safety
  evidence, not the requested V2 Gold metric.
- Old pass → new fail: 0. Old fail → new pass: 2. Missing prior nodes: 0.
- Collection errors: 0. Agent modules: all 94 in established serial offline
  batches with controlled classifier clock. Downstream suites use their existing
  offline runners. No dependencies installed.
- Observed Agent clarification responses with reason trace: 89/89 in this suite;
  not a universal production coverage claim.
- All 193 Agent app/tests Python files, public schemas, Oagnet API/prompt/config,
  vector-store runtime and SQL source remain equal to the baseline.
- Real external model calls: 0. Production external writes: 0. Index publications:
  0. Production V2 routing changes: NO. Production model change: NO.

Full node maps and deltas are in `final_*.json` and `test_delta.json`. The final
35 failures are retained and classified; this delivery does not claim full-suite
green or Phase0C gate PASS.

## Release and rollback

Changes are developed in the service workspaces and synchronized only through
`change_manifest.json` into `E:/yy`. Catalog, binding and audit evidence are
separate commits on one independent branch, with Draft PR base
`phase0c-region-list-20260908-01`. No merge or production deployment is performed.
The original Oagnet checkout HEAD and staged binary diff must remain unchanged.
The release verifier compares every tracked file with its development source
and committed Git blob, checks a clean final tree and preserves that original
index. No environment, credential, private snapshot, logs or cache are published.

Use reviewed `git revert` of the corresponding implementation commit(s), then
manifest-based development synchronization and matching regression tests for
source rollback. This has no deployed/index state to undo. Source rollback is
not evidence that runtime Canary rollback has been implemented or drilled.

## CUTOVER READINESS

Current Stage: Cutover Blocker Audit + minimal Catalog capture/release evidence contract

Cutover Blocker P0: 9 open across all blocker classes; 1 source/offline fix

Cutover Blocker P1: 4 open across all blocker classes

Catalog Blocker: 4 open; publication/runtime gate not passed

Evaluation Gap: Gold/deterministic evaluator and controlled real-model comparison absent

Shadow Gap: Real plan-only comparison absent

V1 Replacement Readiness: **NOT_READY**

Next shortest blocking path:

1. C-01/C-02/C-03: connect the captured snapshot to deterministic complete scoped
   generation, trusted activation marker and pinned query/cache release identity.
2. C-04/X-01: verify the named target's real catalog/index coverage and caller
   boundary. No historical local snapshot can replace this evidence.
3. V-02/V-01: adapt V2 to the current trusted scope and build its autonomous
   plan-only recognition/binding/patch path. Keep production routing unchanged.
4. After catalog and Phase0C safety gates pass, curate 80–120 grounded cases and
   report all requested deterministic V1/V2 metrics with zero-tolerance safety
   gates. Resolve authoritative missing labels; never manufacture Gold.
5. Then verify current official model availability, run the controlled sidecar
   benchmark, and enter real plan-only Shadow only after evaluation passes.
6. After offline and real Shadow acceptance, prepare <=5% Canary, automatic
   stop conditions and verified rollback. Only at READY_FOR_USER_APPROVAL ask
   “是否批准V2正式替代V1？”. V1 remains active until explicit approval.

The overarching replacement goal remains active and incomplete. This report
closes the reviewable audit/evidence delivery, not its open cutover blockers.
