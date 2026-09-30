# JOIN assembly regression closure — 2026-09-30

## Root cause and scope

PROVEN: the deployed translation API reproduced two identical, unaliased
`sales_order` joins for a dealer order-count query with an explicit order-date
range. The time-context phase appended a relation without registering its tables;
the later metric-formula phase added it again. The existing relation parser also
used `[^J]` to split ON conditions, losing path segments containing `j/J`, and
did not register joins accepted earlier in the same path batch.

This is a SQL compiler assembly defect, not evidence of duplicate semantic
relationships or bad vector data. The original historical database error was not
recovered from service logs; the duplicate SQL was independently reproduced
through the running API and an executable offline fixture.

## Changes and review

- `sql_translator_prod.py`: time joins register every path table; all root-query
  contributors (dimensions, filters, HAVING, time, fixed metric filters, formulas
  and declared dependencies) use one query-local assembly registry.
- `join_assembly.py`: split compiler-generated top-level JOINs without splitting
  quoted text or nested subqueries. Reuse identical alias/relationship definitions,
  retain distinct aliases/self-join roles, and report conflicting definitions of
  one alias before database execution. Preserve string/table case and predicates.
- `test_join_assembly.py`: 34 regressions covering contributors, shared paths,
  same-batch duplication, SQL spelling, aliases, conflicts and request isolation.
  The reported time/metric query is executed against an in-memory database.
- Preserved the deployed fixed-filter anchor enhancement found during drift
  inspection; a regression protects its preference for the business fact path.
  Existing scoped/pinned consumers and related-filter helpers share the corrected
  utilities. Related-filter subquery aliases remain independent.

No ASL parameters, authorization scope, API/SSE format, user-visible nodes,
semantic configuration, vector indexes or query-result limits were changed.
Unrelated paused full-result-return edits are excluded from this release.

## Test delta

- Local deployed-source baseline, original SQL tests: 512 passed.
- Local candidate SQL full suite: 546 passed (512 existing + 34 new).
- DataAnalysis full suite: 4,529 passed; Oagnet full suite: 1,224 passed.
- Remote candidate SQL full suite: 546 passed, 3 subtests passed.
- The same remote harness against the previous deployed translator: 531 passed,
  15 failed, all 15 in the new JOIN regression module. All 15 pass with the fix.
- No old-pass to new-fail, no collection errors, no old assertions relaxed.
- Remote staging initially lacked the sibling publication test and the optional
  Chroma import. The harness now imports the real deployed Oagnet modules and
  the publication memory-store fixture. A stage-only Chroma import stub raises
  if a client is constructed; no business implementation is mocked by that stub.
  Network is disabled by the existing offline runner. These were staging
  dependencies, not production changes or ignored failures.

## Release boundary

Runtime manifest: only `sql_translator_prod.py` and new `join_assembly.py`.
Tests and this report are versioned, not copied into production runtime.
Development and canonical Git source hashes are compared file by file.
The deploy script requires a clean remote hash comparison, backups, passing
installed-source tests, configuration preservation, SQL service restart and
health verification; it restores the previous files on deployment failure.
No V2 cutover, database writes, index rebuild or PR merge is included.

Deployment and post-restart read-only query evidence will be recorded below.
