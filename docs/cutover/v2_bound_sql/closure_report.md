# V2 bound SQL values

Baseline `e80132164b4335d38edd7081fd8d884551baa7fe` (Draft PR #34). Branch `cutover-v2-bound-sql-20260909-01`.
**Bound value transport: verified offline. Full V2 replacement: NOT_READY.**

PROVEN first divergence: SQL_TRANSLATOR. User values were rendered into SQL by
a quote-doubling helper and the business executor called cursor.execute(sql)
without parameters. PR34 refused mode-dependent values rather than guessing
their meaning. The installed PyMySQL 1.2.0 already supports connection-aware binding.

The private pinned consumer now separates compiler-owned placeholders from scalar
values. It escapes unrelated percent signs for DB-API formatting, validates each
unquoted named placeholder exactly once, and rejects template collisions, comments,
unproven template backslash rules and invalid/oversized values. Its receipt binds
the original ASL, SQL template, parameter values, count and style. Agent requires
that receipt in addition to its original full scope/catalog/output/policy checks.
Missing values, receipt tampering and downgrade to inline SQL are refused.

The existing executor accepts parameters only through optional Python keyword
arguments, requires the statement fingerprint, copies parameters before I/O and
passes them to the actual driver. Snapshot identity includes bound values and
bound-query errors omit them. Existing calls with parameters=None preserve their
behavior. Public HTTP/ASL/ChatRequest/SSE/result formats and V1 routing are unchanged.
The V2 pinned consumer still denies execution; this does not activate a V2 route.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| Agent | 2681 / 27 | 2697 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 319 / 0 | 354 / 0 |

51 new passing tests: 16 Agent, 35 SQL. Critical 160/160; trace 89/89;
old-pass to new-fail 0; collection errors 0. No old failing expectation changed.

SQLite in-memory execution verifies exact value filtering for quote, backslash,
NUL, Unicode, percent and SQL-like text. Installed PyMySQL mogrify is exercised
without connecting in both backslash modes; mocked executor tests verify copied
parameter forwarding, pre-connect fingerprint rejection, snapshot separation and
error privacy. These are not native MySQL execution, collation or performance proofs.

The same timestamped 81/[205] metadata corpus remains **55 supported / 61 plans**.
The six unsupported plans still concern two multi-subject metrics across three
families. This replay adds no business accuracy claim, fresh native observation or
real special-character source values. Catalog publication/Redis recovery is unchanged.

Prompt / new Regex patterns: 0. Public I/O / production V2 routing changed: NO.
Real model calls, external SQL, business reads and production writes: 0.
Original Oagnet source/index unchanged. Review was self-review only.

## Cutover readiness

Current Stage: bound SQL value transport and executor receipt verification.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: native publication, durable Redis recovery, deployed trust and
metric subject ownership remain unproven. Storage timezone still needs owner evidence.
Evaluation Gap: Gold/model benchmark gated. Shadow Gap: no actual plan-only shadow.
V1 Replacement Readiness: **NOT_READY**.
Next shortest blocking path: identify Redis deployment/durability and authorize
the reviewed 81/[205] native publication, then verify native read-back. Complete
remaining temporal/relationship/INCLUDE_TIES and real-result contracts without
changing public routing. Publication candidate remains the PR31 candidate.
