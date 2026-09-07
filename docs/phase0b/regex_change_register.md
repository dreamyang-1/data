# Regex change register

No StructuredIntent prompt edits. Four behavior families, five production expression sites:

| Expression family | Scope / reason | Positive | Negative / contrast | Regression |
| --- | --- | --- | --- | --- |
| Existing additive verb pattern: 加上 → 加上? in classifier and admission | Existing ADD verb syntax accepts the same optional particle; no metric-specific keyword | 再加订单笔数 | 换成订单笔数 must REPLACE; 不要订单笔数 must REMOVE | c03/c05; single_merge_operation_contrasts; slot_audit |
| Anchored display-only prefix + 前N条 | A whole display request selects rows in existing order; metric ranking is excluded | 只看前5条 | 销售额最高5名 is ranking, not limit | c10/c11; dataset_operation_contrasts |
| Explicit extrema count 最高/最低/最大/最小 + N + 名/条/个 | Extract count where the existing extrema branch already chose a field/direction | 销售额最高5名 / 最低2名 | 销售额从低到高 remains full sort | c11; dataset_operation_contrasts |
| Full-match temporal/grouping-only subject using re.escape(existing dimensions) | Reject an invented product only when the entire candidate is recognized structural text and a parsed period exists | 查询2026年7月按地区拆分销售额 | 查询2026年7月医用导管的销售额 retains product | original pytest-05/06; temporal_structural_guard_does_not_discard_product_text |

No regex is added for a single product, hospital or catalog role. Region CLEAR phrases are a finite set of explicit operation commands; explicit subsequent region reopens scope. Bare `<known dimension>名称` is an exact vocabulary match, excludes other text, and asks the list/group operation before resolving a role.
