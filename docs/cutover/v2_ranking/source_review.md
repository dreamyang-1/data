# Contract and self-review

Authoritative semantic requirements: `RankingSpec`, `OrderingRequirement` and
`ResultContractCompiler` in the existing V2 contract. The new code does not edit
those definitions, infer authorization or add a business default for 81/205.

The Python-only contract has strict keys and generated alias IDs, no arbitrary
SQL expression or source field, no model/domain override and no prompt input.
Aliases must equal the emitted projections; primary direction and limit must
also equal ASL. Missing policy receipt prevents Agent acceptance. Original pin,
source digest, allowed tables/data source and scope checks still run.

Null exclusion is applied in HAVING after aggregation, and null placement uses
an explicit IS NULL ordering key. Both ORDER BY and HAVING may reference quoted
output aliases in MySQL. References checked during review: [MySQL alias rules](https://dev.mysql.com/doc/refman/8.4/en/problems-with-alias.html)
and [MySQL NULL behavior](https://dev.mysql.com/doc/refman/8.4/en/working-with-null.html).
The tests execute the generated common SQL subset with SQLite; no native MySQL
execution/collation/performance result is inferred from those references.

For stable secondary order, only declared group keys or aggregate outputs are
accepted. Adding a raw field to GROUP BY solely for ordering would change the
ranking population and is rejected. Secondary null defaults remain exactly those
of the current ResultContract. Display-limit plans do not acquire ranking policy.

INCLUDE_TIES is an explicit remaining execution-contract gap. A generic subquery
workaround would conflict with `SQLTranslatorProd.validate_read_only_sql`; no
regex bypass, guard weakening or uncontrolled public ASL extension was added.
The entire V2 lowerer remains partial and the raw/public route is unchanged.
