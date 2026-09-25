# Scope root-cause audit and engineering contract

**Status: SAFETY_FIXES_VERIFIED; Phase 0C remains NOT_CLOSED.** The verified scope defects below are fixed, but complete explicit-domain business execution is still blocked. This report does not declare production readiness or full Semantic Scope Contract acceptance.

## Baseline and scope of this iteration

- Version repository: `E:/yy`, branch `phase0c-scope-root-cause-20260908-01`, starting commit `813073ff3d34a5935e98c5433207ab38d55ccf6b`.
- Development: `E:/YouoAgent/DataAnalysis_Agent`, `E:/YouoAgent/Oagnet`, `E:/YouoAgent/sql-translator`.
- Initial version tree was clean. The 642 preexisting source files mapped to the DataAnalysis and Oagnet workspaces matched, allowing the seven already documented Oagnet LF conversions. Four frozen import-evidence files were subsequently copied individually into the DataAnalysis documentation workspace for consistent provenance.
- Existing Draft PRs #1–#4 were open. This branch is stacked on the Oagnet import branch and must not be merged automatically.
- Authority: the user's 2026-09-08 long-term engineering requirements and current single-domain Scope contract. These rules are persisted in `AGENTS.md`; the workspace root points future work to that contract.
- SQL Translator had no local Git repository. Its service core, dependency manifest, README and all root-level tests are included under `sql-translator/` so the changed service code has a reproducible Git version. Existing production maintenance scripts, logs, caches, backups and credentials are excluded.

## Proven first divergences and fixes

| ID | First divergence | Proven defect / change | Result |
| --- | --- | --- | --- |
| SCOPE-01 | Oagnet / semantic retrieval | `PromptBuilder._build_where` and `build_scope_filter` added shared domain `-1` to an explicit grant. | Exact model/domain filters for every semantic role; missing model rejected. |
| SCOPE-02 | Oagnet / display grounding | Display retrieval also added `-1` and returned names without candidate scope provenance. | Exact filter, candidate model/domain validation, provenance in returned matches. |
| SCOPE-03 | Oagnet / SQL evidence | `get_metric_evidence` allowed unassigned domains; evidence could borrow a vector domain for an unassigned SQL row. | SQL query excludes NULL for an explicit grant; raw vector and SQL model/domain facts must match. |
| SCOPE-04 | Oagnet / field and time repair | Registered-field fallback discarded the requested domain and fetched model-wide physical metadata. | Domain retained through both callers; both table and field queries enforce model/domain through active registered entities before fetching. |
| SCOPE-05 | SQL Translator / request admission | Translation, execution and metadata handlers ignored caller-supplied domain restrictions while using model-only catalog/cache logic. | Structured fail-closed error before catalog, Redis, SQL or export access; nested scope conflicts rejected. This closes silent widening, not the missing single-domain planning capability. |
| SCOPE-06 | SQL Translator / cache restore | Model-qualified entity/metric/dimension misses fell back to global legacy Redis keys; contradictory payload model IDs were accepted. | No legacy-key fallback or scan for a model-qualified request; contradictory payloads cannot populate caches/indexes. Cross-model cache reuse has negative tests. |

All six findings are **PROVEN** by source and offline regression tests. The API policy change rejecting multiple distinct explicit domains also follows the latest confirmed contract; it is not presented as a newly discovered seventh production defect.

DataAnalysis's existing early explicit-query guard remains enabled because the translator still lacks strict single-domain planning. Its explanation now names the remaining translator limitation. No new DataAnalysis workaround expands the scope. The real Oagnet API → actual PromptBuilder → mocked model transport → normalization/validation → metric evidence path is tested with competing model/domain/shared candidates; only model 81 / domain 205 reaches the model and response.

## Validation and delta

| Service | Baseline | Final | Old pass → new fail | Collection errors |
| --- | --- | --- | --- | --- |
| DataAnalysis Agent | 1853 passed / 27 failed | 1853 passed / 27 failed | 0 | 0 |
| Oagnet | 248 passed / 12 failed | 289 passed / 10 failed | 0 | 0 |
| SQL Translator | 103 passed / 0 failed | 157 passed / 0 failed | 0 | 0 |

- New scope regressions: **93/93 PASS** (39 Oagnet, 54 SQL Translator).
- Existing DataAnalysis Phase 0B/0C critical regression modules: **137/137 PASS**.
- All 37 remaining failures were already failing before these changes; no original node ID disappeared.
- The two Oagnet old-fail → new-pass cases are a changed multi-domain contract expectation and a repaired catalog mock, not two claimed production bug fixes.
- DataAnalysis was run completely before and after, in the established 14 serial module batches with the existing fixed clock and offline guard. Oagnet and SQL Translator ran complete local test suites in separate isolated processes.
- Oagnet's preliminary runner attempt exposed incomplete test doubles; it is not the baseline used for delta. The verified runner disables dotenv loading, replaces import-time vector/model clients, blocks external networking and native Milvus construction. SQL tests may only connect to ephemeral loopback HTTP servers created by that same process; existing services are not permitted.
- Real model calls: **0**. External production writes / index rebuilds: **0**. V2 routing changes: **NO**. System Prompt changes: **0**. Production Regex changes: **0**.

Exact node-level results are in `test_delta.json` and its referenced baseline/final evidence. `remaining_oagnet_failures.json` preserves all ten remaining Oagnet failures and distinguishes proven missing dependencies, likely incomplete mocks and two UNKNOWN semantic cases requiring controlled exact-value evidence. No unknown failure was relabeled as fixed.

## Old-test decisions

| Tests | Old expectation / fixture | Authoritative evidence | Decision |
| --- | --- | --- | --- |
| `test_cross_domain_hardening.py` multi-domain PromptBuilder and API cases | Successful multi-domain retrieval, including `-1` in one test. | Latest business contract explicitly rejects more than one distinct domain and forbids implicit shared domains. | **STALE_TEST**: require the precise fail-closed code and prove no backend call. Preserve legacy single-domain success. |
| `test_semantic_evidence.py` parameterized SQL scope case | Two explicit domains accepted by the SQL evidence helper. | Current explicit multi-domain prohibition. | **STALE_TEST**: keep parameterization and de-duplication coverage with one domain; add separate multi-domain rejection tests. |
| `test_semantic_evidence.py` duplicate-code fixture and rejection message | SQL fixture omitted a selected model column; rejection expected a late-stage prose string. | Actual SQL selects `semantic_model_id`; provenance is now checked at first divergence. | **MOCK_DRIFT** / error-contract migration: add true model metadata and assert `SEMANTIC_SCOPE_MISMATCH`, retaining rejection behavior. |
| `test_semantic_display_resolve.py` | Catalog mocks had no model/domain provenance; one filter test did not mock the preexisting exact lookup. | Real indexed records have model/domain metadata; exact lookup is part of the existing API path. | **MOCK_DRIFT**: supply each request's matching metadata and complete the test double. Preserve lexical, ambiguity and negative assertions. |

## Self-review and remaining blockers

1. **EXTERNAL_CONTRACT_GAP — PROVEN:** SQL Translator's catalog and planning caches still have model-only contracts. Single explicit-domain business execution must remain rejected. The next engineering stage is domain-scoped translator catalog/cache/planning with positive end-to-end execution evidence, followed by removing the existing DataAnalysis guard only after both services' contracts are proven. No configuration whitelist can substitute for that work.
2. **Integration constraint — PROVEN:** the latest upstream contract guarantees globally unique conversation IDs, while `app/security.py` still requires tenant/user compatibility headers. This is an interface constraint, not proof of a data-authorization flaw. A later focused namespace change must preserve conversation isolation and disallow personal long-term memory without a stable principal. This iteration does not rewrite identity or memory systems.
3. **Deployment evidence — UNKNOWN:** the trusted backend credential, gateway boundary and deployed service versions were not exercised. No production credential was requested or copied. Local source changes are not proof that deployed servers have adopted the contract.
4. **Legacy evidence debt:** DataAnalysis's 27 historical failures and Oagnet's 10 preexisting failures remain visible. Two Oagnet equality/LIKE cases remain UNKNOWN until exact-value dependency evidence is controlled; no assertion was weakened to hide them.

Review confirmed that modified retrieval filters precede backend reads, SQL evidence cannot borrow scope, unsupported SQL scopes are rejected before access, model-qualified Redis misses cannot enter a global namespace, and no changes touch frozen admission/reducer/pending/dataset behavior. Existing service-token authentication remains in place; this patch neither computes user permissions nor deploys services.

**SEMANTIC_SCOPE_CONTRACT = BLOCKED_EXTERNAL_CONTRACT. Phase 0C readiness = NOT_READY_FOR_GOLD.** Only the listed safety fixes and regressions are complete; the overall phase is not declared closed.

## Versioning and rollback

`change_manifest.json` lists every synchronized file, its source and published hashes, baseline version and purpose. Files are copied individually; line-ending normalization is recorded. The original Oagnet index is preserved. SQL's pre-change source hashes are retained as provenance because no SQL repository existed previously. No environment files or original private deployment addresses are published.

Commits are split into the Oagnet fix, the SQL service/guard fix and the engineering contract/evidence update. Resolve their exact hashes using `git log --format=%H%x20%s 813073f..HEAD`; the final commit is the commit containing this report. Review-only rollback uses those commits in reverse order with `git revert`; this report does not execute a rollback, destructive reset or merge. The prior phase/import branches remain unchanged.
