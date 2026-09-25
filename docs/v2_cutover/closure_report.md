# V2 replacement readiness — controlled value probe

Current Stage: Candidate Probe implementation and evidence closure; next is the
user-requested SEMANTIC CUTOVER TRIAGE CHECKPOINT. Baseline `b79fbd02a0f054ea5bd6ab6106569ea0019b0a28` / Draft PR50.
Feature branch `cutover-source-alias-20260909t071700z`. Final commit: commit containing this report.
V1 Replacement Readiness: **NOT_READY**. V1 remains production.

## Actual behavior and evidence

Oagnet now performs bounded readonly discovery only after a complete empty exact
lookup on a current pinned field. Agent only probes a single selected field,
offers at most64 short real values, validates the chosen opaque ID, then uses the
original exact lookup/binding and final catalog acceptance. Source scope/mapping,
implicit-search policy and source drift guards remain enforced. No prefix/suffix
dictionary or business-specific branch was added. This is a new bounded semantic
choice prompt, not a deterministic alias certification.

The existing province short_name pairs are unique but do not contain the Gold
spellings Shanghai/Beijing/Jiangsu. No alias policy was inferred from that column.
The native province probe returned34 values; city exceeded64 and returned no
partial candidates. Source audit used23 readonly business SELECTs (2 pair audit,
21 exact/probe capture); production writes0. Frozen catalog version and scope81/205
were checked before and after capture. Raw values/captures and credentials stay
outside Git. No catalog publication, Redis mutation, API/SSE or routing change.

Three live qwen3.7-max calls with an explicitly selected province field chose all
three reviewed canonical values and passed current binding/finish. This field
Oracle is component evidence, not full-plan accuracy. The unchanged20-case live
Gold used52 calls:6OK/10FAILED/4NOT_RUN,26executed turns. City-field selections
hit the cardinality guard; other draft/shape/fixture failures remain. Every one
of the26 recorded turn results or failures reproduced exactly with no model/SQL
call. Total stage real model calls55; no production business writes.

## Validation and review

Agent3062passed/27preexisting failed; Oagnet677passed/8preexisting failed;
SQL381passed/0failed. Added38tests (Agent24/Oagnet14). Critical160passed;
collection errors0, old-pass to new-fail0, removed tests0. Focused189Agent and87
Oagnet tests pass. Old assertions unchanged; exact-only test fixtures gained the
new query and model stage. Checks include arbitrary candidate/scope rejection,
high cardinality, ambiguity, source/mapping drift, exact revalidation,
ADD/REPLACE/REMOVE/CLEAR and full native frozen replay.

Current acceptance groups remain8P0/4P1, not independent root-cause counts.
Catalog gap: strict shape identity/relation facts and production publication.
Evaluation gap: full labels/Oracle, real candidate ranking, Pending/Dataset
fixtures, region/safety observations and holdout. Shadow gap: no qualifying
semantic gate or real isolated shadow evidence. Redis recovery/publication remain
Canary/Cutover gates; they do not block this offline evaluation.

Per the latest user instruction, stop per-case semantic fixes after this commit
and perform a read-only root-cause/overfit/evaluator checkpoint. No claim of
SEMANTIC_FREEZE, model selection, shadow readiness or production replacement.
