# Round 2.4 Source Value Proof Acquisition

**REAL_EXACT_SOURCE_VALUE_PROOF = FOUND_UNIQUE**

本轮从现有生产兼容只读链取得 `product.product_name = 耐高压植入式给药装置及附件` 的真实精确值证明。授权范围固定为 Semantic Model 81、Business Domain `[205]`；结果为 1 条唯一命中、集合完整。没有手工补 Fixture，没有使用模糊结果替代精确证明，也没有修改 Oagnet、Catalog 或源数据。

## SOURCE_VALUE_PRODUCTION_LOOKUP_PATH

真实调用链为：

1. `preserve_new_task_entity_instance` 仅在新任务、显式具体实体 surface、唯一受治理主属性等约束成立时请求值证明；
2. `ScopedPlanSession.lookup_source_values` 委托 `source_value_binding.lookup_values`；
3. `lookup_values` 将当前 Authorized Scope、Catalog Pin、Attribute、物理字段映射和数据源重新核对；
4. Oagnet `PinnedCatalog.lookup_entity_values` 进入 `catalog_value_sources.observe`；
5. `observe` 调用 `query_values`，使用已验证标识符和参数化值，在 `START TRANSACTION READ ONLY` 中执行有界 `SELECT DISTINCT`，随后 rollback 并关闭连接；
6. 返回 `VERIFIED_SOURCE_EXACT_LOOKUP / EXACT_NORMALIZED` receipt；Agent 再验证 Scope、Catalog Pin、mapping hash、observation hash、集合完整性和候选唯一性。

取证复用了 `tools/cutover/capture_source_values.py::capture`，属于优先级 A 的现有 Source Value service 路径。没有另写业务 SQL。该工具在读取前后核对 Catalog version，并把 receipt 封装为可离线回放的 PRIVATE bundle。

## 固定输入与结果

| 项目 | 结果 |
|---|---|
| Semantic Scope | `81 / [205] / EXPLICIT_DOMAINS` |
| Entity / Attribute | `product / product_name` |
| Attribute ID | `2092484689015631874` |
| Catalog ref | `b72b9d421365935f9b5bc2a3a2ec07c2f277bae7e0928c00ea64ae04ba0eb05e` |
| Catalog version | `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21` |
| Field mapping hash | `4f5733377404afa2354fdbc4521d77f5e8ed58b2ecc476da416949fb2393bc2a` |
| Match mode | `EXACT_NORMALIZED` |
| Result count | `1` |
| Complete | `true` |
| Query hash | `b2d72977df49eeb87afb529614b14775231adee75a5262681b695d30d1cb6655` |
| Observation hash | `ce2198fa6cdd3513df5a6e05872688ce449a5a2f165b7aef441a2e44f5ebfa08` |
| Production writes | `0` |

这里的 normalization 只执行当前正式合同：去除首尾空白，并把九种 Unicode 横线规范成 ASCII `-`；SQL 侧使用等价表达式并进行二进制比较。没有进行 `LIKE`、分词、别名推断或 fuzzy bind。

冻结 bundle 位于 PRIVATE `.eval_private/entity-instance-round2-4/source_value_observation.json`，SHA-256 为 `e542191c54df5b71ff5ceccb899a3632b9c9bd9d5382d11513c22d242b93521c`。连接信息、凭据和无关业务记录没有进入公开报告或 Git。
