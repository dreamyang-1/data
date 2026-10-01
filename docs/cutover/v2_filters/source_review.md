# Contract and self-review

The existing Predicate/BooleanFilterGroup, typed null operator and unary NOT
contracts are authoritative. No schema, reducer, prompt or business rule changed.
SQL leaf metadata, JOIN coverage and data source checks run on every branch.
The executable predicate always comes from the original Boolean tree; it is
not the flattened discovery list. Governed filters remain outside the OR/NOT
expression, and V2's explicit include_null_group setting is retained.

The private contract uses exact keys, fixed operators and typed JSON literals,
bounded tree/list sizes, a semantic fingerprint and the original full catalog
acceptance. It cannot carry a new permission scope or raw SQL. Wrong scopes,
unknown fields, missing/modified receipts and inconsistent ranking/filter
fingerprints fail before a plan can leave Agent. The integration tests also
verify recursively frozen caller artifacts are not mutated.

The literal writer doubles single quotes but does not bind parameters or prove
native SQL mode. Backslash and NUL values are refused on the V2 path, including
otherwise simple AND conditions. [MySQL string literal rules](https://dev.mysql.com/doc/refman/8.4/en/string-literals.html)
confirm that backslash interpretation depends on NO_BACKSLASH_ESCAPES. Native
connection mode/collation and parameterized execution are still missing evidence.
No claim that SQLite proves deployed MySQL behavior or business data correctness.

Remaining major gaps: native publication/recovery/trust, temporal storage rules,
multi-subject metrics, relationship/occurrence lowering, INCLUDE_TIES, real
results, Gold and actual shadow. This change does not close the full cutover gate.
