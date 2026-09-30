# JOIN connectivity and execution limits — 2026-10-01

## Findings

PROVEN: the reported final `JOIN department` repeats the preceding
hospital-to-bridge predicate. It does not reference the newly joined department
table. This can combine every matching hospital/bridge row with every department;
it is not evidence that a physical table is missing. The separately duplicated
SELECT in the pasted text is malformed SQL and is rejected as well.

PROVEN: forward and reverse relation builders could select the wrong subtable
mapping when both endpoints registered the same bridge. They did not check
which endpoint the original join key actually connected. Executable fixtures
reproduce the extra unrelated departments on the previous source.

PROVEN: the live SQL service log contains the disconnected department JOIN on
2026-09-30 (including 12:31:57 and 18:56:04). Read-only diagnostics found no such
active database query at inspection time. The database reports MySQL 5.7 and
`max_execution_time=0`; public execution previously omitted the optional timeout.
The exact duration and resource usage of yesterday's query remain UNKNOWN; no
claim that it ran for several hours is inferred from the log matches alone.

## Fix and review

- `sql_translator_prod.py`: choose the bridge direction from both endpoints in
  the declared relation, complete the other edge from existing catalog mappings,
  and skip disconnected candidate paths. Do not guess business relationships.
- `sql_join_safety.py`: check generated SQL and direct execution before database
  submission. Every JOIN must connect its new alias with an earlier alias in its
  own SELECT scope. Reject missing/forward/undefined references, duplicate aliases,
  constant-only or unrelated ON predicates, unconditioned table combinations,
  pasted SELECTs and overrides of server timeout protection. Preserve legitimate
  aliases, self joins, USING, parameterized values and independent query scopes.
- Default SELECT timeout is 60 seconds. Retain the existing explicitly requested
  internal range of 1–90,000 milliseconds. Require the database session timeout
  to be set successfully before submitting a query; support the MySQL and
  explicitly configured MariaDB setting names. Bound connect, network and metadata
  lock waits as well. Return a non-retryable timeout error and close the connection.
- Limit database execution to eight simultaneous queries per service process;
  release the slot on success, connection failure, timeout and cleanup paths.
  Saturation returns a busy response instead of starting additional queries.

These checks are structural, not a complete SQL parser or proof of correct
business meaning. A syntactically connected but semantically wrong relation still
requires correction in the catalog. Timeouts limit execution, not the total
request/export duration. The semaphore is per process, not a cluster-wide quota.

MySQL documents session SELECT limits and hint precedence in its
[optimizer-hint reference](https://dev.mysql.com/doc/refman/8.0/en/optimizer-hints.html).
MariaDB documents the different timeout setting in
[query limits and timeouts](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/query-limits-and-timeouts).
The installed database setting is also verified through a bounded live read.

## Test delta

- Original deployed-source SQL baseline: 546 passed.
- Candidate SQL full suite: 597 passed (546 existing + 51 new).
- New regressions on the previous translator: 34 failed, 17 passed; all 51 pass
  with the candidate. Includes four executable bridge direction/mapping cases,
  generic positive/negative JOIN shapes and execution-limit failure paths.
- DataAnalysis full suite: 4,529 passed (8 existing pytest warnings).
- Oagnet full suite: 1,224 passed.
- Remote candidate SQL suite: 597 passed, 3 subtests passed.
- No old-pass to new-fail, no collection errors and no relaxed old assertions.

## Change manifest and release boundary

Versioned files: this report, `sql_translator_prod.py`, `sql_join_safety.py`,
`test_join_safety.py`. Runtime manifest contains only the translator and the new
connectivity helper. No changes to API inputs, ASL parameters, semantic scope,
node/progress ordering, V1/V2 routing, semantic tables, vector indexes or configs.
Paused full-result-return edits in the canonical repository remain excluded.

Explicit file synchronization and hash comparison protect unrelated local edits.
Deployment requires matching remote baseline hashes, a rollback backup, passing
installed-source tests, protected configuration hashes and health verification.
Only the SQL service is restarted. No automatic PR merge is authorized.

Deployment/live checks are pending at this source commit and will be recorded
below after they complete. Current offline regression blockers: none found in
the tested paths. This bounded V1 fix makes no V2 cutover-readiness claim.
