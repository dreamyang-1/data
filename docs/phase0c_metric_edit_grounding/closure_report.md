# Phase 0C metric edit grounding closure

Stage result: **PASS — complete metric-only ADD/REMOVE commands do not add
operation fragments as entity constraints**. This is a bounded V1 P0 correction;
it does not authorize V1 replacement or entry into Gold/model evaluation.

Development: `E:/YouoAgent/DataAnalysis_Agent`; version repository: `E:/yy`.
Baseline: `b80492044882b409559a67d7e4bd60c1497be3fc` (PR #14).
Branch: `phase0c-metric-edit-grounding-20260908-01`.
Draft PR base: `phase0c-dimension-mutation-20260908-01`.
The commit containing this report identifies the final source tree. The
post-commit receipt verifies every tracked blob against development.

## Audit and stage selection

At entry, all **823 tracked files** matched their committed blobs and development
sources under the established LF publication policy. Git was clean and the
original Oagnet index matched its preserved import manifest. Reviewed PR #14's
closure, failure evidence, current trust/scope contract and the unresolved
catalog-publication gate. Inventoried all **100 Agent app/standalone-store Python
files and 1,298 functions** by AST. Manual review followed raw parsing, admission,
rewriting, Hybrid enrichment, semantic sanitation, verified context restoration,
Intent-ASL construction and the actual HTTP adapter. Public schemas, state-scope
boundaries and production routing were also checked. This inventory is not a
proof of every legacy heuristic or deployed catalog record.

PR #14 observation DM-A01 was still UNKNOWN. It was selected for investigation
because operation fragments appeared alongside a correct metric edit and then
persisted into later turns. DM-A02 filter-family edits and unmarked regrouping
remain separate evidence requirements.

## Reproductions and first divergence

The base task is `查询2026年7月销售额和销售量，地区上海`. Before editing, the
tests verify a completed query, the Shanghai entity/filter and the July period.

| Signature | Current turn | Before | Final |
| --- | --- | --- | --- |
| MG-01 / ADD | 再加订单笔数 | Correctly adds the metric, but emits entity `再加` | Three metrics; Shanghai/July retained; no operation entity |
| MG-02 / REMOVE | 不要销售量 | Correctly removes the metric, but emits entity `不要` | Sales amount retained; Shanghai/July retained; no operation entity |

The earliest divergent result is **RULE_PARSE**: the implicit metric-subject
fallback admits the operation prefix, and the existing sanitizer accepts it.
**STRUCTURED_INTENT** is a second producer of the same invalid candidate: a
schema-valid `current_entity_values` proposal passes literal-presence checking
and the same sanitizer. The later orchestrator merge can persist it and the
HTTP adapter forwards it as a caller-owned semantic constraint.

These are two dialogue-act failure signatures in one missing-guard family,
tested through **four boundary variants**. Every variant failed independently
twice before runtime changes. The production rewriter path was exercised with
an offline model returning a schema-valid operation/entity role mistake. The
additional constructor path omitted the optional rewriter and exercised actual
model-unavailable fallback. Production dependency injection always supplies a
rewriter, even when semantic search is disabled; the constructor contrast is
not represented as a deployed configuration.

Clean model output plus the real rewriter, including genuine unavailable-model
fallback, already passed before the patch. The direct Legacy merge-to-HTTP
contrast also passed. These passing paths do not disprove the model role error,
and the original mock-only observation alone was not treated as sufficient P0
evidence. An initial invalid None-return model fixture was corrected and its
failure discarded; model unavailability follows the existing caught exception
contract. No real model was called.

The outgoing payload is captured by the **real HTTP adapter** at an offline ASL
transport. Only fixture metric IDs/versions are cleared before that boundary
to avoid mock catalog ID conventions; entity mentions, filters, time, scope and
admitted state are preserved. This proves the caller-owned constraint defect,
not a deployed SQL result. Source review confirms that the unchanged downstream
validator requires source-backed entity filters and otherwise raises
`ASL_ENTITY_MENTION_UNRESOLVED`. Ignoring a fabricated entity in Oagnet would be
the wrong repair; the producer must stop creating it.

## Minimal change and review

Runtime changes are confined to `app/intent/classifier.py`. Its existing
`sanitize_semantic_entity_mentions` entry point now recognizes complete metric
ADD/REMOVE commands and excludes their operation-only spans. This entry point
already runs after rule parsing, after model enrichment and at the orchestrator
convergence boundary. No new pipeline stage or response contract is introduced.

Recognition uses existing known/current metric labels and the existing Legacy
addition/negative-slot vocabulary. A command must be consumed completely; its
remaining body contains only metric labels, supported list separators and an
optional 指标 suffix. The helper identifies full prefixes at complete metric
boundaries, including `再加销售额和` before the last metric. It never strips a
substring from a remaining business noun or invents a catalog binding.

Typed filter values retain their role. Review also found that the first draft
could remove an admitted inherited entity literally named `再加`. A dedicated
counterexample failed, the guard was corrected to preserve admitted inherited
mentions, and the full Agent suite was rerun. This is an implementation review
correction, not another historical production bug. Previously persisted state
is not bulk migrated or guessed to be corrupt.

Positive tests cover addition/removal variants, lists, punctuation, suffixes,
successful and unavailable models, model role mistakes, actual HTTP payloads,
and add/remove followed by an August update. Contrasts preserve names such as
`再加`, `不要`, `再加医疗器械`, `不要忘记口罩`, named institutions and companies.
Unknown targets, dimension edits and compound business requests are outside
the new grammar. Scope/conversation mismatches cannot reuse the old task.

Prompt changes: **0**. Regex changes: **one bounded whole-command grammar plus
one escaped, longest-label span extraction**; existing whitespace normalization
and operation vocabulary are reused. Full-command anchoring, complete labels,
literal-name contrasts and inherited-entity protection bound the behavior.
This does not extend the Legacy metric mutation grammar, infer open-world roles,
or decide the semantics of unsupported compound commands.

The structured-model client/Prompt, admission gate, orchestrator, API handlers,
HTTP adapter and dependency injection are byte-identical to baseline. There are
no cross-service source edits, state-schema changes, identity changes, catalog
publication changes or production V2 imports.

## Verification

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,108 passed / 27 failed | **2,159 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160/160** |
| New metric edit grounding tests | Defects reproduced | **51/51** |

Focused combined suite: **402 passed**. The final full Agent run covers all
**92 test modules**, using the established 14 serial offline batches plus eight
newer contract modules. Classifier time is fixed to 2026-09-07; no dependencies
were installed. Oagnet's same-turn guarded suite is retained because its source
was unchanged during Agent review.

The first SQL suite reported **199 passed / 1 failed**: its existing ephemeral
loopback route test encountered Windows connection abort **10053**. The exact
node subsequently passed **three independent runs**, followed by **200/200**
in a fresh full SQL run. This is recorded as a flaky environment dependency;
the precise host cause remains UNKNOWN. No SQL code or expectation was changed.
The initial failure is retained in `test_delta.json`, not hidden by the final
aggregate.

Final old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old expectation changes: **0**. New tests: **51**.
All **79 observed public clarification responses** retained reason traces.
OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match baseline. Public response/SSE formats are unchanged.

Real model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Production V2 routing changed: **NO**.
Cross-service patches: **NO**. Deployment: **NO**. Automatic merge: **NO**.

## Release and remaining limits

`change_manifest.json` enumerates individually synchronized paths and source/
published SHA-256 hashes. Publication excludes environments, credentials,
runtime data, diagnostic logs, caches and backups. The original Oagnet index is
preserved. One independent implementation/evidence commit is pushed on the new
branch, with a Draft PR against PR #14. Rollback uses a reviewed revert of that
commit and explicit path synchronization; no production rollback is required.

The existing 27 Agent / 10 Oagnet failures retain their previous classifications.
No broad expectation changes disguise them. DM-A02 and broader role/compound
command behavior remain **UNKNOWN_NEEDS_EVIDENCE**. The existing configuration
hygiene P1 remains outside this change. This gate closes the two verified
metric-edit behaviors, not every Legacy P0 candidate.

Highest overall blocker: **BLOCKED_CATALOG_PUBLICATION** from PR #7. Governed
owned-domain projections/identifiers and an approved publication/deployment
target still need operational evidence. No production access or index-write
authority is inferred from this source task. Gold, evaluator, benchmark and
canary acceptance remain incomplete.

**V1 replacement is not ready.** Keep the existing routing. Once replacement
evidence passes, obtain user confirmation before switching.
