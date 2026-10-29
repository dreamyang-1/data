# Controlled catalog publication plan — pending operations authorization

This plan is reviewable; it has **not** been executed. It requires an explicitly
approved environment, semantic model and business domain, and existing trusted
service/gateway access. Credentials must stay in the existing private runtime
configuration. The Agent does not calculate authorization.

1. **Read-only preflight.** Inspect the target's current deployed revision and
   metadata schema. Read the approved model/domain's entity and attribute IDs,
   dimension binding JSON and referenced mappings through the existing catalog
   connection. Keep record-level evidence in an ignored/private location. Report
   only counts/hashes publicly. The local SQLite auditor is a historical snapshot
   check, not a substitute for current MySQL/Milvus evidence.
2. **Projection review.** Use the committed pure projector on that governed
   catalog snapshot, with no embedding/model/index call. Require ownership of
   both entity and attribute; show candidates, eligible projections and rejected
   definitions. Verify special rules/hierarchies against owned fields before
   extending support. Missing IDs or unresolved definitions keep the gate blocked.
3. **Controlled publication.** Deploy the reviewed Oagnet revision through the
   existing release process. Only after separate authorization, invoke the
   existing `/vector/rebuild` for the approved model/domain. This endpoint also
   refreshes that model's shared dimension definitions; that maintenance effect
   must be included in the operations approval. Its generation-before-upsert and
   stale-ID cleanup are retained. No new rebuild is run during ordinary queries.
4. **Post-publication acceptance.** Confirm current backend versions and trusted
   caller boundary; inspect exact-domain projection counts and record provenance.
   Run the same scope matrix using a controlled catalog/mock model transport,
   including model-wide contrast, foreign-model/domain/shared rejection, owned
   dimension ASL, state/cache isolation and Agent critical multi-turn checks.
   Transport success or a larger record count alone is not PASS.

Before operations, retain the existing snapshot/version and review a rollback
through the supported publication process. Do not delete, mirror or overwrite
the vector store directory. No production query result rows need to be exported.

Once every Phase 0C-1.5 gate is evidenced, automatically continue to Phase 0C-2
Gold/Evaluator/Benchmark. Keep production models and V2 routing unchanged.
