# V2 entity-value source contract

Baseline: `0bb3f92327e69372e8450c7f49c2a8e2c0871630` (Draft PR #27).
Branch: `cutover-v2-entity-value-sources-20260908-01`. Date: 2026-09-08.

**Source contract acceptance: PASS. V1 replacement readiness: NOT_READY.**

The first divergence is catalog source capture. Existing catalog documents
retained attribute names and physical field strings, but omitted enough of the
entity's data-source ownership and connection configuration that a routing
change could be invisible to the catalog version. Legacy entity-value vectors
also lack membership in the pinned catalog generation. Treating these vectors
as published canonical values would overstate both provenance and freshness.

This change belongs to Oagnet. No Agent classification workaround is added.
The public DSL, HTTP requests/responses, SSE, V1 behavior and model defaults
retain their existing formats and routes. The private catalog capture now
includes `entity_value_sources` inside its physical metadata document. Its
active model/domain/entity/attribute/data-source/table/field joins establish the
exact source mapping. Entity and attribute VARCHAR keys use the existing
`normalize_governed_id` contract, including UUIDs. Only the metadata port is
normalized from its actual VARCHAR representation; request model/domain IDs
remain strict positive integers.

The capture reads metadata in the existing consistent MySQL transaction, now
also requiring the data-source metadata table to be transactional. A route
contains model ID, data-source ID, driver and a locator digest. Captured source
facts include the governed vectorization policy. No credentials, raw host or
database locators, or business values enter the snapshot or committed report.
Changes to routing, ownership, mapping or policy change catalog identity;
credential rotation alone does not. Missing or ambiguous mappings do not
authorize a source query. The original public DSL loader is unchanged.

`PinnedCatalog.entity_value_source` proves one actual publication record's
attribute, canonical owner and source mapping. The opt-in internal
`lookup_entity_values` then re-reads current joined metadata and verifies that
the source still matches. It issues a parameterized exact SELECT in a read-only
transaction against that registered source, using validated governed identifiers.
The transaction rolls back and closes even on failure. Errors contain bounded
codes. No SQL, scope, source or connection can be supplied by a model tool.

Returned canonical strings are checked against the exact normalized query.
Binary matching preserves distinct source strings that share a normalized
surface, rather than hiding an ambiguity through database collation. A bounded
query reads one extra value and exposes `complete=false` when truncated. It
does not assert that a partial list is the complete candidate universe. The
observation contains source evidence and the current pin, not a declaration
that mutable business values were published in the static semantic catalog.
`finish` repeats observations and rejects changed/deleted values, changed
source mappings or changed publication activation before accepting the pin.
Caller mutation of a returned receipt cannot alter that acceptance evidence.

## Actual scope evidence

The user selected **81 / [205]** for read-only validation. The fresh capture at
`2026-09-08T15:32:18.587418+00:00` verified all **89 attributes** against active
governed source mappings, with one distinct configured locator. It generated
the same **290 supported static records**. Vectorization metadata contains 26
enabled, 52 disabled and 11 unspecified attributes; display-main flags do not
grant vector search eligibility.

Catalog version:
`b3106dea38f62738f5f2551e4667ceee6a0d3ba96f5e88ddd90e7ef746b88506`.
See `catalog_source_evidence.json`. The private snapshot is uncommitted.
Actual business-source reads: **0**. Native publication: **not performed**.
Model calls, embedding calls and production external writes: **0**.

## Validation and review

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2573 / 27 | 2573 / 27 |
| Oagnet | 560 / 8 | 625 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

There are **65 new passing tests**. The final catalog source/publication/release
suite passed **202/202**. Complete three-service regression has zero final
collection errors and zero old-pass to new-fail nodes. Critical: **160/160**.
Public clarification trace: **89/89**. Prior business assertions are unchanged.
Two existing metadata fixtures now distinguish the new source-definition query
from the server-identity query, without changing their expectations.

The initial SQL full run had one loopback `WinError 10053` in
`test_route_matching_is_exact`. Two independent reruns and another complete
200-test SQL run passed with no source changes. The precise host/transport
cause remains unknown; the transient is retained in `test_delta.json` rather
than erased or used to justify changing assertions. An intermediate Oagnet
command supplied flags without its default test directory and collected a
backup module. The corrected explicit `tests` invocation collected normally;
no backup or cache was deleted. Final totals use complete runs, not sums of
repeated tests.

Self-review also corrected a new fixture that used canonical logical IDs in
place of actual generation record IDs. Actual metadata inspection exposed the
UUID and VARCHAR-port storage types; the new contrasts cover them. Coverage
now distinguishes captured route configuration from runtime server identity.
Prompt changes: **0**. Regex changes: **0**. No independent reviewer is claimed.

## Remaining cutover gates

This is the source-contract prerequisite for C-04/V-01, not an end-to-end
entity-value or Catalog PASS. Raw V2 must still consume these observations as
typed field/value evidence, enforce owner compatibility in predicates and
relationship occurrences, handle true ambiguity, and revalidate on Pending,
task or cache restoration. A prior receipt's digest is not authority to reuse
a value. Approximate or unqualified search needs governed eligible fields and
complete candidate evidence; legacy vector hits can only suggest candidates.

The Agent's platform `database_id` must go through the existing database-load
mapping before constraining Oagnet `data_source_id`. These IDs are not assumed
equal. Knowledge-base/state context remains the Agent's current-request
responsibility. No source-stage test claims these unfinished integrations pass.

A locator hash proves configured routing, not DNS resolution, server UUID,
an execution snapshot or an atomic observation across multiple business sources.
The two read observations do not freeze mutable business data for later SQL.
Runtime source and downstream ASL/SQL binding evidence remain required.

The isolated native publication still requires the pending operational approval.
Its old candidate must be regenerated against this new catalog version before
any approved publication. Gold/model evaluation follows the catalog gates;
actual plan-only shadow and rollback evidence follow evaluation.

Current Stage: V2 entity-value source contract.
Cutover Blocker P0: **8 open**. Cutover Blocker P1: **4 open**.
Catalog Blocker: actual source mappings verified; native publication and dynamic
value integration remain open.
Evaluation Gap: formal V1/V2 Gold and real-model comparison remain open.
Shadow Gap: actual plan-only deployment and collected differences remain open.
V1 Replacement Readiness: **NOT_READY**.
Next shortest blocking path: wire the verified observation into scoped Raw V2
filters and restored state, then validate authorized native publication and
actual source retrieval. No V1 replacement, merge or deployment was performed.
