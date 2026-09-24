# DSL business-rule precedence

## Scope and source interpretation

Current stage: targeted V1 ASL maintenance, not V2 replacement.
Interpretation used: DSL means the current authorized published semantic DSL,
not hardcoded numbers copied from the local business-description Markdown.
The source distinction was raised with the user; no alternate file was supplied.
Only Oagnet runtime files change. No DataAnalysis/SQL runtime changes, catalog
publication or environment edits. Deployment was separately requested after the
code commit; its verification is recorded below.

PROVEN conflicts: the generation prompt advertised published DSL defaults but
also forced absent user time to null and restricted limit to Top N. The legacy
date normalizer erased default ranges even when the selected metric supplied a
published `time_caliber.special_rule` and the anchor was a known field.

## Changes / boundaries

- Explicitly make scoped DSL business meaning and applicable defaults take
  precedence over generic prompt guidance/non-authoritative descriptions.
- More specific selected entity/metric rules take precedence over generic DSL
  defaults. User-supplied values override overridable defaults. Fixed metric
  definitions, confirmed caller contracts, scope and execution limits remain.
- Allow model-generated default time/row limits when supported by the applicable
  DSL. Do not hardcode 12 months or 1000 rows; null upstream hints do not prohibit
  an applicable DSL default. Do not confuse query limits with preview/export sizes.
- Preserve a generated default range in legacy normalization only when a selected
  metric has a nonempty published time special rule and its anchor is registered.
  Unselected metrics cannot supply the rule, unknown anchors are not authorized,
  and explicit date handling plus final validation remain unchanged.
- Existing prompt formatting already preserves `special_rule` and other nested
  metadata; do not add a new DSL schema or manufacture policies absent from the
  catalog. SQL already consumes explicit detail limits, so no translator patch.

## Verification / review

- Mock full baseline 994 passed; final 1003 passed, nine added tests.
- Strict offline baseline 982 passed / 12 failed; final 991 passed / same 12
  fixture failures. No newly failing or removed tests, no collection errors.
- DataAnalysis Critical Suite 206 passed; frozen node/Pending contracts unchanged.
- Tests cover selected vs unrelated policies, explicit date override, unregistered
  anchor, DSL text reaching the prompt, and non-ranking detail limits.
- Review confirmed no hardcoded business duration/row limit and no config writes.
  Tests are offline; this does not prove a live catalog currently publishes such
  defaults or that every free-text rule is correctly interpreted by the model.

V1 Replacement Readiness: unchanged, no cutover requested. Existing catalog,
evaluation and shadow readiness gaps remain outside this patch. Next shortest
task path: verify the actual published DSL defaults with representative user queries.

## Deployment (separately authorized)

Code version `c6bbcbb` deployed with exact-file SHA-256 verification and backup.
Remote prior runtime files matched `643b5a9`; no unknown concurrent code drift.
Only `Oagnet/agent.py` and `Oagnet/prompt_build.py` replaced. Configuration hashes
unchanged; SQL and DataAnalysis processes not restarted or modified.

Oagnet restarted at 2026-09-24 11:37:53 CST. New process confirmed, active,
NRestarts=0, health HTTP 200/UP, vector health=true. Remote focused tests:
133 passed, including nine DSL-precedence tests against deployed source.

Real-model smoke: initial 4/5 ASL + SQL translation cases passed. Three scalar
paraphrases and one detail projection passed. The per-province aggregate failed
once with `ASL_DIMENSION_INVALID`; server log reason was `ASL dimension attr was
not retrieved from semantic scope`. Two immediate fresh repetitions both passed
ASL and SQL translation, one using attr=null and one a registered binding ID.
No business SQL was executed during this deployment smoke.

Known issue: intermittent dimension-attr binding remains unresolved; successful
retries do not establish a fix or prove whether this is a new regression. Service
deployment and health are confirmed, but do not report full functional smoke as
unconditionally passing. No additional runtime fixes or blanket validation
relaxation were included in the deployment-only request.
