# Filter ownership and grouping separation

## Scope and evidence

- Current stage: bounded V1 root-cause repair; no V2 routing change.
- PROVEN: SQL join planning preferred every grouping entity when resolving a
  filter table. Grouping by dealer could override a hospital subject's city
  route. The regression test fails against the pre-change translator.
- PROVEN: surface value normalization could rebind an existing hospital/dealer
  location predicate to another entity carrying the same literal, or collapse
  two different predicates. Six new cases fail against the pre-change agent.
- No semantic database writes, catalog publication, reindexing, deployment or
  service restart in this change. No DataAnalysis runtime changes.

## Changes

- SQL Translator: automatic grouping anchors now require a catalog-declared
  hierarchy (`level_list`). Ordinary grouped entities do not dictate filter
  ownership. Explicit role-owned filter fields and existing hierarchy-path
  behavior remain supported; source/scoping rules are unchanged.
- Oagnet: independently source-verified scalar predicates keep their field
  ownership when several tables contain the same value. Preserve `=` and `!=`
  and two independently requested same-value predicates. If the original field
  has no matching source value, existing correction still applies. Existing
  same-table specification/name correction and IN/NOT IN handling are unchanged.
- Prompt: determine filtering ownership separately from grouping using the
  completed question, structured reference and DSL. Prefer registered
  role-owned fields; do not invent columns or put labels into ID columns.
- No new public ASL/API/SSE fields, model call or model selection changes.

## Validation

| Suite | Baseline | Final |
| --- | --- | --- |
| SQL strict offline full | 475 passed | 479 passed |
| Oagnet full with dependency doubles | 1003 passed | 1010 passed |
| Oagnet strict offline full | 991 passed, 12 failed | 998 passed, same 12 failed |

No old-pass to new-fail or old-fail to new-pass changes in the strict suite;
11 new tests across the two services. No final-suite collection errors.
The 12 existing Oagnet failures require missing vector/MySQL test doubles;
they were reproduced on the unmodified version. They are not suppressed by
changing runtime validation or old assertions.

Relevant critical coverage is included in both full suites: semantic scope,
source grounding, relation authorization, alternative sets, literal handling,
and SQL hardening. No progress/SSE code changed.

Evidence (local, not committed): `filter-role-sql-baseline.json`,
`filter-role-sql-verified-final.json`, `filter-role-sql-red-confirmed.json`,
`filter-role-oa-strict-baseline.json`, `filter-role-oa-strict-final.json`,
`filter-role-oa-red-verified.json` under the workspace temporary directory.

## Boundary and next verification

A bare shared dictionary field such as `dim_city.city_name` still cannot say
whether its owner is a hospital or dealer. Removing grouping priority is NOT
proof that a shortest-path fallback recovers the user's business intent.
No hospital default is hardcoded and no metric formula is used to guess the
owner. Source existence alone also does not prove the model's interpretation.

The semantic team must publish role-distinguishable, physically executable
attribute/value mappings (or an already supported role-specific source).
Registered owner FK filters require real canonical codes, not guessed codes.
Changing only relationship descriptions does not change the SQL graph or add
missing filter ownership. A future per-filter relationship-path API would need
an explicitly agreed contract; it is not added here.

After publication and deployment, test the same metric grouped by dealer with
hospital-location and dealer-location questions separately, including both
locations in one question; inspect ASL field/value ownership and actual JOINs.
The original production question is therefore **not yet end-to-end verified**.
Catalog/production gap remains; V1 replacement readiness is unchanged. This
patch does not constitute V2 cutover approval.

## Exact change manifest

- `Oagnet/agent.py`
- `Oagnet/prompt_build.py`
- `Oagnet/tests/test_surface_set_filter_roles.py`
- `sql-translator/sql_translator_prod.py`
- `sql-translator/test_filter_role_joins.py`
- `docs/cutover/filter_role_routing_20260924.md`

Self-review: bounded changes only; no permissions weakening or new credentials;
counterexamples protect dealer filters and the existing grouped hierarchy;
remaining catalog ambiguity explicitly retained as an integration gap.
