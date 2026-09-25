# V2 ranking contract: bounded TopN lowering

Baseline `b6ca3db0d8602f9f7457a64092d60c449a1c725d` (Draft PR #32). Branch `cutover-v2-ranking-contract-20260909-01`.
**EXCLUDE_TIES source implementation and offline acceptance: PASS. Full V2 replacement: NOT_READY.**

First divergence is PROVEN at V2_BACKEND_LOWERING / SQL_TRANSLATOR: the actual
ASL 2.0 single-sort object does not carry the existing RankingSpec null policies
or ordered tiebreakers. Serializing only its primary key would lose semantics.

The internal lowerer now passes a separate immutable `pinned-ranking-v1` Python
argument to the pinned SQL consumer. It contains output aliases, semantic
fingerprint, ordered direction/null policies, EXCLUDE_TIES and limit. Public
ChatRequest, HTTP ASL, SSE and result formats are unchanged. The ASL object alone
does not represent the complete ranking contract and must not bypass this seam.

The SQL consumer validates exact aliases and primary sort/limit, emits explicit
null ordering and aggregate-level null exclusion, and returns a contract hash
only after original full scope/catalog acceptance. Agent refuses absent or
changed receipts. Global/user filters still precede aggregate ranking. An extra
group key is rejected because it would change the ranked population. Existing
RankingSpec and ResultContract definitions are unchanged, including the default
EXCLUDE policy for explicit secondary ordering requirements.

INCLUDE_TIES remains unsupported with a specific code. The downstream read-only
execution guard rejects general subqueries and the current ASL lacks a ties
contract. This delivery neither weakens that guard nor claims complete ranking.
Time, relationship, Boolean, source-subject and actual execution gaps remain.

## Validation

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| Agent | 2645 / 27 | 2661 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 261 / 0 | 282 / 0 |

37 new passing tests (16 Agent, 21 SQL). Critical 160/160, trace 89/89,
old-pass to new-fail 0, collection errors 0. Existing failure expectations remain
unchanged. The prior positive EXCLUDE_TIES test now asserts the new supported
behavior; its INCLUDE_TIES contrast still requires refusal.

Generated SELECTs were executed against synthetic sqlite3 :memory: tables to
verify six direction/null combinations, aggregate-before-limit, null groups,
stable equal-value ordering, reversed input, global/user filters, and ResultContract
failures for reversed/truncated/excess output. This does not prove deployed MySQL
collation, runtime data or performance. No real business database was queried.

The prior timestamped 81/[205] capture is explicitly reused. No fresh native
observation is claimed. Typed-plan/SQL replay is **41 supported / 47 plans**;
new ranking coverage is **9/11**. The six unsupported plans correspond to the
same two multi-subject metric definitions across three query families. They
remain governance gaps, not six independent bugs or a recognition accuracy score.

Public I/O changed: NO. Production V2 routing changed: NO. Real model calls,
external SQL executions, business reads and production writes: 0. Prompt and
new Regex patterns: 0. Original Oagnet source/index remains untouched. Review
was self-review, not independent review. Runtime mode stays SHADOW_ONLY.

## Cutover readiness

Current Stage: bounded V2 ranking/SQL contract and offline result verification.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: native publication, Redis durability/recovery and deployed trust
remain unproven. Primary subject and source timezone need authoritative evidence.
Evaluation Gap: Gold/model benchmark gates remain open.
Shadow Gap: no actual plan-only shadow evidence.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: obtain Redis deployment/durability evidence and
authorization for the reviewed 81/[205] native publication, then native read-back;
continue only actual backend semantic gaps while those operational prerequisites
remain pending. Candidate remains `../pinned_sql_catalog/publication_candidate.md`.
