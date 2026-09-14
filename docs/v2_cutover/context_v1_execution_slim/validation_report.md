# V2 Context → V1 Execution Slim Bridge

**V2_CONTEXT_V1_EXECUTION_SLIM_OFFLINE_COMPLETE**

The live bridge now performs only context work: it restores stable V2
conversation state, reads the current authorized semantic catalog for this
request, runs V2 context recognition and TaskState updates, renders
`completed_question`, and calls the original V1 execution path once.

1. **Live Bridge responsibilities.** `context_v1_execution.py` owns V2 state,
   current-catalog binding revalidation, completed-question rendering,
   idempotent JSON/SSE response reuse, and the single handoff to V1.
2. **Removed live dependencies.** Frozen publication/manifest gates,
   process-wide Catalog authority drift and AUTO_REFRESH, pin-specific Redis
   identity, `RedisScalarSessionStore`, `DemoExecutionEnvelope`, Bridge scope
   materialization, Dataset/dependency restoration, demo SQL retry, prior-result
   fallback, and Limited Scalar execution are absent from the live module.
   The former implementation remains in `context_v1_execution_cutover.py` for
   benchmark/cutover use.
3. **PRE_RESOLVED V1 entry.** `DataAnalysisOrchestrator.
   execute_v1_from_completed_question()` deep-copies the original `ChatRequest`,
   clears only `history`, marks the internal pre-resolved mode, and invokes the
   existing `handle()` path. Intent, MultiQuestionPlanner, Canonical request,
   V1 Scope, Oagent, ASL, SQL, Dataset and response assembly are not copied.
4. **QuestionRewriter.** `rewrite(apply_previous_context=False)` keeps current
   typo, wording, time and entity/catalog normalization. It disables only
   `_apply_context()` and the history-only deterministic shortcut.
5. **Request fields.** The Bridge changes `question` to `completed_question`,
   sets `history=[]`, and sets one private context flag. It does not modify
   business-domain fields, resolved domains, database, knowledge bases,
   dataset, dependency constraints, tools or execution configuration.
6. **Stable state key.** The Redis key is
   `prefix:conversation:sha256(canonical_json({schema_namespace, tenant_id,
   user_id, application_id, conversation_id, semantic_model_id,
   requested_authorization_scope}))`. Requested authorization scope contains
   the transport model/domains/mode/database/knowledge-base values. Catalog,
   vector, publication and activation generations are absent. Catalog version
   is stored only as per-message request provenance.
7. **PURE V1 parity.** For the same standalone relation query, both paths
   reached the query adapter with identical semantic model, requested/resolved
   domains, selection mode, database, knowledge bases, source dataset, Intent,
   metrics, dimensions, filters, time, entity and fields. The only differences
   were generated request/thread IDs and internal provenance assumptions
   (`RAW_STANDALONE` versus `V2_COMPLETED_QUESTION`).
8. **Offline verification.** Targeted: 201 passed. Comparable affected set:
   591 passed plus one identical baseline failure. Full baseline/final:
   3666/106 versus 3686/106. All 3772 baseline nodes remain present;
   old-pass→new-fail is zero. The 106 failures are unchanged baseline failures.
9. **Live validation.** 8088 restarted as PID 35388 with
   `runtime_mode=V2_CONTEXT_V1_EXECUTION`; `/live` and `/ready` both returned
   HTTP 200 and readiness reported all dependencies healthy. A real platform
   request reached `/agent_chat/stream`, then stopped before CurrentTurn at the
   request-scoped Catalog load with `CATALOG_PHYSICAL_MODEL_MISMATCH`. No V2
   state was reserved and V1/Oagent/SQL were not reached, so the follow-up was
   deliberately not sent.
10. **Remaining pin/envelope/migration behavior.** The live Bridge contains no
    startup/frozen Pin gate, execution envelope, old-key migration or catalog
    lifecycle. The unchanged V2 recognition contract still records the current
    request catalog identity in its scoped artifact metadata; that label is
    request provenance and does not affect Redis identity or readiness.

No Oagnet, SQL Translator, Java/platform, database, V2 Recognition, Reducer or
TaskPatch source was changed. No model, Benchmark, business SQL or production
write was run during offline verification.

## Live first failure

The real platform request used the existing MODEL_WIDE contract: semantic model
81 with requested business domains empty. The thin Bridge preserved that input
and called the current Catalog reader with `(81, [])`. The authority resolved
the model's domain list to `[205]` for semantic documents, but its physical
capture called `get_table_field_by_scope(..., business_domain_id=None)`. That
returned 437 tables, including 419 platform-wide tables whose
`semantic_model_id` is null. `catalog_generation.build_catalog_records()`
correctly rejected the first such table because every captured physical table
must belong to semantic model 81.

A read-only comparison proved the boundary: `(81, [])` failed with 437 tables;
`(81, [205])` passed with 14 tables and 290 records. The authorized model domain
lookup itself returned `[205]`. This is not a conversation-key, Pin, migration,
Recognition, completed-question, V1 execution or Oagent failure. It is a
MODEL_WIDE current-Catalog physical-capture mismatch. Readiness currently
reports `v2_context_current_catalog_on_request=true` without executing a Catalog
capture, which explains why `/ready` remained READY while the request failed.

Per the freeze instruction, no code was changed after this finding. The first
demo group is `BLOCKED_BEFORE_V2_CONTEXT`; Catalog metadata mutation and the
second group were not started.

## MODEL_WIDE current-Catalog capture closure

The request-scoped Catalog reader now preserves the requested MODEL_WIDE
contract while resolving the current model's authorized domains solely for
physical Catalog capture and V2 binding validation. In the current environment,
semantic model 81 resolves to `[205]`; the original `ChatRequest` remains
`business_domain_ids=[]`, so V1 still receives and processes its established
MODEL_WIDE request contract.

The resolved domain set is also passed to `ScopedPlanSession` and
`RawTurnPlanner` so their per-request Catalog provenance matches the materialized
Catalog. It is not written into the conversation identity or V1 request. Models
with no valid domain, duplicate/invalid identifiers, or more than one domain
remain fail closed because the existing physical Catalog capture only supports
one resolved domain safely.

Verification after this minimal correction:

- Bridge test file: **22 passed / 0 failed**.
- Comparable affected set: **594 passed / 1 existing failed**.
- The single failure is the same baseline `slot_conflict` node with the same
  reason-code expectation difference; old-pass to new-fail remains **0**.
- A read-only capture against the current authority preserved requested domains
  `[]`, resolved `[205]`, and successfully built **290** Catalog records.
- Model, Benchmark, business SQL, production write, Oagent, SQL Translator and
  Java/platform changes remained **0**.

The earlier live failure above remains the historical first attempt. A new 8088
process must load this correction before real-platform retesting.

## Post-fix real-platform result

8088 restarted as PID 25288 in `V2_CONTEXT_V1_EXECUTION` mode. `/live` and
`/ready` returned HTTP 200, while the existing Oagent and SQL Translator
processes remained unchanged.

The real platform first turn, `查询去年江苏省订单笔数`, reached
`/agent_chat/stream` and completed through V1, Oagent and data source 58. The
MODEL_WIDE current-Catalog capture resolved `[205]`; the previous
`CATALOG_PHYSICAL_MODEL_MISMATCH` did not recur. The response was persisted as
`V1_EXECUTION_RESPONSE_SAVED` with `QUERY_RESULT` and Oagent ASL evidence.

This first turn took `V1_EXECUTION_FALLBACK_NEW_TASK`. Its safe context barrier
advanced state version to 1 but deliberately contained no Task. The real
same-conversation follow-up `换今年` read the same Redis state key, then returned
`V2_CONTEXT_UNRESOLVED`. It was persisted as a V2-only safe fallback;
`v1_execution_called=false`, with no Oagent or SQL evidence. Therefore:

- MODEL_WIDE Catalog capture correction: **PASS_LIVE**.
- Stable conversation identity across the two requests: **PASS_LIVE**.
- First-turn V1/Oagent/DB execution: **PASS_LIVE**.
- First-group context continuity: **FAIL_NO_PUBLISHED_V2_TASK**.
- First failure stage: **V2_CONTEXT**, before completed-question generation.

Per the instruction to stop architecture changes after the comparable suite and
avoid adding more fallback/anchor compatibility, no Bridge code was changed for
this result. Catalog metadata mutation and the second relation-query group were
not started because the first group did not pass.

## Context capability closure — 2026-09-14

The subsequent implementation keeps the slim architecture and closes the
observed failures by semantic class rather than by business literal:

| Capability class | Generic behavior |
|---|---|
| Complete standalone question | V2 first attempts normal Task publication; a complete unsupported NEW_TASK may still pass its unchanged text to V1. |
| Time and time-grain edit | Replace only the active task's time slot and preserve its metric, entity and filters. |
| Metric edit | Add, remove or replace a catalog-proven metric while preserving the active task scope. |
| Filter edit | Replace only a compatible semantic family; explicit cross-family replacement is rejected rather than silently becoming ADD. |
| Filter clear | Clear the named family, including nationwide/all-value language, without restoring an older value. |
| Named return | A named object plus an explicit metric replaces both slots only after unique current-catalog/V1 resolver proof. |
| Relationship pronoun | A singular pronoun resolves to one proven relationship actor; plural/result-set references remain tied to result evidence. |
| Result-set continuation | Ranking, drill-down, count and extreme-value continuations retain the immediate governed result context and fail closed when no unique antecedent exists. |
| New-topic isolation | A self-contained current question wins over older pronouns, values and failed tasks. |
| V1 parser compatibility | Demo mode may retry one simpler real V1 question for universal entity scope or a redundant generic type suffix; it never invents a result and never removes a concrete entity value. |

The bridge still delegates authentication, MODEL_WIDE scope, retrieval,
Canonical construction, Intent, Oagent, ASL, SQL, database access, Dataset and
response assembly to the original V1 path. It does not import or invoke
`context_v1_execution_cutover.py`. Catalog Pin, AUTO_REFRESH, catalog
migration/reseal, `DemoExecutionEnvelope`, execution-scope reconstruction and
prior-result fallback remain outside the live bridge.

Final verification on the accumulated candidate:

- Focused and affected tests: **347 passed / 0 failed**.
- Full offline suite: **3774 passed / 91 existing failed / 0 collection errors**.
- Compared with the latest pre-closure full candidate: **old-pass -> new-fail = 0** and **old-fail -> new-pass = 15**.
- The candidate contains 45 new nodes. Three older nodes were intentionally
  replaced by tests for the approved slim contracts: MODEL_WIDE is forwarded
  to V1/Oagent instead of being rejected by the Bridge, and failed direct
  completion preserves current semantic context rather than reviving an older
  task.
- Python AST and `compileall` checks passed. `git diff --check` passed; Ruff was
  unavailable in the installed environment.

This evidence covers context completion, state continuity and the V1 handoff.
It does not claim semantic answer accuracy for every downstream query shape.

## Final runtime load

The 16 explicit source, test and report files were synchronized from the Git
worktree to the development runtime directory and verified byte-for-byte with
SHA-256. Only DataAnalysis Agent was restarted: 8088 moved from PID 6992 to PID
26356. `/live` returned HTTP 200 and `/ready` returned HTTP 200 / `READY` with
`runtime_mode=V2_CONTEXT_V1_EXECUTION`. The unrelated loopback 8088 listener
and the Java/platform, Oagent and SQL Translator services were not restarted.

The fixed two-minute platform input stream was paused before this final load,
so no new live semantic result is claimed in this closing section.
