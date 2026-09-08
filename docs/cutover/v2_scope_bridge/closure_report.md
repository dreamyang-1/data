# V2 current-scope plan bridge: source delivery

Date: 2026-09-08. Baseline: `aa6252f367b7da35774ccbabb17c85545975bd20`
(Draft PR #20). Branch: `cutover-v2-scope-bridge-20260908-02`.

**Offline source gate PASS; V1 replacement readiness NOT_READY.** This delivery
closes the proven V-02 contract mismatch and partially integrates C-03. It does
not close Phase0C or establish autonomous V2 plan accuracy.

## Proven divergence and implementation

The frozen V2 plan validator demanded a database, nonempty domain set and
ACL-shaped row/column/metric evidence. The current authorized contract permits
MODEL_WIDE and an optional database and assigns permission decisions to the
business backend. Consequently a legitimate current request could not compile
a bound plan without inventing permission evidence. First divergence is
`INTENT_ASL_CONTRACT`, before execution.

The explicit logical-plan 0.2.2 profile accepts current AuthorizedSemanticScope
and pinned catalog membership. The 0.2.1 branch retains its original guard.
`ScopedPlanSession` connects current request, scoped catalog handles, restored
state and checked TaskPatch to TurnResolver and LogicalPlanCompiler; complete
pin acceptance is mandatory before a plan leaves the session. Oagnet adds only
the read-time `PinnedCatalog.identity` accessor, which does not replace finish.
The exact contract and remaining limits are in [contract.md](contract.md).

Self-review checked scope/version/namespace forgery, role compatibility,
activation changes, corrupted envelopes, embedded conversation identity,
baseline task versions and Dataset ownership/snapshot references. It also
prevented plan-only code from minting executed results. Existing DatasetState
does not prove a snapshot, so snapshot-dependent references fail closed until
the execution adapter supplies verified evidence. These findings and source
hashes are in `source_review.json`.

## Verification

| Service | Baseline passed / failed | Final passed / failed | New tests |
| --- | --- | --- | --- |
| DataAnalysis Agent | 2258 / 27 | 2348 / 27 | 90 |
| Oagnet | 555 / 8 | 555 / 8 | 0 |
| SQL Translator | 200 / 0 | 200 / 0 | 0 |

All 95 Agent test modules were covered using the established serial offline
batches. The full run initially had 2346 passed / 27 failed; the final 256-test
affected V2 run verified the last plan-identity and Dataset checks and added two
tests. The final result map transparently overlays that run. Raw report hashes
and exact node deltas are recorded in `test_delta.json`; no unaffected suites
were repeated after the final isolated checks.

- Critical Suite: 160 / 160 PASS.
- Existing public clarification trace checks: 89 / 89; no untraced or unsafe ask.
- Old pass to new fail: 0; missing prior nodes: 0; collection errors: 0.
- Existing failure expectations and categories remain unchanged; no stale-test
  edit, Prompt change or Regex change was needed.
- Current public input/output formats, V1 behavior and production V2 routing
  were not changed. No SQL Translator source changed.
- This delivery made zero live catalog reads, real model/embedding calls,
  index publications, external production writes or deployment operations.

The source tests use real Oagnet publication/pin code with fake stores and
synthetic metadata. They do not count as actual publication, native persistence,
recognition accuracy, runtime trust, dataset execution or traffic-shadow proof.

## Cutover readiness

Current Stage: Phase0C catalog/scope closure, before Evaluation Readiness.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: C-01 through C-04 remain open; actual 81 / 205 capture exists,
but isolated publication/native read-back/marker evidence is absent. C-03 now
has a verified V2 compiler path, while autonomous recognition and downstream
metadata/store integration remain pending.
Evaluation Gap: governed Gold and deterministic V1/V2 metrics not yet accepted.
Shadow Gap: no real plan-only traffic comparison or latency evidence.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: obtain the pending operational authorization for
the concrete PR #20 isolated publication candidate, then verify the fresh
81 / 205 target and native release acceptance. In parallel, connect actual
raw-turn recognition and deterministic patch generation through ScopedPlanSession
(V-01), then close Dataset/ASL/state integration gates before Gold and shadow.
The user's scope choice is not publication permission. Current public model
defaults stay unchanged; V1 replacement still requires explicit approval after
all readiness gates pass.

## Release and rollback

Development files are synchronized individually to `E:/yy` using the explicit
change manifest, with CRLF-to-LF normalization and SHA-256 verification. The
original Oagnet checkout HEAD and staged user diff must remain preserved.
Implementation commit IDs and final evidence commit identity are recorded in
`git_commit_manifest.json`; final verification checks every committed blob and
requires a clean version repository. The Draft PR stacks on PR #20, with no merge.

Rollback is a reviewed revert of this source delivery and explicit manifest
resync. No live state/index/routing changed, so no live rollback was performed
or claimed. See `rollback_manifest.json`.
