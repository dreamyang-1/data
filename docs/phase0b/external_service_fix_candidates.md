# External contract candidates — no cross-service patches

## EXT-01 / sql-translator / P0 capability blocker

Read-only source evidence: `E:/YouoAgent/sql-translator/api_server_prod.py:194-196` dispatches only `/v1/semantic/metrics/{metric_id}/lineage`; `_handle_metric_lineage` calls `catalog.lineage(metric_id, version)` at lines 264-270. `sql_translator_prod.py:495` defines metric-only lineage. DataAnalysis's existing SemanticAdapter likewise accepts a metric.
Expected contract: a scope/permission checked, versioned target of METRIC, FIELD, COLUMN, TABLE, ENTITY or DATASET resolves lineage. Actual: nonmetric routes and typed target contract are absent. Recommended external fix: agree and implement typed-target lineage in sql-translator and then align the adapter contract with contract tests. Do not encode a field as a fake metric.
Legacy now recognizes the target and reports the absent downstream capability without asking for a metric. This is not a claim that field lineage can execute today.

## CAT-01 / Semantic Catalog owner / P0 list capability blocker

The frozen `docs/phase2/semantic_catalog_inventory.json` and `docs/phase25/surface_collision_inventory.json` provide entities/attributes and collision evidence, but no governed `default_display_attributes` publication was established. Read-only Oagnet source search did not establish such a contract either.
Expected: entity-specific, immutable catalog version, allowed default projection. Actual: no evidenced policy to consume for the live hospital-list case. Recommended fix: publish governed defaults with role and permission scope; never hardcode hospital fields in Legacy. A controlled fixture proves that the new consumer uses only a matching entity/version and permitted attributes. Missing or invalid policies stay blocked.

Historical result projection/alignment failures only suggest Oagnet/SQL boundaries. They remain UNKNOWN until the original ASL, SQL and result contract evidence is recovered; they are not silently classified as proved external bugs.
Cross-service patches performed: **NO**.
