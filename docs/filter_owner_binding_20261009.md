# Predicate ownership binding — 2026-10-09

## Scope and first divergence

PROVEN: the department-ranking question produced an unowned shared province
predicate. SQL could legally reach that dictionary through the dealer instead
of the hospital. Query subject, output grouping and predicate ownership are
different concepts; selecting a fact-table subject does not identify whose
location a filter constrains. Whether that wrong route alone explains the
original empty result remains UNKNOWN until a live counterfactual is checked.

The user authorized this fix and deployment of verified Agent, Oagnet and SQL
updates, explicitly retaining verified old failures only if no new failures
appear. The unrelated Java backend is excluded. No semantic writes, vector
rebuild, configuration replacement, authorization expansion or V2 cutover.

## Service changes

- Agent: optional owner on structured filters; preserve it during grounding,
  deduplication, planner-to-canonical handoff, contract completeness checks and
  generated-filter validation. Same literal on different owners stays separate.
  Before freezing extraction, uniquely explicit location modifiers in the
  current question ground the owner; grouping alone cannot choose it. Clinical
  departments are distinguished from enterprise sales business departments.
- Oagnet: use the declared owner and published relationship deterministically.
  Source-verified dictionary keys produce an owner-specific foreign-key filter
  with a per-predicate receipt. A missing/ambiguous owner is not inferred from
  subject, grouping or shortest path. Preserve direct name filters and existing
  entity-alias behavior; never convert ordinary names to IDs just for joins.
  Recovery of a name supplied to a declared foreign key is constrained to that
  exact published dictionary edge, not another dictionary of the same owner.
- SQL: existing deterministic translator consumes the explicit physical field.
  No additional natural-language interpretation and no new SQL code required.
- Preserve two server-only differences (semantic-description failure logging
  and a planner docstring) in the local version repository; their behavior is
  preserved, including when the planner source is updated for this repair.

The release also includes previously verified local final-summary/chart output
updates. SQL's MySQL semantic-store implementation already matches the server.

## Verification and release closure

- Local/server-isolated Agent affected critical suite: 190 passed.
- Server-isolated Oagnet critical suite: 529 passed.
- Server-isolated SQL full suite: baseline/candidate 647 passed, no failures.
- Server-isolated Oagnet full suite: baseline 1,508 passed / 11 old failures;
  candidate 1,532 passed / the same 11 failures; no removed cases.
- Server-isolated Agent full comparison: baseline 4,599 passed / 96 old failures
  / one pre-existing collection error; candidate 4,621 passed / exactly the same
  failures and collection error. Zero new failures or removed cases.
- Bounded Draft PR port: Agent 185 passed, Oagnet 272 passed. These are separate
  targeted results; the full counts above describe the canonical deployed
  snapshot, not a full regression claim for the divergent PR integration base.
- Native ingress original question and three owner-variation questions: 4/4
  completed without ASL clarification or SQL failure. Executable SQL confirms
  hospital ownership, dealer ownership, hospital ownership despite dealer
  grouping, and simultaneous dealer/hospital predicates. September 2025 and
  order count remain unchanged. The original clinical-department predicate is
  bound to the department name, not the sales business department.
- Original, dealer-ranking and hospital-location/dealer-grouping queries return
  zero rows; the dual-owner scalar aggregate returns one row. This validates
  binding/execution, not independent business-answer gold. Empty results do not
  prove business absence or establish the sole cause of earlier empty results.
- Three bounded rollouts completed, with exact-file backups: initial verified
  Agent/Oagnet updates (14 modules), owner/dictionary follow-up (three modules),
  and clinical-department extraction guidance (one module). Agent and Oagnet
  were restarted when affected. SQL already matched and was not restarted.
  Protected configuration/source hashes, fresh affected worker PIDs, Agent
  readiness, Oagnet vector health and SQL MySQL-source health passed.
  Final read-only audit confirms all 197 inventoried runtime Python/prompt files
  match the committed release and protected files remain unchanged.
- User explicitly authorized retaining only reproduced old failures. No new
  failure, collection error or removed case was introduced; no old assertion
  was relaxed. This is not reported as a full-suite pass.
- Draft PR #89 remains unmerged. Raw query/test artifacts, configuration and
  credentials are private and excluded from commits.

Tests cover positive/negative and multi-value filters, multiple owners sharing
one literal, missing keys/relationships, ambiguous edges, dictionary-only scope,
legacy ownerless non-geographic fields, alias compatibility, receipt isolation,
planner fast-path handoff, proper names versus explicit location modifiers,
province/city dictionary collisions, clinical versus business departments, and
unchanged time/query shape. Existing tests and
failure expectations were not weakened. Public stage-order regression passes.

Retained limitation: the existing empty-result presentation may still warn that
downstream filter-binding evidence is incomplete. The new scoped owner receipt
does not fabricate that separate evidence or convert empty data into a verified
business conclusion. No unrelated presentation behavior was changed for this.

During final audit, additional unverified local edits appeared in interpretation,
summary and orchestrator sources. They were preserved and excluded from this
verified release; the deployed snapshot is the committed release, not a claim
that every subsequently modified development file is deployed.

Current stage: V1 scoped binding repair and verified-update rollout. V2
replacement readiness is unchanged; this release is not a cutover approval.
