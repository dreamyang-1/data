# V2 declared paths and entity occurrence closure

Baseline: `b5037e7d15ecfd8e733551c853cbf9441be7c4bd` (Draft PR #25).
Branch: `cutover-v2-relation-paths-20260908-01`. Date: 2026-09-08.

**Source acceptance PASS; V1 replacement readiness NOT_READY.**

## Contract and implementation

The prior flat RelationshipSpec could not describe an intermediate entity or
distinguish multiple occurrences of the same entity. This is a proven V2
contract gap. Scoped 0.2.2 now accepts an ordered RelationshipPathSpec of one to
eight declared hops and two to nine occurrence nodes. Each hop retains its
direction and declared cardinality; the contract does not invent a single
composed cardinality for the path. Current pinned catalog records supply the
endpoints and join evidence. The compiler reconstructs the path and rejects
disconnected, relabeled, forged or inconsistent claims.

Code derives occurrence aliases from the current catalog identity, root entity
and directed path prefix. Extending a path retains prior occurrence meaning;
choosing a different route does not silently rebind an inherited alias. Repeated
entities and return paths remain distinct occurrences. Field owners are checked
by entity code and domain against the selected occurrence. An unaliased
attribute is accepted only when exactly one path node owns it. Ambiguous
repeated ownership is a bounded unresolved result, never arbitrary selection.
Physical/dimension/computed ownership without an explicit proof remains unsupported.

The model supplies offered relation handles and directions, plus node:N field
selectors where needed. It cannot supply canonical endpoints, cardinality,
joins or authoritative aliases. Hydration constructs this turn's effective path
before resolving field selectors. Scoped prior paths are available for actual
follow-ups; new requests inherit nothing. Historical model context exposes path
labels and occurrence positions from already restored state.

Aliased predicate/projection/result variants preserve occurrence identity
through reduction, nested Boolean serialization, semantic fingerprints, default
display and the ResultContract. Membership ADD/REPLACE/REMOVE/CLEAR retain the
targeted predicate class and occurrence; subsequent turns do not revive cleared
conditions. Default display refers to the terminal occurrence. Conflicting
output IDs cannot hide that target. A flat self relationship is rejected because
its old representation cannot distinguish direction/occurrences.

Old flat non-self plans retain their serialized shape. Frozen 0.2.1 files remain
unchanged and the final legacy guard rejects path/alias extensions. Current
0.2.2 and model edit/value schemas are checked against their source. Public HTTP,
SSE, ChatRequest and response formats, V1 routing and model defaults are unchanged.
The existing backend remains SHADOW_ONLY; this work does not execute ASL or SQL.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2484 / 27 | 2523 / 27 |
| Oagnet | 560 / 8 | 560 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

Full Agent regression covered 100 modules and returned 2522/27. A test-only
review then strengthened scope tests to reach the actual restore boundary and
added schema equality. Its complete module passed 39/39; replacing that module's
results yields 2523/27 unique current nodes. Before that review, 504 affected V2
tests passed. Critical Suite: 160/160; public clarification coverage: 89/89.
Old-pass to new-fail, missing prior nodes and collection errors: zero. Existing
expectations were not changed. Full Oagnet and SQL suites ran offline unchanged.

The new tests cover directed paths, intermediate/source/target attributes,
self/repeated entities, default/result aliases, nested serialization, stale or
wrong alias rejection, field ownership, scope restore, path append/replacement,
filter edits across three turns, output collisions and schema/legacy boundaries.
Self-review caught subclass serialization dropping aliases, corrected predicate
subclass preservation and tightened output collision and scope-test evidence.
No separate reviewer or real-model evaluation is claimed.

A fresh user-selected 81/[205] read-only capture retained catalog version
`c054d927062b4728fe40d28efa87bd45336f230f62282c257b4c13233a664d92`:
14 entities, 24 relations, 290 generated records. With offline publication
doubles, every one of the 250 directed two-hop walks compiled successfully,
including 76 repeated-entity walks. These are typed path acceptance checks,
not natural-language accuracy, business Gold or actual native publication.
Raw directory contents stay outside Git. Real model/embedding calls, native
index writes, Redis activation writes and external production writes: zero.

## Prompt and limitations

V2 draft prompt version: v2-current-recognition-v5. The change describes the new
ordered-hop and occurrence-selector schema. Code supplies and verifies all
catalog facts; choosing what natural-language path the user meant still requires
later evaluation. Positive, reverse, self, disconnected, wrong-owner and stale
occurrence contrasts are recorded in the new module. Regex changes: zero.

Current Stage: catalog/state/semantic source readiness before Gold. Cutover
blockers remain P0=8, P1=4. Native isolated catalog publication remains absent
and awaits the already requested operational authorization; its old candidate
must be prepared again against current source/target before any approved write.
Source work does not close deployed catalog trust or cache verification.

Next shortest source path: dependent temporal comparisons, external entity
values and remaining analytical/decision contracts, plus explicit downstream
ASL/SQL alias binding. Gold/evaluation and real-model comparisons remain gated
on catalog/state closure; actual plan-only shadow and rollback evidence follow.
No V1 replacement, traffic switch, merge or deployment occurred. Scope 81/205 is
the selected validation scope, not an application default or permission grant.
