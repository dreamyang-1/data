# Contextual ASL grain and prompt rule audit

## Scope and evidence

Current stage: targeted V1 ASL repair; no V2 cutover or API/SSE contract change.
PROVEN: the former quantifier normalizer could interpret part of a registered
metric label as an instruction to add an entity-name grouping. The previous
scalar fallback also inferred intent from keyword presence/absence. Both are removed.

Reviewed Oagnet's complete static generation prompt, PromptBuilder additions,
main's caller/reference/surface additions, surface role guidance, and the invoked
normalization/validation categories. Also read the upstream description-loading
path and business description. This is not deletion of every legacy normalizer.
No unrelated DataAnalysis or SQL Translator runtime code is changed.

## Decisions

| Rule family | Decision and reason |
| --- | --- |
| Quantifiers add dimensions; keyword absence removes them | Deleted. One contextual model review of completed question, original structured extraction, reference, draft and scoped catalog determines scalar/grouped grain. |
| Model grain review | Internal only, no public shape field. Accept only scoped dimension objects; preserve metrics, filters, time, having, subject and limits. Clear dangling dimension sorting only. Invalid/unavailable review preserves original ASL; later normal validation remains. |
| Always at least one metric; detail amount means aggregate | Removed conflicting instructions. Distinguish record attributes from calculations using full context. |
| Exact metric word must be output | In advisory-context path, substring presence no longer imposes a required metric (e.g. negated mention). Explicit caller-bound metric contracts still enforced. |
| Generic sales word forces amount/quantity clarification | No keyword-injected clarification in advisory-context path. Model still can report genuine missing meaning; unstructured legacy behavior retained. |
| Any empty catalog section forces clarification | Fixed: only wholly empty recall gets the empty-catalog reminder. Detail needs no metric and scalar counts need no grouping dimension. |
| Five hardcoded ecommerce examples | Removed: invented entity/metric/field codes and aggregate-only examples bias unrelated domains. Schema and short semantic contrasts remain. |
| Name always gets LIKE wildcards | Removed; exact vs fuzzy follows requested meaning and grounded value. |
| All time anchors must come from a metric | Corrected: published time dimensions/entity date attributes may bind a meaningful anchor; detail does not require a metric. No invented anchor. |
| Current sales/activity terms always mean time/date | Removed fixed prompt interpretation; use published meaning. Existing bounded date normalization and tested activity compatibility retained. |
| Different metric entities automatically require clarification | Removed; consult catalog dependencies/relationships. |
| Explicit multi-domain is encouraged in prompt | Removed unreachable contradictory prompt branch; strict scope contract retained. |
| Missing optional display field interrupts whole query | Prompt aligned with existing partial-projection behavior. Essential query conditions are not silently invented or discarded. |
| Schema, canonical aliases, scoped field binding, confirmed caller contract | Retained: these ensure executable authorized ASL rather than classifying business intent. |
| Relation traversal, brand/product role separation, IN/NOT IN, numeric/code literals | Retained: previously verified correct behaviors. No wholesale removal of associated tested normalizers. |
| Canonical value correction, duplicate-dimension cleanup, administrative-level preference | Retained: metadata-bound normalization and user-confirmed policy, not a new keyword classifier. |
| Record projection/time repair and explicit time granularity compatibility | Retained this round to protect tested behavior; some legacy lexical helpers remain. Their removal needs an equivalent context-based replacement, not blanket deletion. |

The upstream `语义描述文件.md` is loaded into the extraction user message, not
directly passed as the whole document to Oagnet. Oagnet receives the extraction
plus current recalled DSL. The description still contains default 12-month/
1000-row rules and conflates the product-department relation with dealer relations.
It is business reference, not trusted physical JOIN instructions. The file is not
edited in this release; deployed catalog mappings remain authoritative.

## Verification and review

- Oagnet mocked full baseline 972 passed; final 994 passed.
- Strict offline baseline 960 passed / 12 failed; final 982 passed / same 12 failed.
  No new failures or collection errors. Existing failures require missing
  database/vector test doubles; they are not hidden by production changes.
- 22 net additional tests (29 new node IDs, seven parametrized reference tests
  renamed for the new advisory-context contract).
- STALE_TEST: the existing missing dealer-grain test now supplies a contextual
  model decision instead of requiring regex insertion; same grouped result remains
  asserted. Existing scalar helper tests now exercise model decisions rather than
  keyword decisions. Mocks verify plumbing, not real-model understanding.
- DataAnalysis Critical Suite: 206 passed, including frozen node order and pending
  handoff. No DataAnalysis runtime changes, hence no unrelated full DA rerun.
- Review: prompt and code agree on context ownership; no caller scope widening;
  no new public response fields; no deletion of user-confirmed unrelated behavior.

## Deployment / readiness

Deployed code commit: `643b5a9`. Only `Oagnet/agent.py` and
`Oagnet/prompt_build.py` uploaded after confirming remote files matched the known
previous release; SHA-256 verified before/after and backup retained. No
configuration or business data writes. SQL and DataAnalysis processes unchanged.

Oagnet restart confirmed at 2026-09-24 11:06:53 CST; PID changed, ActiveState=active,
NRestarts=0, HTTP health=200/UP and vector health=true. Remote focused suite:
124 passed. Post-final-edit local focused suite: 73 passed.

Real-model ASL plus SQL translation: five of five passed, no ambiguity:

- Original full metric name, paraphrased hospital count and negated grouping:
  dimensions=[], COUNT query without GROUP BY (13.1 / 11.0 / 11.5 seconds ASL).
- Per-province count: published province dimension and SQL GROUP BY (10.7 seconds).
- Hospital-name list: metrics=[], hospital name projection, no GROUP BY (7.9 seconds).

The paraphrases selected different valid province/city filter fields for the same
municipality. Two additional scoped read-only scalar count executions returned
one row each and equal counts in current data. This is not evidence that all
future municipality bindings or underlying data are equivalent. No raw records
were fetched and no full platform SSE conversation was exercised in this smoke.

V1 replacement readiness: unchanged / not requested. Catalog/Evaluation/Shadow
gaps and existing strict-offline fixture failures remain outside this fix.
Current target's deployment verification is complete. Next product verification:
try a new platform conversation against the deployed service. Do not claim all
legacy business rules removed or blanket production readiness.
