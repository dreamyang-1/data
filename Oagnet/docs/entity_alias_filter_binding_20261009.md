# Governed entity and attribute label binding closure

## Proven defects and repair boundary

PROVEN: the original entity alias existed in the vector catalog, but exact-label
correction could replace a physical name field with a same-named dimension code.
Multiple mappings then failed to resolve; an unrelated entity carrying the same
literal could also win broad value recovery. This was not evidence of a missing
alias embedding.

The follow-up request explicitly expands this contract beyond department
filters. Reproductions additionally demonstrate: registered aliases separated by
the Chinese enumeration separator were not split; grouping/projection/sort did
not consistently retain a whole-entity alias's parent; same-name diagnostic
candidates without parent display names collapsed; model-selected value IDs
could replace an already exact standard value; and relationship review could
rewrite an already correctly owned name predicate onto an order foreign key.

Repairs apply to published metadata rather than business-specific name lists:

- Resolve canonical entity code/ID, then canonical entity name, then registered
  aliases. Support list, JSON-list and delimited alias/synonym representations.
- Keep explicit parameter ownership and exact attribute precedence. Whole-entity
  names/aliases supply ownership in filters, grouping, projection and sorting.
  Registered qualified attribute aliases retain their parent too.
- Group only through published identity dimensions; display through a unique
  published main attribute. Missing identity mappings do not authorize grouping
  by a name or inventing a new dimension. Multiple exact aliases remain ambiguous.
- Complete scoped owner recall before top-k neighbors hide relevant attributes.
  Existing bounded recovery for incorrectly typed model/specification values
  remains available; this is not an authorization expansion.
- Require complete standard-value evidence on one compatible field. Exact
  canonical values precede labels attached to a model-selected value ID; no IN
  member is silently dropped, swapped or combined across different fields.
- Preserve already directly owned predicates during relationship review. Shared
  dictionaries still use published relationships to distinguish business owners;
  existing shared-attribute query modes remain unchanged.
- Diagnostic candidate identity includes the parent entity, attribute identity
  and physical mapping, not only a display name. Duplicate copies of the same
  identity may collapse; different parents/fields do not. Qualified filter-value
  hints must match their parent as well as their attribute.

Shared aliases, conflicting owners, incomplete evidence and unavailable fields
remain unresolved. Unknown explicit owners with contradictory whole-entity
labels cannot fall back to arbitrary values; legacy field-only grounding with
incomplete entity-name metadata retains its prior behavior.

No query slots, metrics, operators, time grains, time ranges, limits, subject
selection policy, relationships or authorization are invented. Existing
multi-value equality normalization is retained. No DataAnalysis/SQL Translator
source, model prompt, API/SSE schema, node order, production configuration,
semantic definitions, vector indexes or runtime routing is changed.

## Exact change manifest and review

Five Oagnet sources: `binding_ownership.py`, `structured_binding.py`,
`prompt_build.py`, `query_binding_review.py`, `api.py`.

Tests: `tests/test_binding_label_matrix.py` (144 new cases),
`tests/test_filter_alias_binding.py` (27 retained cases); this closure document.

Matrix coverage includes dealer, hospital, product, manufacturer, salesperson,
company and department across filter/display/group/sort and wrong/missing/error
model choices. Further cases cover metadata encodings, attribute/dimension/
metric synonyms, qualified aliases, canonical-name precedence, shared aliases,
conflicting parents, foreign dimension mappings, multiple main attributes,
missing identity dimensions, same-field value-ID swaps, all four equality-set
operators, scoped recall, diagnostic identity and relationship-review protection.
Existing ownership/scope, name/code, cross-field model recovery, shared dictionary,
time, detail, sort, limit and shared-attribute regressions are included in review.

STALE_TEST: `test_alias_display_field_cannot_return_another_entity_attribute`
formerly expected ambiguity for a whole-entity display alias despite a unique
published main attribute. The expanded user contract requires resolving that
projection. Its assertion now requires the correct parent's exact physical main
field and no ambiguity; it still prohibits the neighboring entity's attribute.
The baseline uses the original assertion. No other old assertions were changed.

## Verification and reproducible delta

| Offline suite | Original modules | Candidate |
| --- | --- | --- |
| Critical binding/recovery/scope/time/detail/review | not separately rerun | 505 passed |
| Full suite, fixed hash seed and isolated test store | 1,349 passed, 25 failed | 1,493 passed, 25 failed |
| Additional cross-entity regression matrix | not present | 144 passed |

Zero old-pass to new-fail, zero old-fail to new-pass, zero missing existing cases,
zero collection errors; the old-failure set is identical. Both full runs use the
same fixed hash seed, isolated Chroma storage and the same one deselection.
Earlier unseeded runs had different failure counts and are not the comparison
used to claim this delta.

Existing failures include legacy-agent hooks, unsupported mocked display-store
methods, old geography/group/retrieval expectations and ten tests that attempt
an unmocked database connection unavailable in this local environment. They are
retained as failures, not claimed as repaired or passed. An existing native
Chroma concurrency test crashes Windows with original modules too:
`test_vector_rebuild_uses_model_level_lock_for_all_domain_forms`. It is explicitly
BLOCKED and deselected in both runs, not counted as a pass.

Four latest isolated runs using the real scoped vector catalog and configured
model passed ASL validation: the original department alias question, hospital
alias grouping, product alias projection and salesperson alias sorting. Extracted
parameters remained unchanged in every run. Published dimension sorting is a
valid canonical result when the input requests a whole-entity alias, rather than
an explicit name attribute. Prior isolated checks also cover a catalog without
an entity alias, quarter ranges and multi-value filters. These are ASL validation
checks, not end-to-end SQL/business-result assertions. No production sources,
semantic data or vector indexes were changed by verification.

## Release gate and unchanged stage

Synchronize only the eight manifested files, hash-check them, commit locally,
and update the existing functional branch and Draft PR. Do not include the
separate server-mirroring commit or other workspace changes. No automatic merge.

The 25 reproduced old failures and native-component blocker still gate default
deployment: no restart or production rollout is claimed. No index rebuild or
semantic publication is needed for these code repairs. Existing date coverage
and geographic/relationship attribution issues are separate from this label
binding repair; they have not been silently changed here.

Current stage: bounded Oagnet V1 grounding repair, not a V2 cutover stage.
Catalog/Evaluation/Shadow gaps and V1 replacement readiness are unchanged; these
tests do not establish production replacement readiness. The next shortest
release path is classification/resolution or explicit waiver of the reproduced
release blockers, followed by remote-difference checks and an Oagnet-only rollout.
