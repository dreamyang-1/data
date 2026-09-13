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
