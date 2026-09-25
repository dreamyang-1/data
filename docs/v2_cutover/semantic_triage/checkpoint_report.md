# SEMANTIC CUTOVER TRIAGE CHECKPOINT

**SEMANTIC_TRIAGE_COMPLETE**

Audited runtime commit: `0e408509126aae0a3a324aff02aef848fd0d3c56`.
Candidate Probe closure: [Draft PR #51](https://github.com/dreamyang-1/data/pull/51).
Checkpoint branch: `semantic-cutover-triage-20260909t074900z`.
V1 Production Routing remains unchanged. No production source, Gold labels,
prompt, schema, candidate policy or acceptance threshold changed in this checkpoint.

## How many root causes are actually supported by evidence?

There are **11 observed root-cause groups** behind **41 nonpassing records**:
8 semantic groups affect 31 records, and 3 evaluator/fixture groups affect 10 records.
The remaining 79 records pass their currently labeled component axes. These are
records from two corpora, not 120 independent bugs.

This is an evidence-backed lower bound, **not proof that only 11 code fixes remain**.
Full binding/IR/dry-plan/Oracle/safety coverage is incomplete and may expose more
causes. Root groups are behavioral contracts, not individual model mistakes or
counts of error strings. Pending and Dataset have two fixture variants but share
one explicit missing-state-entry guard; they are one root group, not two bugs.

All 120 records have one inventory row. Every FAIL/NOT_RUN has exactly one primary
first divergence and one root group. PASS rows have no fabricated divergence.
Later rejections and blocked target turns appear only in `downstream_effect`.
Root-fix coverage estimates count primary cases whose first barrier could be
removed; they do not promise that later stages or whole cases will pass.

## First Divergence Distribution

| Stage | Cases | Case IDs | Severity | Cutover blocker | One root-fix primary coverage |
| --- | ---: | --- | --- | --- | --- |
| MENTION_BOUNDARY | 10 | G81-002, G81-004, G81-005, G81-006, G81-013, G81-029, G81-030, G81-033, G81-034, G81-094 | P0/P1 | YES | RC-BOUNDARY-OBJECT:6; RC-BOUNDARY-METRIC:4 |
| ENTITY_VALUE_GROUNDING | 5 | S81-004, S81-005, S81-009, S81-010, S81-015 | P1 | YES | RC-VALUE-CANDIDATES:5 |
| TASK_OPERATION | 6 | G81-080, S81-006, S81-007, S81-008, S81-012, S81-017 | P0/P1 | YES | RC-OP-ATOMIC:2; RC-EDIT-COVERAGE:3; RC-REFERENCE-AS-EDIT:1 |
| QUERY_SHAPE | 10 | G81-001, G81-003, G81-007, G81-008, G81-009, G81-010, G81-011, G81-012, G81-014, G81-093 | P1 | YES | RC-SHAPE-LIST:9; RC-SHAPE-LINEAGE:1 |
| EVALUATOR_GAP | 6 | G81-071, G81-073, G81-082, G81-084, G81-090, G81-091 | P2 | YES | RC-EVAL-TURN-PROXY:5; RC-EVAL-DATASET-PROXY:1 |
| FIXTURE_GAP | 4 | S81-016, S81-018, S81-019, S81-020 | P2 | YES | RC-FIXTURE-STATE-ENTRY:4 |

The other 11 requested stages have 0 primary cases in this observed inventory:
TURN_RESOLUTION, MENTION_EXTRACTION, SEMANTIC_ROLE, CANDIDATE_RETRIEVAL,
CANONICAL_BINDING, TIME_NORMALIZATION, TASK_REDUCER, SEMANTIC_QUERY_IR,
DRY_PLAN, CATALOG_GAP and EXTERNAL_BLOCKER. **Zero primary cases is not PASS**:
several of these stages are not exercised or lack independent labels. Candidate
ranking is unmeasured, and the native dry-plan seam is not part of this runner.

Example of causal ordering: S81-017 correctly selects the historical task, but
turns the metric naming that task into a REPLACE edit. In this particular capture
that replacement is idempotent. A later wrong grouped shape causes the actual
validation failure. The early reference/edit confusion is the single primary
cause; grouped shape is a downstream effect. Potential silent deletion on a
multi-metric target is HIGH_CONFIDENCE risk, not an observed production result.

S81-015 never reaches its lineage question because its history fails source-value
grounding. It is counted under value grounding, not as another lineage defect.
S81-004/005/009/010/015 select the city field; the current contract does not prove
province was the only valid business interpretation. The proven barrier is an
unavailable bounded alias candidate path, not an invented “correct province” rule.

## Recomputed Gold status

| Corpus | PASS | FAIL | NOT_RUN | BLOCKED | Executable for existing listed axes |
| --- | ---: | ---: | ---: | ---: | ---: |
|100 parser-axis Gold|73|27|0|0|100|
|20 transition Gold|6|10|4|0|16|
|Total|79|37|4|0|116/120|

PASS means all **currently supplied labeled axes** pass. It does not mean Offline
Semantic Ready. The 100-case corpus is parser-axis evidence, and 42 of its records
contain history that the parser benchmark does not execute. Its prompt/schema
match the audited HEAD; scores were recomputed locally with the current evaluator,
without new model calls or changed labels. The 20-case evidence is the latest PR51
run using the audited code and frozen source observations.

No current record is BLOCKED by Redis persistence, native publication or a missing
API key. The four NOT_RUN records are S81-016(Pending) and S81-018/019/020(Dataset).
Their fixture/entry gap is an engineering obligation for the next stage, not a
reason to leave Critical Gold untested indefinitely.

Fully labeled, end-to-end canonical/TaskPatch/IR/dry-plan semantic Gold acceptance
is **not established**; 0 records are certified complete at that boundary. This does
not erase the 116 executable component records or the narrower passing controls.

## Replay, Oracle and candidate evidence

The available V2 reports contain 94 replay/control receipt rows referencing 77
distinct full-turn source capture hashes, including 10 parser-representation controls
within those 94 rows. Repeated receipts and output interventions are not additional
Gold cases. All 26 latest PR51 turns reproduced exactly, including 10 safe failures.
Older receipts remain historical evidence and are not relabeled as a fresh HEAD
run. The inventory lists every source/hash and the embedded three-turn targeted
compound-metric replay.

The older output Oracles change selected recorded fields while keeping later
recorded model outputs; they do not prove regenerated-model accuracy. The three
province-field Oracle probe cases did regenerate the value-choice model stage
and pass binding/finish, but prove neither field selection nor whole plans.
Full Oracle Mention/Role/Candidate/Relation/IR acceptance remains incomplete.

`_candidates` enumerates pinned role-compatible records; the isolated catalog has
constant 2D vectors. Real ranked retrieval and Recall@k are NOT_EVALUATED. Per-turn
candidate context hashes are retained; they are not a frozen ranked corpus for
all 120 cases. The latest 20-case run invoked 0 value-choice model stages because
earlier guards/cardinality failed. A selected-member or constant plan score is
not candidate retrieval quality or actual dry-plan evidence.

## Root-cause priority and the next three

P0 below denotes potential silent wrong-query/result risk; this checkpoint did
not execute production SQL and does not claim observed production corruption.
P1 is incorrect clarification/inheritance or inability to execute. P2 is an
evaluation/diagnostic gap; a P2 infrastructure issue can still block the semantic
freeze gate. Business severity and acceptance-blocker status are separate fields.

| Root cause | Severity | Primary cases | Type | Confidence |
| --- | --- | ---: | --- | --- |
| RC-BOUNDARY-METRIC | P0 | 4 | SEMANTIC | PROVEN |
| RC-OP-ATOMIC | P0 | 2 | SEMANTIC | PROVEN |
| RC-REFERENCE-AS-EDIT | P0 | 1 | SEMANTIC | HIGH_CONFIDENCE |
| RC-SHAPE-LIST | P1 | 9 | SEMANTIC | PROVEN |
| RC-BOUNDARY-OBJECT | P1 | 6 | SEMANTIC | PROVEN |
| RC-VALUE-CANDIDATES | P1 | 5 | SEMANTIC | PROVEN |
| RC-EDIT-COVERAGE | P1 | 3 | SEMANTIC | PROVEN |
| RC-SHAPE-LINEAGE | P1 | 1 | SEMANTIC | PROVEN |
| RC-EVAL-TURN-PROXY | P2 | 5 | EVALUATION | PROVEN |
| RC-FIXTURE-STATE-ENTRY | P2 | 4 | EVALUATION | PROVEN |
| RC-EVAL-DATASET-PROXY | P2 | 1 | EVALUATION | PROVEN |

**TOP 3 NEXT ROOT CAUSES**

1. **RC-EVAL-TURN-PROXY** — 5 primary mismatches, with entry/observation parity
   affecting interpretation of the entire 100-record parser corpus. Measure real
   current-state TurnResolver/reducer outcomes, including existing REMOVE/CLEAR
   behavior; keep parser diagnostics separate. This also provides the base for
   complete region/state/safety/dry-plan observations.
2. **RC-FIXTURE-STATE-ENTRY** — all 4 critical NOT_RUN cases. Reuse existing scoped
   state/artifact APIs to initialize Pending and complete/truncated Dataset
   fixtures at the actual semantic entry. This unlocks Pending hijack and unsafe
   global-ranking acceptance rather than adding more easy single-turn tests.
3. **RC-BOUNDARY-METRIC** — 4 primary cases with potential silent formula/metric
   substitution. Reconfirm exact governed compound-metric boundaries on the
   faithful evaluation entry and independent contrasts before proposing a generic
   fix. Do not start another Gold-sentence patch during this checkpoint.

The mandatory infrastructure-first order follows the user's current instruction.
Among semantic work, compound metrics outrank the 9 simple-list shape cases because
of silent-query risk. This order is not based on which individual case is easiest.

## Deterministic fixes and overfit

All six reviewed changes are **GENERIC CONTRACT FIX**; no direct business-word or
Gold-sentence CASE_SPECIFIC_PATCH was proved. However, **OVERFIT_RISK=YES**:
repeated exposure to the development Gold, finite time grammar, narrow demonstrated
compound-span coverage and absent certified blind holdout. No fix was removed.
See `deterministic_fix_review.md` for each change's bounds and counterexamples.

Direct semantic Regex: 0 new. Schema validation regex declarations: 2, both digest
validation. New language/format tokens: 33, plus a separate 10-character numeral
alphabet. Business-word special cases: 0. Prompt sentence units: 0 at initial V2,
34 at first raw-input V2,92 now (19 parse + 63 draft + 10 probe), an increase of 58 from the
meaningful raw baseline. These are defined, reproducible sentence-volume counts,
not a claim that every sentence is one independent rule. The reused calendar
helper has 25 existing Regex call sites and 13 unchanged referenced pattern definitions.

## V2 SEMANTIC FREEZE GATE

**FAIL — formal Model Benchmark must not start yet.**

The certified Catalog Snapshot gate passes. Critical evaluability, divergence
convergence, prompt/schema acceptance freeze, complete Candidate Snapshot,
Evaluator freeze, untouched Blind Holdout and Critical Semantic Safety do not.
Hashing today's artifacts records a version; it is not the accepted freeze gate.

Required before formal comparison: execute the four critical state cases, unify
runtime observations with labels, complete canonical/IR/Oracle/safety and dry-plan
coverage, converge the risk-bearing root causes, freeze prompt/schema/candidates/
catalog/evaluator together, and certify an untouched holdout. All 7 semantic safety
gates are currently NOT_EVALUATED. Critical 160 offline regression passes do not
replace those missing observations. Full acceptance criteria and current hashes
are in `semantic_freeze_gate.json`.

Redis production recovery and native catalog publication remain Canary/Cutover
requirements; they are not global prerequisites for Offline Gold/Benchmark.
Shape-specific identity/relation facts remain explicit rather than silently
filled with name identity or fabricated joins.

## Checkpoint verification and stop

Both original score reports were reproduced for all existing evaluation fields.
Inventory uniqueness, case/root coverage, stage totals and status totals were
checked; old Gold labels remain unchanged. The latest code regression baseline is
Agent 3062 passed / 27 existing failed, Oagnet 677 / 8 existing, SQL 381 / 0; new-pass regression
losses 0 and collection errors 0. This document-only checkpoint does not repeat the
full suite or claim a new production test run.

Checkpoint model calls 0, source SQL 0, production writes 0, production source changes 0.
Current status: **SEMANTIC_TRIAGE_COMPLETE**; Offline Semantic Ready and
READY_FOR_USER_APPROVAL are not reached. Stop here as requested. V1 remains the
production route; no merge, canary or replacement was performed.
