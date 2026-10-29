# Oagnet semantic scope publication contract

The business backend owns authentication and authorization. Oagnet enforces
the current positive semantic model and optional exact domain. It never adds
shared domain `-1` to an explicit query. Multiple explicit domains remain
unsupported. `AUTO` in the legacy response is only a presentation alias for
`MODEL_WIDE`.

`business_domain_id=-1` is a **GLOBAL_SHARED storage marker** for dimensions
and enumerations, not a grant to every business domain. Existing shared
`dimension` / `enum` records retain their model-wide behavior.

New publication retains governed entity/attribute IDs. For a dimension with
both an entity and its own attribute in the published domain, it emits a
separate `scoped_dimension` record and, if applicable, `scoped_enum` records.
Their IDs include the model/domain, and metadata carries
`scope_projection_version=dimension-domain-v1`. Bindings to another domain,
unknown IDs, conflicting ownership and ambiguous duplicate IDs are excluded.
Physical mappings and names are reconstructed from owned entity/attribute
rows rather than copied from denormalized dimension bindings. Unproven metric
bindings are omitted.

Explicit dimension/enum filters include the corresponding scoped record kind
and the exact current domain. Model-wide role queries continue selecting only
the original shared kind: projected copies cannot consume their top-k slots.
The two new record kinds belong to the existing semantic Milvus collection;
no new collection or schema column is introduced.

Opaque field mappings, special rules and hierarchical definitions currently
do not receive an explicit projection. Their ownership/field contracts must
be verified before support is extended. Missing ownership IDs never trigger
name matching, inference or relabeling of an old shared record.

Prompt vector results, deterministic completion results, and both exact and
approximate entity-value results must prove matching model/domain metadata.
This matters because Milvus filters scalar columns while returning a separate
metadata JSON payload. A mismatch is rejected before rendering or response
assembly. The Agent's own scope rejection remains unchanged.

Rebuild statistics expose `dimension_projection_candidates` and
`dimension_projections_published` per scope. Successful index transport alone
is not a semantic coverage PASS. Scoped refresh removes stale projections in
that domain while preserving other domains' projections. The existing shared
publication refresh remains a model-level maintenance effect, not a query
authorization rule.

Old snapshots need a controlled catalog republish. The code does not perform
one at import, startup or query time. A read-only local inspection is available:

```powershell
python scripts/audit_semantic_snapshot.py <local-chroma.sqlite3>
```

It emits only counts and a hash; it exports no record IDs, names, values or
conversation text. A local Chroma audit is not deployed Milvus/MySQL evidence.
Deployment, credentials and index publication require the existing controlled
operations process. Runtime models, prompts and V2 routing are unchanged.
