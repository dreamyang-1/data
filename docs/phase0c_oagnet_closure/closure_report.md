# Phase 0C-1.5 Oagnet scope closure review

**Overall gate: BLOCKED_CATALOG_PUBLICATION. Phase 0C-2: NOT_STARTED.**

The root fixes and synthetic/offline acceptance pass. This is not a declaration
that the current persisted catalog or deployed services satisfy the full gate.
No Gold/benchmark work was mixed into the scope stage and no model ranking was
invented. The production model and V2 routing remain unchanged.

## Baseline and audit boundary

- Version baseline: `cdb2a8cfb059c841fbd8de51eb3c3a59cb0cf981`, containing the
  previously completed root-cause and single-domain SQL stages. The attachment's
  `e406029` / `813073f` are historical ancestors, not the current workspace.
- Feature branch: `phase0c-oagnet-scope-closure-20260908-01`; base branch:
  `phase0c-single-domain-execution-20260908-01` / Draft PR #6.
- All 743 starting tracked files and committed blobs matched their development
  workspaces, with only seven previously documented line-ending differences.
  The version working tree was clean. The separate Oagnet HEAD/index remain
  preserved; only its working source is edited and explicitly synced to `E:/yy`.
- Full-file reads and AST inventories cover `api.py`, `prompt_build.py`,
  `agent.py`, `mysql_tool.py`, `vector_store.py` (13,972 baseline lines,
  285 function definitions). `source_audit.json` preserves hashes, function
  ranges and call references. Manual boundary review follows admission,
  retrieval, graph completion, exact-value SQL authorization, ASL repair and
  validation, evidence assembly and catalog publication. Unrelated business
  heuristics are indexed, not claimed to have received a new behavioral proof.
- Repository searches covered `-1`, business-domain aliases, `get_dsl_by_scope`,
  scope and domain IDs. Maintenance scripts, physical-source lookup sentinels
  and ordinary negative Python indices were separated from semantic-query
  authorization. No maintenance script or remote service was executed.

## The twelve requested -1 findings

| Question | Evidence and conclusion |
| --- | --- |
| First definition | `vector_store.build_records_from_dsl` assigns `-1` when a DSL document has no business-domain header and unconditionally for shared dimensions/enums. It is not a named authorization constant. |
| First addition to a query | In available import commit `813073f`, `PromptBuilder._build_where` appended `-1` to the explicit domain set. `build_scope_filter` and display resolution repeated that policy. Original authorship before the available history is not asserted. |
| Trigger | A positive explicit business domain was supplied; dimension/enum publication independently used model-wide storage. |
| Why | Publisher code, model-only `get_dimension` SQL, shared record IDs and stored binding data show an attempt to make shared dimension definitions retrievable. This was storage compatibility, not backend authorization. |
| Endpoints | `/agent/query` and `/vector/semantic-elements/resolve` inherited shared query widening. `/vector/entity-attributes/search` already issued exact domain filters, but lacked independent returned-payload proof. Rebuild endpoints manipulate shared publication as maintenance. |
| Vector retrieval | Prior widening was removed in the earlier root-cause commit. Exact request filters remain. This change closes the publisher/retriever mismatch using separate owned projections, without adding `-1` to queries. |
| Semantic Catalog | `get_dimension(model)` has no domain column and returns model-level definitions; `get_dsl_by_scope` is a publisher source. `get_entity` loads the selected domain; metric SQL uses model/domain. Query-time physical/attribute fallbacks retain domain predicates. |
| Entity values | Vector exact and approximate filters retain model/domain. Source exact-value resolution joins model -> domain -> entity -> attribute -> registered data source/table/field. It does not use shared-domain `-1`. New payload checks reject a stale/foreign metadata envelope. |
| Relationships | Published relations require both endpoint IDs in the scoped entity map. Prompt graph completion retrieves entities/relations through the same scope; new metadata proof covers its deterministic lookup results too. |
| ASL | The old shared dimension payload could bring foreign bindings into model context and the validator's known-field set. New projections contain only verified owned bindings; a foreign binding returned by the model fails real ASL validation. |
| Actual records | Read-only inspection of the local persisted Chroma snapshot found **66 dimensions and 70 enums** under `-1`, among 1,836 records. No actual record name, ID, value, SQL row or user conversation is published. This is local historical data, not current deployed Milvus evidence. |
| Expansion of an explicit domain | **PROVEN** for the historical code: adding a model-level shared record admits its unrestricted entity bindings. The storage marker cannot authorize those bindings. Current query tests show zero expansion; old shared-only snapshots remain unavailable to explicit dimension retrieval. |

Classification of the requested **business-domain** marker: **GLOBAL_SHARED**.
This is supported by code and stored records, not naming alone. The separate
physical `semantic_model_id=-1` sentinel and Python `[-1]` indices are not this
classification and do not authorize business-domain scope expansion.

## Root causes and implemented changes

| Finding | First divergence | Fix / evidence |
| --- | --- | --- |
| Shared dimension publication incompatible with strict retrieval | OAGNET_SEMANTIC_RETRIEVAL / publication | Preserve governed entity/attribute IDs; publish separate domain-owned dimension/enum projections; retain global originals for model-wide queries. Rehydrate mappings from owned rows, remove foreign and unproven bindings. Positive, contrast, stale projection removal, model-wide isolation and real ASL tests. **PROVEN.** |
| Returned vector JSON could contradict filtered scalar columns | OAGNET_SEMANTIC_RETRIEVAL | Prompt search, deterministic lookup and fallback all validate returned model/domain before rendering. **PROVEN by fault injection**, not a claim of an observed production incident. |
| Entity-value endpoint echoed current model without validating candidate model | OAGNET_SEMANTIC_RETRIEVAL | Check exact and approximate results before merge/ranking; fail with `SEMANTIC_SCOPE_MISMATCH`. Valid explicit/model-wide contrasts also pass. **PROVEN by endpoint fault injection.** |

Implementation and tests are confined to Oagnet. DataAnalysis's defense in
depth, SQL Translator behavior, prompts, runtime model settings, identity
system and V2 routing remain unchanged. No regular expression was added.
The prompt template string is byte-for-byte unchanged; only governed catalog
payloads and deterministic admission have changed.

## Regression and self-review

| Service | Baseline | Final | Old pass -> new fail | Collection errors |
| --- | --- | --- | --- | --- |
| DataAnalysis Agent | 1876 passed / 27 failed | 1876 passed / 27 failed | 0 | 0 |
| Oagnet | 289 passed / 10 failed | 343 passed / 10 failed | 0 | 0 |
| SQL Translator | 200 passed / 0 failed | 200 passed / 0 failed | 0 | 0 |

- **54/54 new acceptance tests PASS**; existing Agent critical multi-turn/scope
  and single-domain suite **160/160 PASS**. All original node IDs are retained.
- Oagnet ran a fresh complete baseline and final suite. Unmodified Agent/SQL
  baselines are their exact committed final results; both full suites were
  rerun. Agent retains its established 14 serial batches and controlled clock.
- Real model calls, remote production writes, deployments and index rebuilds:
  **0**. Synthetic in-memory publication tests are not production publication.
- Five old test modules receive fixture-only repairs: four lacked model/domain
  facts present in real indexed records, and one fake store did not understand
  the already-supported `$in` type filter. No expected business result or
  original node ID changed. The ten old Oagnet failures retain their previous
  evidence classification; no historical failure is claimed fixed.
- Review prevented projected copies from competing with global dimensions for
  model-wide top-k slots by using separate record kinds in the existing semantic
  collection. It also replaced stale binding mappings with ownership-proven
  attribute mappings and added fallback-path scope validation.
- Opaque mappings/rules/hierarchies remain rejected, not guessed. Rebuild stats
  now distinguish total dimension candidates from published owned projections.
  This is needed to prevent transport success being reported as coverage PASS.
- Public evidence is node outcomes, aggregates, source references and hashes.
  Raw diagnostic logs remain outside Git; the snapshot auditor emits no records.

`test_delta.json` and sanitized baseline/final node outcomes provide exact
counts. The tests prove source behavior and controlled integration, not remote
database schema or deployed service correctness.

## Hard gate and remaining blockers

| Required item | Result and evidence boundary |
| --- | --- |
| Required strict model; MODEL_WIDE semantics | PASS in source/current offline suite |
| Explicit query adds -1 / other domains | 0 in source/current offline suite |
| History expansion; Pending/Dataset/cache cross-scope reuse | 0 in unchanged Agent acceptance suite |
| Entity, metric, attribute, relationship retrieval | PASS in source/current offline suite |
| Entity-value and semantic display resolution | PASS in scoped positive/negative endpoint tests |
| Dimension retrieval and ASL scope | PASS for ownership-proven published projections; **BLOCKED for the current old snapshot and unsupported opaque definitions** |
| Critical multi-turn; old-pass -> new-fail; collection errors | 160/160; 0; 0 |
| Catalog publication / current deployed evidence | **BLOCKED / UNKNOWN**, not inferred from local mocks |

The local snapshot has **0 explicit dimension projections**, and **0 of 126
entity records contain a governed entity ID**. It therefore cannot prove the
ownership needed for this contract. In addition, **26 shared dimensions have
opaque special rules and 5 have hierarchical definitions**; these counts may
overlap. Their field/ownership contracts need catalog evidence before support
is extended. These facts are in `catalog_snapshot_aggregate.json` and were
obtained with a read-only SQLite transaction, without loading credentials.

The next permitted action is the controlled plan in
`catalog_publication_plan.md`: obtain an authorized target/environment, verify
current catalog ownership, review projection coverage, deploy through the
existing process and republish the approved catalog scope. No old record may
be relabeled as authorized, and excluded definitions must stay explicit
blockers. The local snapshot does not prove what is currently in Milvus.

**PHASE_0C_1_5 = BLOCKED_CATALOG_PUBLICATION. PHASE_0C_2 = NOT_STARTED.**
Gold count, evaluator metrics and tested benchmark candidates: **not produced**
because the preceding hard gate has not passed. Best Overall/Turn/Semantic/ASL,
recommended thinking mode and model improvement: **NOT_EVALUATED**. The existing
model is kept operationally, without a claim that it won a benchmark.
**Phase 1A readiness: NOT_READY.**

## Versioning and rollback

The root implementation/tests/operator audit form one independent Oagnet commit;
closure evidence forms a second commit. The branch is pushed with a Draft PR
against its immediate baseline branch; there is no merge or force push.
`change_manifest.json` lists every individually synchronized file and hashes.
Resolve final hashes from the branch log after `cdb2a8c`; the final evidence
commit contains this report. A rollback would use reviewed `git revert` commits
in reverse order, not reset/clean. No deployment or index rollback is needed
because neither was performed.
