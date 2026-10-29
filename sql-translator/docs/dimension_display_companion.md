# Entity dimension display companions

## Cause

The published `city` dimension in model 81 binds `dim_city.city_id`.
The same entity declares `city_name` separately, without a main-attribute flag.
The translator previously projected the identity with the logical dimension's
human label. Rendering cannot recover a name from that value alone.

## Change

For an entity-named registered dimension bound to that entity's declared primary
key or entity-code `_id`/`_code` attribute, retain the identity and add a unique
same-table display attribute (main attribute or entity-code `_name`). Deduplicate
repeated metadata by physical field. Select the display name first and label the
identity column as a code; GROUP BY retains both, so equal names do not collapse
distinct identities. No name values, city codes, model IDs or business domains
are hard-coded into production logic. No new join or catalog write is performed.

Physical code projections, independently named code dimensions, enum/time
dimensions and absent/ambiguous/cross-table display mappings are not rewritten.
This deliberately does not invent mappings for every possible catalog shape.
Such mappings still require published metadata. This does not correct an
upstream request explicitly bound to the wrong physical field.

## Validation

- SQL Translator full suite before sort coverage: 429 passed. Final suite:
  430 passed, one local HTTP test failed with WinError 10053; rerunning that
  complete API test file passed all 7 tests. No assertion regression observed.
- New regressions: city, department, product, hospital and dealer; identity
  preservation; explicit code; missing/ambiguous/cross-table names; duplicate
  metadata; already-selected name.
- Source and local version repository synchronized with SHA-256 comparison.
- Live platform replay: HTTP 200, COMPLETED, 72.1 seconds, 31 rows with city
  names and retained identity codes. Compared with the preceding replay:
  all 31 identity/month/amount tuples are identical; all display names present.
  No raw result rows are committed.
- Scoped catalog flat bindings may omit resolved bind_entities. Ownership is
  established by the mapped field on the currently scoped entity, not by that
  optional list; a regression covers this actual production shape.

## Integration

B's pending error-detail task touches the same translator file. Merge that
candidate by reviewed diff; do not overwrite the display companion methods.
