# Entity alias / dimension predicate binding closure

## Proven defect and minimal repair

PROVEN: an entity alias was present in the vector catalog and the model selected
the correct physical name field, but exact-label correction replaced that field
with a same-named dimension code. A dimension with multiple physical mappings
then failed to resolve. Broad value recovery also could not distinguish an
unrelated entity carrying the same literal. This was a binding defect, not proof
of a missing alias embedding.

- A whole entity name/alias used as a filter or display slot retains its parent.
  Explicit attribute names and explicit parameter owners keep their precedence.
- A dimension predicate resolves only against its published physical mappings
  and name attributes on the same table or registered logical parent.
- Governed value evidence must cover every input value on one unique field.
  An exact canonical value precedes an identifier's display-label alias.
- An exact physical attribute precedes a homonymous dimension. Multiple exact
  attributes and genuinely shared entity aliases remain ambiguous.
- Unauthorized/unrecalled fields, unknown IN members and conflicting owners
  cannot be invented, silently dropped or resolved by model rank.

No new business parameters, authorization, relationship paths, time grains,
metrics, operators or result limits are inferred. Existing normalization of
multi-value equality/inequality to IN/NOT IN is retained. No DataAnalysis, SQL
Translator, prompt, API/SSE, node-order or query-routing code is changed.

## Change manifest and review

`binding_ownership.py`, `structured_binding.py`,
`tests/test_filter_alias_binding.py`, this document.

Review covers absent aliases with governed dimension mappings, entity aliases,
wrong/missing/error model choices, same-valued unrelated fields, explicit owner
precedence, shared aliases, scope boundaries, name/code selection, mixed exact
attribute/dimension matches and all-or-nothing multi-value binding. Existing
tests and their assertions are unchanged.

## Validation and delta

| Offline suite | Baseline | Candidate |
| --- | --- | --- |
| Critical binding / recovery / scope / time / sort / limit | — | 344 passed |
| Isolated full suite | 1,331 passed, 16 failed | 1,359 passed, 15 failed |
| New alias/dimension regressions | — | 27 passed |

Zero old-pass to new-fail, zero missing existing cases, zero collection errors.
One baseline failure did not recur in the candidate run; it is not claimed as
a repaired defect. Known failures concern existing legacy-agent test hooks,
display resolution, geographic binding, retrieval and grouping expectations.
One existing native Chroma concurrency test crashes Windows with the original
code too; it was deselected from both comparison runs and is explicitly BLOCKED,
not counted as a pass. Test stores were isolated from production indexes.

Four isolated candidate runs against the real scoped vector catalog and model
validate entity alias, dimension without entity alias, entity name with quarter
range, and multi-value alias cases. These verify ASL binding, not correctness of
all SQL/business results. Structured extraction is unchanged in each run; no
production source, semantic database, vector index or runtime mode was changed.

## Release boundary / known blockers

Old test failures still block default deployment until explicitly waived or
resolved. No index rebuild or semantic publication is required for this repair.
An empty result for an uncovered historical date is separate from alias binding;
existing regional/relationship attribution is outside this minimal change.
Catalog/evaluation/shadow gaps and V1 replacement readiness remain unchanged.
