# Verified P0 closure and evidence limits

Failure universe: 81 observations (32 current failures, 30 historical partial traces, 15 critical scenarios, 4 closure audit findings). Three critical cases passed at baseline and are controls. Deduplication yields 35 failure signatures, not 62 independently assumed bugs. Counts in closure_counts.json are signature counts except explicitly labeled observations/nodeids.

Verified REAL_PRODUCTION_BUG signatures: 17; P0 fixed: 16; remaining: 1 (fixed trusted identity, named security owner blocker). The external typed-lineage and catalog display gaps are separate P0 capability blockers. Historical 30 observations are P0 candidates with UNKNOWN root cause and missing replay artifacts; symptom deduplication does not declare them repaired by similar regression fixtures.

First divergences and minimal repairs:

- RULE_PARSE: pure temporal/grouping syntax and region relationship scope became product filters; explicit field projection disappeared; nonmetric lineage targets were forced through metric readiness. Preserve literal role and intent-specific target; no model prompt expansion.
- TURN_ADMISSION: missing execution slots polluted turn relation; correction and result-sort evidence failed to preserve the right context. Only reference/dependency evidence decides inheritance; new full requests retain their own frame.
- PENDING_ADMISSION: pending presence captured unrelated inputs; admitted candidate choices could still be replaced later. Complete new requests win, expected answer/candidate guards bind only compatible replies, and opaque candidate numbers follow existing option resolution.
- SLOT_MERGE: ADD/REMOVE and regional CLEAR lost values or allowed later inheritance. Keep the existing reducer, fix set mutation and persist a bounded region inheritance barrier. Audit operations now retain ADD/REMOVE/CLEAR semantics.
- DATASET_FOLLOWUP: display LIMIT and explicit ranking count were conflated; completeness was unavailable in persisted provenance. Preserve display order, distinguish local and global operations, retain source_truncated, and fail closed/requery when a global operation lacks a complete source. Projection and explicitly local sorting remain reusable.
- INTENT_ASL_CONTRACT / PENDING_ADMISSION: clarification had no structured reason and could repeat or delegate backend failure to users. Gate and trace cover ordinary, composite and cached final paths. No raw business text is added to long-term traces.

Current business evidence outranks old single test expectations. Four provisional stale classifications were explicitly reverted to UNKNOWN on insufficient evidence. No speculative parser repair is applied to ambiguous forecast/report period policy or PARTIAL_SUCCESS expectations.

Prompt modifications: **0**. Regex changes: **4 bounded families / 5 sites**: additive optional 上 (classifier + admission); anchored display-limit syntax; numeric extreme ranking count; pure time/grouping subject filter. See regex_change_register.md for positive/negative contrasts. Bare name and clear-region guards use finite existing vocabulary/literals, not additional regex.
Cross-service patches: **NO**. Production V2 routing changes: **NO**. Security refactor: **NO**. Full event store, new CAS design, long-term memory and V2 integration remain outside scope.
