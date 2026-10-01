# Round 2.2 Targeted Case Result

**ROUND22_TARGETED_RESULT = COMPLETE**

只复验 RB50-10、RB50-37、RB50-25。每条执行 CurrentTurn和SemanticEdits各一次，共6次模型调用；retry=0、前置轮模型调用=0、第三阶段=0、SQL=0、生产写入=0。固定 Scope 81/[205]、冻结 Catalog/Source Value和原问题文本保持不变。

| Case | Dynamic Schema | Semantic Draft | Runtime / Publication | 判定 |
|---|---|---|---|---|
| RB50-10 | FAIL | 未进入 Runtime | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION`；Task未发布 | `DYNAMIC_SCHEMA_VIOLATION_EARLY_REJECT` |
| RB50-37 | PASS | subject=产品、metric=含税销售总额；没有具体产品Filter | Task已发布，但查询范围扩大为全部产品 | `SCHEMA_COMPLIANT_BUT_SEMANTICALLY_WRONG` + `TASK_PUBLISHED` |
| RB50-25 | PASS | subject=产品、payload=`METADATA`，无Source Value request | `V2_PAYLOAD_WOULD_DROP_SEMANTICS`；Task未发布 | `DOWNSTREAM_VALIDATION_FAILURE` |

## RB50-10

CurrentTurn 将产品短语和“合作的医院”拆成 subject与relationship target，比 Round 2.1 的单一大 span更具体；SemanticEdits仍生成动态 Schema明确禁止的 Source Value request和field handle。新客户端在入口拒绝，`consumed_draft=NOT_REACHED`。这属于安全边界改善，尚无正确关系查询 Draft或Task发布。

## RB50-37

模型本次遵守动态 Schema，没有再生成非法 Source Value request。它发布了 `NEW_TASK / SCALAR_AGGREGATE`，绑定产品实体和含税销售总额，但没有保留“耐高压植入式给药装置及附件”这一具体产品限制。实际计划会统计全部产品，和原问题以及冻结 Benchmark期待的产品名称Filter不一致。

因此 `TASK_PUBLISHED` 不能计作业务正确。该 Case暴露出 exact Schema只能保证结构合法，不能保证模型选择了完整语义。

## RB50-25

本次输出遵守动态 Schema，不再生成 orphan Source Value request，随后在 payload coverage拒绝。原Case的两条前置轮都没有发布成功 Task，本轮按既有约束没有重跑前置轮，因此当前输入没有可恢复 TaskState。不能据此证明真实 REPLACE/FOLLOW_UP语义正确或错误；只能证明在该空状态诊断中 Schema边界通过、Task仍未发布。

## 证据与结论

- 3个case capture完整，6/6模型请求HTTP 200
- tokens：prompt 74,725；completion 2,205；total 76,930
- 三条总耗时：18.047s、15.156s、11.282s；只是定向诊断，不是SLA
- PRIVATE targeted analysis SHA-256：`34d7bbb302f08d55f501557a7949829a4daeab529dfe13d30088073da5d158d0`
- PRIVATE diagnostic manifest SHA-256：`604832db753e9a01dd4d6a922a6a1960549854f0b1c9caf6baa36804e4c831df`

本轮没有修改 Benchmark或重算分数。三条结果都不能成为新的业务正确 PASS：RB50-10安全提前拒绝；RB50-25仍下游拒绝；RB50-37静默丢失具体产品条件。
