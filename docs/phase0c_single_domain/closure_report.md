# Phase 0C single-domain execution closure

**Stage gate: PASS (source and offline integration). Overall Phase 0C: not yet closed.**

The previous stage safely rejected all explicit-domain SQL queries. This stage implements strict single-domain catalog/planning/execution and enables Agent queries only with matching service scope evidence. It also fixes one reproducible parsing P0 discovered by the complete natural-language acceptance path. It does not deploy services or declare production readiness.

## Baseline, audit and chosen objective

- Version repository: `E:/yy`; clean starting commit `8711a3f93a088c63053dcd447d68c2c1b2f9bbcc`.
- New branch: `phase0c-single-domain-execution-20260908-01`, stacked on `phase0c-scope-root-cause-20260908-01` / Draft PR #5. No previous branch history is changed.
- Development workspaces: `E:/YouoAgent/DataAnalysis_Agent` and `E:/YouoAgent/sql-translator`. Oagnet code and its original staged/index changes remain untouched.
- All 704 starting tracked files matched their mapped development sources and Git blobs, except the four documented LF/CRLF presentation differences. Existing scope contracts, prior closure/test delta and open Draft PR state were checked before editing.
- Audit followed request scope validation and binding, restored-state/cache guards, orchestration, semantic resolution, Oagnet evidence admission, ASL translation, raw SQL execution, source selection and Dataset creation. Every current `SemanticCatalog._query` call was examined, together with Redis getters, graph indexes, time-watermark access and the actual publishing/metadata source code in the adjacent services.

The highest actionable previous blocker was the translator's model-only catalog/planning capability. The objective was therefore to complete a bounded **single-domain execution contract**, preserve model-wide behavior and frozen V1/V2 routing, and keep explicit multi-domain input unsupported. Conversation identity compatibility is a separate next stage.

Self-review rejected sharing a mutable domain setting on the singleton translator: concurrent requests could overwrite each other's cache/index scope. The implementation uses new scoped translator/loader/catalog instances for each explicit request, sharing only the Redis transport. It also rejects executing a caller-provided SQL string solely because an earlier translation succeeded: execution must replan the ASL within the current grant and compare both SQL and source.

## Proven findings and resulting behavior

| Finding | First divergence / evidence | Result |
| --- | --- | --- |
| Explicit queries had no strict SQL planning capability | SQL Translator admission and model-only loader/catalog contracts; previous closure and current source | `ScopedTranslator` adds an independent single-domain catalog/planning/execution path. The Agent's blanket single-domain prohibition is replaced by response-proof validation. **EXTERNAL_CONTRACT_GAP closed in source.** |
| Metadata fallback, table indexes and watermark lookup could bypass an ordinary getter-only fix | All catalog SELECTs, direct entity-index scans, `iter_scoped_dimensions` | Catalog tables are restricted before DB reads; indexes use matching model/domain records and real entity IDs; watermark dimensions use the same scoped loader. No global-cache reuse or unscoped Redis fallback. **PROVEN.** |
| Dimensions do not have their own business-domain column | `Oagnet/mysql_tool.py::get_dimension` and the actual SQL publishing script's dimension payload | Eligibility comes from published entity bindings. SQL selects dimensions with an owned entity binding, then retains only owned bindings. Tests deliberately omit the nonexistent domain column. No implicit grant to shared domain `-1`. **PROVEN.** |
| Execution needs current plan and source provenance | `/api/execute` formerly accepted only SQL/model/source | Explicit execution requires ASL, recompiles under the current scope, requires identical SQL and one consistent source, and refuses missing/reassigned sources. The model-wide protocol remains unchanged. **PROVEN.** |
| Calendar period became a business entity | `RULE_PARSE`, `_apply_semantic_catalog_guardrails` implicit metric subject: `查询2025年销售额` produced mention `2025年` despite an already parsed time range; final response failed `ASL_ENTITY_MENTION_UNRESOLVED` | Reuse existing deterministic time grammar before adding the inferred entity. Explicit named filters and product/institution literals remain protected. **REAL_BEHAVIOR_BUG / P0 fixed**, with failing-before evidence in the development run and positive/contrast/final orchestration tests. |

The P0 exception to the frozen V1 rule applies only to this independently reproduced calendar/role error. TurnAdmission, Pending admission, reducers, dataset operations and production V2 routing are otherwise unchanged.

## Single-domain wire and execution contract

- The trusted business backend owns authorization. The Agent and translator enforce the supplied current model/domain; they perform no role/user-permission lookup.
- `MODEL_WIDE` retains the existing path. One domain uses the new scoped path. Multiple distinct domains fail with `EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED`; conflicting aliases/scope fields fail with `REQUEST_SCOPE_INVALID`.
- SQL metadata, translation and execution responses confirm `scope_contract_version=single-domain-v1`, exact `semantic_model_id`, `business_domain_ids` and the canonical complete `authorized_scope_fingerprint` (including database and knowledge-base context).
- Agent metric resolution, definition GET, lineage POST, current-definition lookup, translation and execution retain/verify that grant. Missing, stale-version or foreign response evidence returns `SEMANTIC_SCOPE_UNCONFIRMED` without an irrelevant clarification.
- `/api/execute` receives the original semantic `asl` with the SQL. A changed SQL string fails `SCOPED_SQL_PLAN_MISMATCH`; omitted ASL fails `SCOPED_EXECUTION_ASL_REQUIRED`. Unsupported plans remain rejected. Existing Agent SQL postprocessing cannot bypass this equality check.
- Entity/metric Redis payloads need exact domain membership, and current MySQL membership/formula/graph/source metadata is checked again. Missing domain metadata does not become model-wide. Metric dependencies and all referenced entities must remain in scope and agree on one source.
- A scoped cache refresh does not erase another request's global model cache. Publication changes between split translation and execution are checked by the new execution request's fresh scoped state.
- This stage introduces no persistent plan-token cache, permission database, gateway replacement, index rebuild or production service change.

## Validation and regression delta

| Service | Baseline passed / failed | Final passed / failed | New tests | Old pass → new fail | Collection errors |
| --- | --- | --- | --- | --- | --- |
| DataAnalysis Agent | 1853 / 27 | 1876 / 27 | 23 | 0 | 0 |
| SQL Translator | 157 / 0 | 200 / 0 | 43 | 0 | 0 |
| Oagnet (unchanged source) | 289 / 10 | 289 / 10 | 0 | 0 | 0 |

- **66/66 new tests pass. Critical suite: 160/160 pass**, comprising the existing 137 Phase 0B/0C tests plus 23 new Agent tests. The 43 new SQL cases also all pass.
- No original node ID disappeared; no old failed node is relabeled fixed. All 37 final failures were already present. Oagnet's two UNKNOWN equality/LIKE cases and other previously classified environment/mock failures remain documented in the previous root-cause report.
- DataAnalysis baseline and final suites ran in the established 14 serial module batches, using the existing fixed business clock and offline guard. This is not a claim about single-process order independence.
- SQL baseline and final full suites ran through its offline runner. Oagnet source hashes were unchanged, its committed prior final evidence is the baseline, and its full suite was rerun.
- Integration exercises real Agent parsing/orchestration/adapters, translator API handlers, catalog SELECTs, semantic planning, execution admission and Dataset construction. Oagnet/model responses and source-row execution are controlled doubles; no model or production data is contacted.
- Catalog tests execute the actual restricted SELECT statements against isolated SQLite fixtures. Narrow equivalents implement MySQL JSON membership functions. These are storage-independent contract tests, **not live MySQL dialect/deployment proof**. The fixture schema was corrected during self-review to match dimension ownership facts.
- New tests include ordinary metric, grouping, filter, explicit time and detail/LIMIT planning; natural-language orchestration; cross-domain/shared/foreign-model rejection; metadata scope continuity; response-proof tampering; unsupported multi-domain; raw-SQL/source tampering; changed publication; and interleaved request-local indexes.
- Real model calls: **0**. Production external writes/index rebuilds: **0**. Production V2 routing changed: **NO**. Prompt changes: **0**. Natural-language Regex additions: **0**.

`test_delta.json` records exact baseline/final evidence paths and all new/remaining node IDs. Only final/authoritative baseline evidence is synchronized; intermediate failed development probes remain local.

## Old-test decisions and Regex review

Old tests correctly described the preceding capability blocker. With the now-proven strict path, their blanket single-domain rejection expectations are stale; they are updated against positive integration evidence, not relaxed to hide a failure.

| Tests | Old expectation | New assertion and evidence |
| --- | --- | --- |
| SQL scope capability `[205]` | Always unsupported | Single-domain admission succeeds; the real catalog/API/execute test proves capability. Multiple domains still fail. |
| SQL six-handler × four-alias guard matrix | Reject every valid single domain before backend access | Validate each single-domain alias, then reject its unsupported multi-domain contrast before service access. Dedicated new tests execute single-domain translation, execution, combined execution and metadata handlers. |
| Agent Oagnet discovery `[205]` | Always unsupported | Successful exact-domain discovery; multi-domain remains blocked before calls. |
| Three Agent query/metadata fail-closed checks | Blanket `EXPLICIT_DOMAIN_*_NOT_SUPPORTED` | An upstream stub without scope evidence fails `SEMANTIC_SCOPE_UNCONFIRMED`; it produces no clarification or SQL execution. |

This changes capability expectations for 25 existing SQL and four existing Agent node IDs. All node IDs are preserved. Existing lexical or business-result assertions outside these decisions remain unchanged.

There is one new production Regex: `ScopedCatalog._catalog_table`, for the finite internal `FROM/JOIN semantic_model_*` SELECT grammar. It is never applied to user questions or caller SQL. It restricts each known semantic table using model/domain/ownership predicates; aliases and parameters stay intact. Positive tests execute existing catalog templates, negative tests cover foreign ownership, and an unknown semantic table fails closed. The calendar P0 fix reuses the existing time grammar and adds no Regex or Prompt rule.

## Gate, remaining work and next stage

**SINGLE_DOMAIN_EXECUTION_CONTRACT = PASS** for the audited source/offline boundary. Current-stage parsing P0: before **1**, fixed **1**, remaining **0**. The previous single-domain SQL capability blocker is closed in source; no wider-scope fallback is used to achieve success.

Remaining items are explicit:

1. **Conversation trust compatibility — PROVEN, next stage:** the backend guarantees globally unique conversation IDs, but current API identity handling still requires tenant/user headers. Align that namespace contract and prove that absent stable user principals cannot enable cross-conversation personal memory. No authorization/SSO system should be built in the Agent.
2. **Deployment acceptance — UNKNOWN / external evidence:** actual MySQL schema/dialect, published catalog payloads, deployed Oagnet/translator versions and backend-only service access were not exercised. Deployments with missing ownership facts, unsupported SQL shapes or old response contracts remain fail closed. The service trust boundary must be verified through the existing gateway/service credentials/network controls.
3. **Existing debt:** 27 Agent and 10 Oagnet failures remain classified in prior reports. This stage neither hides them nor claims all business behavior is complete. The Agent's preexisting explicit-domain display-discovery opt-out also remains; independent detail discovery/planning is preserved.

Proceed to **Phase 0C Conversation Trust & State Namespace Compatibility**. Overall Phase 0C is not yet accepted for Gold; production readiness is not asserted.

## Source publication and rollback

`change_manifest.json` records every synchronized file and source/published SHA-256, relative to the starting commit. Only those explicit paths are staged. Credential files, local environment, runtime logs/results, caches and backups are excluded. Original Oagnet HEAD and index are preserved.

Commits separate the SQL service, Agent integration/calendar correction, and closure documentation/evidence. Exact hashes are the branch log after `8711a3f`; the final release commit is the commit containing this report. All tracked development/checkout contents and committed blob identities are verified after committing. Rollback is reviewable with `git revert` of these new commits in reverse order; no reset, clean, force push or automatic merge is performed.
